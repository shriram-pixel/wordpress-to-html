"""Start and stop the throwaway MariaDB and PHP servers for one job.

Two processes are managed here, both private to a single job and both torn
down when the job ends:

* :class:`MysqlServer` -- a MariaDB instance with its own data directory inside
  the job workspace, listening on a random free port, reachable only from
  loopback, with networking restricted and no root password prompt. Nothing is
  installed as a service and no existing database on the machine is touched.
* :class:`PhpServer` -- PHP's built-in development server (``php -S``) with a
  router script, serving the restored WordPress. Using it removes Apache and
  Nginx from the dependency list.

The built-in PHP server handles one request at a time. That matters twice
over: concurrent page renders queue behind each other, and a page that makes a
request back to its own site -- WordPress and its plugins do this routinely --
deadlocks, because the only worker is busy serving the page that is waiting.

``PHP_CLI_SERVER_WORKERS`` would solve both, but it depends on ``fork()``. On
Windows PHP prints "forking is not supported on this platform" and silently
runs a single worker. :class:`PhpServerPool` therefore runs several independent
``php -S`` processes behind a small TCP load balancer on one public port, which
gives real concurrency and removes the self-request deadlock on every platform.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from app.services.runtime_provisioner import MysqlRuntime, PhpRuntime

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0


class ServerStartupError(RuntimeError):
    """A managed server did not come up in time."""


def find_free_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for an unused port.

    There is an unavoidable race between releasing the port here and binding it
    in the child process; in practice it is harmless for a local, short-lived
    tool, and startup failures are retried by the caller.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, 0))
        return sock.getsockname()[1]


def wait_for_port(host: str, port: int, timeout: float = 60.0, interval: float = 0.25) -> bool:
    """Block until *port* accepts a TCP connection, or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(interval)
    return False


def _terminate(process: subprocess.Popen | None, name: str, grace: float = 10.0) -> None:
    """Stop a child process, escalating to a kill if it ignores the request."""
    if process is None or process.poll() is not None:
        return
    logger.debug("stopping %s (pid %s)", name, process.pid)
    try:
        process.terminate()
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        logger.warning("%s did not stop gracefully; killing it", name)
        try:
            process.kill()
            process.wait(timeout=grace)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.error("could not kill %s: %s", name, exc)
    except OSError as exc:
        logger.debug("error stopping %s: %s", name, exc)


