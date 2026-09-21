# Running on a Linux server

Tested design, untested platform: the code is cross-platform and its Windows
path is exercised daily, but the first Linux run is tomorrow. Work through
this in order and you will know within an hour whether the server is good.

Everything below assumes **Ubuntu 24.04** as a non-root user with `sudo`.

**The release matters.** The tool needs **Python 3.12 or newer**
(`shutil.rmtree(onexc=)`); 24.04 ships 3.12, while **22.04 ships 3.10 and
cannot import the application at all**. `setup.sh` checks this first and stops
with instructions rather than building a virtualenv that will never work. On
22.04 you can install 3.12 from the deadsnakes PPA, but picking 24.04 is
simpler. Debian 13 ships 3.13 and works; Debian 12 ships 3.11 and does not.

---

## 1. Size the machine

Each conversion runs its own database, a pool of PHP workers and a browser:

| Resource | Per conversion | 4 at a time |
|---|---|---|
| Memory | ~3.6 GB | 16 GB, plus 8 GB for the system |
| CPU | 2–3 cores | 12–16 cores |
| Disk while running | 3× the backup | 4 × 3 × average backup |

**Recommended: 16 vCPU, 64 GB RAM, 500 GB NVMe.** Disk speed matters more
than clock speed: extraction and the database import are disk-bound.

---

## 2. Install (about 10 minutes)

```bash
git clone <your-repo> /opt/wpsc && cd /opt/wpsc      # or rsync the folder up
sudo ./setup.sh
```

If you **rsync** the folder from Windows rather than cloning it, delete three
things first — `.env`, `.venv/` and `runtime/`. A copied `.env` still says
`WPSC_JOBS_DIR=C:\wpsc-jobs`, which on Linux is not an error: it is a *relative*
directory with a backslash in its name, created wherever you happened to be
standing. Jobs then run successfully into a folder nobody thinks to look in.
`setup.sh` will not overwrite an existing `.env`.

`setup.sh` installs Python, PHP with the extensions WordPress needs, MariaDB,
Chromium and **fonts**, then writes a starter `.env`, checks the dependencies
and runs the fast test suite. Three things it does that are easy to miss by
hand, and each of which otherwise costs an afternoon:

* **Fonts.** A bare server has almost none. Without them every page renders
  with substitute faces: different line breaks, different layout, screenshots
  that do not match the original.
* **AppArmor.** Ubuntu confines MariaDB to `/var/lib/mysql`. A conversion runs
  its database inside the job workspace, which AppArmor denies — and the
  failure names no permission, so it looks like a corrupt backup.
* **The system MariaDB is stopped.** Jobs start their own on a random port;
  the service only competes for memory.

---

## 3. Prove it works, before the batch

```bash
.venv/bin/python scripts/doctor.py          # PHP, MariaDB, Chromium
.venv/bin/python -m pytest -q -m "not integration"    # ~300 fast tests
.venv/bin/python -m pytest -q -m integration          # a real conversion, ~5 min
```

The integration suite restores a real WordPress, renders it, exports it and
checks the result in a browser. If it passes, the platform is good.

Then convert **one real backup** and look at the result before trusting a
batch of fifty:

```bash
.venv/bin/python convert.py /srv/backups/site.wpress -o /srv/exports --screenshots
```

Open the report at `/srv/wpsc-jobs/<job-id>/report/conversion-report.html` and
read the **Quality check** section first.

---

## 4. Run the batch

```bash
.venv/bin/python run_batch.py /srv/backups -o /srv/exports --parallel 4
```

* Runs four conversions at a time; `--parallel` defaults to what the machine
  can hold (cores ÷ 4, memory ÷ 8 GB).
* One site failing never stops the batch.
* Re-running skips sites that already have a ZIP; `--retry` forces them again.
* Each site writes `/srv/exports/logs/<site>.log`.
* `/srv/exports/summary.csv` gets one row per site: pages, problems, source
  issues, visual score, minutes, ZIP path.

Run it under `tmux` or `screen` so closing the terminal cannot stop it:

```bash
tmux new -s batch
.venv/bin/python run_batch.py /srv/backups -o /srv/exports --parallel 4
# detach with Ctrl-B then D; return with: tmux attach -t batch
```

**Read `summary.csv` before publishing anything.** Sites with `problems > 0`
need a look; `source_issues` are faults in the original site (missing images,
dead links) that the export reproduces faithfully.

---

## 5. Security

The temporary WordPress runs with a password-less database, so it must never
be reachable from outside the machine.

* Leave `WPSC_HOST=127.0.0.1` and `WPSC_ALLOW_EXTERNAL_BIND=false`.
* Reach the web interface through a tunnel:
  `ssh -L 8000:127.0.0.1:8000 user@server`, then open `http://127.0.0.1:8000`.
* Run as an ordinary user, not root.
* Keep `/srv/backups` off any web root.

---

## 6. When something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| "could not create the temporary database data directory" | AppArmor confining MariaDB | `sudo ln -sf /etc/apparmor.d/usr.sbin.mariadbd /etc/apparmor.d/disable/ && sudo apparmor_parser -R /etc/apparmor.d/usr.sbin.mariadbd` |
| Pages render but fonts look wrong | No fonts installed | `sudo apt install fonts-liberation fonts-dejavu fonts-noto fonts-noto-cjk && fc-cache -f` |
| "the restored WordPress did not respond" | A slow first page, or a plugin erroring | Look at `<job>/php-server-1.log`; the first view after a restore can take minutes |
| Chromium fails to start | Missing shared libraries | `sudo .venv/bin/playwright install --with-deps chromium` |
| "Too many open files" | Default limit of 1024 | `ulimit -n 65535`, and check `/etc/security/limits.conf` |
| Jobs slow to a crawl | Too many at once for the memory | Lower `--parallel`; each job needs ~3.6 GB |
| "not enough free space" | Pre-flight needs 3× the backup | Free space, or point `--jobs-dir` at a bigger disk |

---

## 7. Tuning

Start with the defaults: the tool measures cores and memory at the start of
every job and logs what it chose, for example

```
this machine: 16 CPU(s), 62.8 GiB memory of which 48.1 GiB is free: 12 page(s)
at a time, 24 PHP worker(s), 8 HTML worker(s), 32 asset worker(s)
```

Only then tune, and measure rather than guess:

* `--parallel 2` vs `4` vs `6` on ten sites tells you the right number.
* `WPSC_RENDER_CONCURRENCY` caps pages per job if you want the machine
  responsive for other work.
* `--screenshots` roughly adds 15% to a job; worth it for the first batch of a
  new site, optional afterwards.