# ---------------------------------------------------------------------------
# MariaDB
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class MysqlServer:
    """A private MariaDB/MySQL instance for one job."""

    runtime: MysqlRuntime
    data_dir: Path
    socket_dir: Path | None = None
    host: str = "127.0.0.1"
    port: int = 0
    user: str = "root"
    password: str = ""
    process: subprocess.Popen | None = field(default=None, repr=False)
    log_path: Path | None = None

    # -- lifecycle ----------------------------------------------------------
    def initialise(self) -> None:
        """Create a fresh, empty data directory."""
        # Absolute, always: the server and its install helper are launched with
        # their own basedir as the working directory, so a relative --datadir
        # would be created underneath the MariaDB installation instead of in
        # the job workspace.
        self.data_dir = Path(self.data_dir).resolve()
        if (self.data_dir / "mysql").is_dir():
            logger.debug("reusing existing data directory at %s", self.data_dir)
            return

        self.data_dir.mkdir(parents=True, exist_ok=True)
        logger.info("initialising database data directory at %s", self.data_dir)

        if self.runtime.flavour == "mariadb" and self.runtime.install_db_binary:
            command = [
                str(self.runtime.install_db_binary),
                f"--datadir={self.data_dir}",
                "--default-user",
            ]
            if not _IS_WINDOWS:
                command.append(f"--basedir={self.runtime.base_dir}")
        else:
            # MySQL, and MariaDB builds without the helper, use --initialize.
            command = [
                str(self.runtime.server_binary),
                "--initialize-insecure",
                f"--datadir={self.data_dir}",
                f"--basedir={self.runtime.base_dir}",
            ]

        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=600,
            shell=False, cwd=str(self.runtime.base_dir), creationflags=_NO_WINDOW,
        )
        if proc.returncode != 0 or not (self.data_dir / "mysql").is_dir():
            raise ServerStartupError(
                "could not create the temporary database data directory:\n"
                f"{(proc.stderr or proc.stdout)[-1500:]}"
            )

    def start(self, timeout: float = 300.0) -> None:
        """Launch the server and wait until it accepts connections.

        The default allowance is generous because the slow case is not a fresh
        start: it is InnoDB crash recovery after a job was killed, which on a
        multi-gigabyte data directory routinely takes minutes.
        """
        self.initialise()
        self.port = self.port or find_free_port(self.host)
        self.log_path = Path(self.data_dir).parent / "mysql-error.log"

        command = [
            str(self.runtime.server_binary),
            f"--datadir={self.data_dir}",
            f"--port={self.port}",
            f"--bind-address={self.host}",
            # Loopback only, and no chance of clashing with a system instance.
            "--skip-name-resolve",
            "--skip-grant-tables",
            # skip-grant-tables removes authentication entirely, which is safe
            # only because the server is bound to loopback on a random port and
            # lives for the duration of one job. It also sidesteps every
            # difference in default auth plugin between MySQL and MariaDB.
            "--skip-networking=0",
            f"--basedir={self.runtime.base_dir}",
            f"--log-error={self.log_path}",
            # WordPress dumps frequently contain rows larger than the default.
            "--max_allowed_packet=512M",
            # Sized for a multi-gigabyte import; the default is far too small.
            "--innodb_buffer_pool_size=1G",
            "--innodb_log_file_size=512M",
            "--innodb_log_buffer_size=64M",
            "--innodb_io_capacity=2000",
            # Doublewrite protects against a torn page on power loss. For a
            # throwaway copy that is re-creatable from the backup it only halves
            # write speed; a killed process (the case resume handles) is safe
            # without it, because the operating system still completes writes.
            "--innodb_doublewrite=0",
            # A crawl issues many short connections.
            "--max_connections=200",
            "--innodb_flush_log_at_trx_commit=2",
            "--sql-mode=NO_ENGINE_SUBSTITUTION",
        ]
        if _IS_WINDOWS:
            command.append("--console")
        else:
            self.socket_dir = Path(self.socket_dir or tempfile.mkdtemp(prefix="wpsc-mysql-"))
            command.append(f"--socket={self.socket_dir / 'mysql.sock'}")

        logger.info("starting %s on %s:%d", self.runtime.flavour, self.host, self.port)
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(self.runtime.base_dir),
            shell=False,
            creationflags=_NO_WINDOW,
        )

        if not wait_for_port(self.host, self.port, timeout=timeout):
            detail = ""
            if self.log_path and self.log_path.exists():
                detail = self.log_path.read_text(errors="replace")[-1500:]
            self.stop()
            raise ServerStartupError(
                f"the temporary database did not start within {timeout:.0f}s.\n{detail}"
            )
        # The port opens slightly before the server will answer a query.
        self._wait_until_queryable(timeout=30)
        logger.info("database ready on port %d", self.port)

    def _wait_until_queryable(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with self.connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                return
            except Exception as exc:  # pymysql raises a broad family here
                last_error = exc
                time.sleep(0.4)
        raise ServerStartupError(f"the database accepted a socket but not a query: {last_error}")

    def stop(self) -> None:
        _terminate(self.process, "database server")
        self.process = None
        if self.socket_dir and Path(self.socket_dir).exists():
            shutil.rmtree(self.socket_dir, ignore_errors=True)

    def __enter__(self) -> "MysqlServer":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- access -------------------------------------------------------------
    def connect(self, database: str | None = None, **kwargs):
        """Open a PyMySQL connection to this instance."""
        import pymysql

        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=15,
            read_timeout=600,
            write_timeout=600,
            **kwargs,
        )

    def create_database(self, name: str) -> None:
        """Create *name*, replacing any previous attempt."""
        if not re.fullmatch(r"[A-Za-z0-9_]{1,60}", name):
            # Identifiers cannot be parameterised, so the name is validated
            # against a strict allowlist instead of being quoted and hoped for.
            raise ValueError(f"unsafe database name: {name!r}")
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
            cur.execute(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        logger.info("created temporary database %s", name)


# ---------------------------------------------------------------------------
# PHP built-in server
# ---------------------------------------------------------------------------
#: Router script for ``php -S``.
#:
#: The built-in server serves an existing file directly and otherwise calls
#: this script. WordPress pretty permalinks need every unmatched request to go
#: to index.php, which is what .htaccess normally does under Apache.
_ROUTER_PHP = r"""<?php
/**
 * Front controller for PHP's built-in server, generated by wp-static-converter.
 *
 * Mirrors the WordPress .htaccess rules: serve real files as-is, route
 * everything else through index.php so pretty permalinks resolve.
 */
$uri = urldecode(parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH) ?? '/');
$root = rtrim($_SERVER['DOCUMENT_ROOT'], '/\\');
$path = realpath($root . $uri);

// Never serve anything outside the document root, whatever the request says.
if ($path !== false && strpos($path, $root) === 0 && is_file($path)) {
    $name = basename($path);

    // wp-config.php holds the database credentials; it must never be readable
    // over HTTP, even though it never normally would be.
    if (strcasecmp($name, 'wp-config.php') === 0) {
        http_response_code(403);
        exit;
    }

    // Let the server handle static files itself (correct MIME types, ranges).
    if (substr($path, -4) !== '.php') {
        return false;
    }

    require $path;
    return true;
}

// Directory index.
if ($path !== false && is_dir($path) && is_file($path . '/index.php')) {
    require $path . '/index.php';
    return true;
}

require $root . '/index.php';
return true;
"""


def _opcache_args(runtime: PhpRuntime, cache_dir: Path) -> list[str]:
    """Command-line flags that switch OPcache on for the render server.

    Without it every request recompiles WordPress core, the theme and every
    plugin from source -- on a page-builder site that is most of the time a
    page takes to render. The built-in server runs under the CLI SAPI, where
    OPcache is off unless ``opcache.enable_cli`` is set.

    The file cache lets the pool's separate PHP processes, and later runs of
    the same job, reuse code another process already compiled. Timestamps are
    still checked, so a file rewritten mid-job (wp-config.php) is picked up.
    """
    extension_dir = Path(runtime.binary).parent / "ext"
    available = (
        (extension_dir / "php_opcache.dll").is_file()
        or (extension_dir / "opcache.so").is_file()
        or not _IS_WINDOWS  # usually compiled in on Linux and macOS
    )
    if not available:
        return []
    args: list[str] = []
    if (extension_dir / "php_opcache.dll").is_file() or (extension_dir / "opcache.so").is_file():
        args += ["-d", "zend_extension=opcache"]
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        args += ["-d", f"opcache.file_cache={cache_dir}"]
    except OSError:
        pass
    return args + [
        "-d", "opcache.enable=1",
        "-d", "opcache.enable_cli=1",
        "-d", "opcache.memory_consumption=256",
        "-d", "opcache.interned_strings_buffer=32",
        "-d", "opcache.max_accelerated_files=40000",
        "-d", "opcache.validate_timestamps=1",
        "-d", "opcache.revalidate_freq=2",
    ]


@dataclass(slots=True)
class PhpServer:
    """WordPress served by PHP's built-in development server."""

    runtime: PhpRuntime
    document_root: Path
    host: str = "127.0.0.1"
    port: int = 0
    workers: int = 4
    process: subprocess.Popen | None = field(default=None, repr=False)
    router_path: Path | None = None
    log_path: Path | None = None
    log_name: str = "php-server.log"
    _log_handle: object = field(default=None, repr=False)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 60.0) -> None:
        self.document_root = Path(self.document_root).resolve()
        if not (self.document_root / "index.php").is_file():
            raise ServerStartupError(
                f"{self.document_root} does not look like a WordPress install: no index.php"
            )

        self.port = self.port or find_free_port(self.host)
        self.router_path = self.document_root.parent / "router.php"
        self.router_path.write_text(_ROUTER_PHP, encoding="utf-8")

        self.log_path = self.document_root.parent / self.log_name

        command = [
            str(self.runtime.binary),
            "-S", f"{self.host}:{self.port}",
            "-t", str(self.document_root),
        ]
        if self.runtime.ini_path:
            command[1:1] = ["-c", str(self.runtime.ini_path)]
        command[1:1] = _opcache_args(self.runtime, self.document_root.parent / "runtime" / "opcache")
        command.append(str(self.router_path))

        environment = os.environ.copy()
        # Honoured on Linux and macOS only -- Windows has no fork() and ignores
        # it. PhpServerPool is the portable way to get several workers.
        if not _IS_WINDOWS and self.workers > 1:
            environment["PHP_CLI_SERVER_WORKERS"] = str(self.workers)

        logger.info("starting PHP %s server on %s", self.runtime.version, self.base_url)
        self._log_handle = self.log_path.open("wb")
        self.process = subprocess.Popen(
            command,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(self.document_root),
            env=environment,
            shell=False,
            creationflags=_NO_WINDOW,
        )

        if not wait_for_port(self.host, self.port, timeout=timeout):
            detail = self.tail_log()
            self.stop()
            raise ServerStartupError(
                f"the PHP server did not start within {timeout:.0f}s.\n{detail}"
            )
        logger.info("PHP server ready at %s", self.base_url)

    def wait_until_wordpress_responds(
        self, timeout: float = 180.0, expected_status: tuple[int, ...] = (200, 301, 302),
        should_stop=None,
    ) -> tuple[bool, str]:
        """Poll the home page until WordPress renders something real.

        An open port is not enough: WordPress may still be erroring on a
        database connection, so the body is inspected too.
        """
        def problem() -> str | None:
            if self.process is not None and self.process.poll() is not None:
                return f"the PHP server exited with code {self.process.returncode}"
            return None

        return _wait_for_wordpress(
            self.base_url, problem, self.tail_log, timeout, expected_status, should_stop
        )

    def tail_log(self, lines: int = 40) -> str:
        if not self.log_path or not Path(self.log_path).exists():
            return ""
        try:
            content = Path(self.log_path).read_text(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(content[-lines:])

    def stop(self) -> None:
        _terminate(self.process, "PHP server")
        self.process = None
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    def __enter__(self) -> "PhpServer":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


def _wait_for_wordpress(
    base_url, problem, tail_log, timeout, expected_status, should_stop=None
) -> tuple[bool, str]:
    """Poll the home page until WordPress renders something real.

    An open port is not enough: WordPress may still be erroring on a database
    connection, so the body is inspected too. *problem* returns a reason string
    when the server has died, so a crash fails fast instead of waiting out the
    whole timeout.
    """
    import httpx

    deadline = time.monotonic() + timeout
    last_detail = "no response"

    while time.monotonic() < deadline:
        reason = problem()
        if reason:
            return False, f"{reason}\n{tail_log()}"
        if should_stop is not None and should_stop():
            return False, "cancelled while waiting for WordPress"
        remaining = max(5.0, deadline - time.monotonic())
        # A single request cannot be interrupted, so when a cancel is possible
        # it is capped: the wait as a whole still runs to the deadline, but a
        # cancel is noticed in between rather than up to 15 minutes later.
        if should_stop is not None:
            remaining = min(remaining, 20.0)
        try:
            # The first request after a restore is genuinely slow -- WordPress
            # and page builders rebuild caches, per-page CSS and search indexes
            # on first view, which on a large site takes minutes. Give that one
            # request all the remaining time: abandoning it does not stop PHP,
            # it only means asking again and waiting for the same work.
            logger.info(
                "waiting for WordPress's first page (up to %.0f more seconds; the first "
                "view after a restore can take several minutes)", remaining,
            )
            with httpx.Client(timeout=httpx.Timeout(remaining, connect=15.0),
                              follow_redirects=False) as client:
                response = client.get(base_url + "/")
            body = response.text[:4000]

            if "Error establishing a database connection" in body:
                last_detail = "WordPress cannot reach its database"
            elif "There has been a critical error" in body:
                last_detail = "WordPress reported a critical error on the home page"
            elif response.status_code in expected_status:
                return True, f"HTTP {response.status_code}"
            else:
                last_detail = f"HTTP {response.status_code}"
        except Exception as exc:
            last_detail = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)

    return False, f"{last_detail}\n{tail_log()}"


class _TcpBalancer:
    """Spread TCP connections across several backend ports.

    Each incoming connection is piped, byte for byte, to whichever backend has
    the fewest connections open at that moment. Working at the TCP level rather
    than parsing HTTP keeps it tiny and means the Host header, keep-alive and
    streaming responses all pass through untouched.

    Least-busy selection is what breaks the self-request deadlock: when worker
    A, halfway through serving a page, requests another page from the same
    site, that connection is steered to an idle worker instead of queueing
    behind A itself.
    """

    def __init__(self, host: str, port: int, backends: list[int], idle_wait: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.backends = list(backends)
        self.idle_wait = idle_wait
        self.active = {backend: 0 for backend in self.backends}
        self._cond = None
        self._loop = None
        self._server = None
        self._thread = None
        self._ready = None

    def start(self) -> None:
        import asyncio
        import threading

        self._ready = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self._cond = asyncio.Condition()
            try:
                self._server = loop.run_until_complete(
                    asyncio.start_server(self._handle, self.host, self.port, backlog=256)
                )
            except BaseException as exc:  # surface bind failures to the caller
                errors.append(exc)
                self._ready.set()
                return
            self._ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._thread = threading.Thread(target=run, name="wpsc-balancer", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=15)
        if errors:
            raise ServerStartupError(f"the load balancer could not bind port {self.port}: {errors[0]}")

    async def _acquire(self) -> int:
        """Claim an idle backend, waiting briefly for one if all are busy.

        Idle, not merely least-busy. PHP's built-in server serves one request
        at a time, so sending a request to a worker that already has one means
        it queues behind that worker's current page -- and if that page is the
        one waiting on this request, nothing ever completes. That is the exact
        deadlock this pool exists to remove, so least-connections only applies
        as a last resort once no worker has freed up in time.
        """
        import asyncio

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.idle_wait
        async with self._cond:
            while True:
                idle = [port for port in self.backends if self.active[port] == 0]
                if idle:
                    backend = idle[0]
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    backend = min(self.backends, key=lambda port: self.active[port])
                    break
                try:
                    await asyncio.wait_for(self._cond.wait(), remaining)
                except asyncio.TimeoutError:
                    pass
            self.active[backend] += 1
            return backend

    async def _release(self, backend: int) -> None:
        async with self._cond:
            self.active[backend] -= 1
            self._cond.notify_all()

    async def _handle(self, client_reader, client_writer) -> None:
        import asyncio

        # PHP's built-in server closes the connection after every response, so
        # one connection is one request and "active connections" is an exact
        # count of busy workers.
        backend = await self._acquire()
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(self.host, backend)
        except OSError:
            await self._release(backend)
            client_writer.close()
            return

        async def client_to_backend() -> None:
            try:
                while True:
                    chunk = await client_reader.read(65536)
                    if not chunk:
                        break
                    upstream_writer.write(chunk)
                    await upstream_writer.drain()
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                pass
            # Half-close only. Closing the upstream socket outright when a
            # client gives up does not stop PHP -- it carries on with the
            # request -- but it would mark the worker free while it is still
            # busy, and the next request would queue behind it while every
            # other worker sat idle.
            try:
                if upstream_writer.can_write_eof():
                    upstream_writer.write_eof()
            except (ConnectionError, OSError, RuntimeError):
                pass

        async def backend_to_client() -> None:
            client_alive = True
            try:
                while True:
                    chunk = await upstream_reader.read(65536)
                    if not chunk:
                        break  # PHP finished: only now is the worker free
                    if client_alive:
                        try:
                            client_writer.write(chunk)
                            await client_writer.drain()
                        except (ConnectionError, OSError):
                            client_alive = False  # keep draining PHP regardless
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                pass
            finally:
                for writer in (client_writer, upstream_writer):
                    try:
                        writer.close()
                    except Exception:
                        pass

        try:
            await asyncio.gather(client_to_backend(), backend_to_client())
        finally:
            await self._release(backend)

    def stop(self) -> None:
        import asyncio

        loop = self._loop
        if loop is None:
            return

        async def shutdown() -> None:
            if self._server is not None:
                self._server.close()
            # Cancel the pending accept and any in-flight pipes and let them
            # unwind, otherwise asyncio prints "Task was destroyed but it is
            # pending" at exit on Windows.
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            loop.stop()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop)
        except RuntimeError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._loop = None


@dataclass(slots=True)
class PhpServerPool:
    """Several PHP built-in servers behind one port.

    Drop-in replacement for :class:`PhpServer`: same ``start``/``stop``,
    ``base_url``, ``wait_until_wordpress_responds`` and ``tail_log``. Pages
    render against ``base_url``; the workers each listen on a private port.
    """

    runtime: PhpRuntime
    document_root: Path
    host: str = "127.0.0.1"
    port: int = 0
    workers: int = 4
    servers: list = field(default_factory=list, repr=False)
    balancer: object = field(default=None, repr=False)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def log_path(self) -> Path | None:
        return self.servers[0].log_path if self.servers else None

    def start(self, timeout: float = 60.0) -> None:
        self.port = self.port or find_free_port(self.host)
        count = max(1, self.workers)
        logger.info(
            "starting %d PHP %s worker(s) behind %s", count, self.runtime.version, self.base_url
        )
        try:
            for index in range(count):
                server = PhpServer(
                    runtime=self.runtime,
                    document_root=self.document_root,
                    host=self.host,
                    port=find_free_port(self.host),
                    workers=1,
                    log_name=f"php-server-{index + 1}.log",
                )
                server.start(timeout=timeout)
                self.servers.append(server)

            self.balancer = _TcpBalancer(self.host, self.port, [s.port for s in self.servers])
            self.balancer.start()
        except BaseException:
            self.stop()
            raise

        if not wait_for_port(self.host, self.port, timeout=15):
            self.stop()
            raise ServerStartupError(f"the load balancer did not come up on {self.base_url}")
        logger.info("PHP pool ready at %s (%d workers)", self.base_url, count)

    def wait_until_wordpress_responds(
        self, timeout: float = 180.0, expected_status: tuple[int, ...] = (200, 301, 302),
        should_stop=None,
    ) -> tuple[bool, str]:
        def problem() -> str | None:
            dead = [s for s in self.servers if s.process is not None and s.process.poll() is not None]
            if len(dead) == len(self.servers):
                return "every PHP worker has exited"
            return None

        return _wait_for_wordpress(
            self.base_url, problem, self.tail_log, timeout, expected_status, should_stop
        )

    def tail_log(self, lines: int = 40) -> str:
        return "\n".join(
            f"--- worker {index + 1} ---\n{server.tail_log(lines // max(1, len(self.servers)))}"
            for index, server in enumerate(self.servers)
        )

    def stop(self) -> None:
        if self.balancer is not None:
            self.balancer.stop()
            self.balancer = None
        for server in self.servers:
            server.stop()
        self.servers = []

    def __enter__(self) -> "PhpServerPool":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


@dataclass(slots=True)
class WordPressEnvironment:
    """The pair of servers making up one running WordPress, started together."""

    mysql: MysqlServer
    php: PhpServer

    @property
    def base_url(self) -> str:
        return self.php.base_url

    def stop(self) -> None:
        # PHP first: it holds connections to the database.
        self.php.stop()
        self.mysql.stop()

    def __enter__(self) -> "WordPressEnvironment":
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
