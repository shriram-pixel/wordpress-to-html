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

**This project has never run on Linux.** The code is cross-platform and its
Windows path is exercised daily, but every Linux-specific fix in it was made by
reading code, not by running it. That is worth less than it sounds: the same
approach once fixed Linux and broke Windows, with the whole test suite passing
either way, because the tests stubbed the very binary that rejected the flag.

So verify in this order. Each step costs more than the one before it and proves
more, and each fails in a way that names what to do. Rent the machine by the
hour for this -- an hour is about half a euro, and if something is wrong you
will know before committing to anything.

### 3.1 The machine is what you paid for  (1 minute)

```bash
lscpu | grep -E "^CPU\(s\)|Thread|Model name"   # cores vs threads, which CPU
free -g                                          # memory
df -h /srv                                       # disk, and that /srv is the big volume
ulimit -n                                        # open files
vmstat 1 5                                       # the "st" column: steal time
```

**Expect:** `ulimit -n` at 65535, steal time near zero.

`ulimit -n` of 1024 means `setup.sh` has written `/etc/security/limits.conf`
but PAM applies it only at **login** -- so this session never got it. Log out
and back in, or `ulimit -n 65535` for the current shell. A job runs a database,
a dozen PHP workers and a browser at once; 1024 is not enough, and it fails
mid-render as "Too many open files".

Steal time above ~5% means a noisy neighbour is using the cores you are paying
for. Rendering is latency-bound, so this hurts more here than it would
elsewhere.

### 3.2 Setup finishes without warnings  (~10 minutes)

```bash
sudo ./setup.sh
```

**Expect:** no yellow warnings, and a final "Setup finished" block.

It stops immediately if Python is older than 3.12 -- Ubuntu 24.04 ships 3.12,
22.04 ships 3.10 and cannot import the application at all. Everything else it
does (fonts, AppArmor, stopping the system MariaDB, the file limit, installing
Chromium as the run user rather than as root) is a failure that would otherwise
appear much later, disguised as something else.

### 3.3 The dependencies are real  (5 seconds)

```bash
.venv/bin/python scripts/doctor.py
```

**Expect:** `Ready to convert.`, and specifically these four lines:

```
OK   PHP                        8.3.x (system)
OK   MySQL / MariaDB            mariadb 10.11.x (system)
OK     mariadb-install-db       /usr/bin/mariadb-install-db
OK   Chromium                   /home/<you>/.cache/ms-playwright/...
```

`mariadb-install-db` is the one to check by eye. On Ubuntu the server lives in
`/usr/sbin` and its helpers in `/usr/bin`, and without that helper **no job can
create its database** -- MariaDB has no `--initialize` of its own. The doctor
refuses to say Ready without it.

Run the doctor as the user who will run conversions, not with `sudo`. Chromium
is found relative to `$HOME`, so as root it would report a browser that the run
user cannot see.

### 3.4 The fast tests  (~15 seconds)

```bash
.venv/bin/python -m pytest -q -m "not integration"
```

**Expect:** ~339 passed. A failure here is a broken install, not a broken
platform -- usually a missing Python package.

### 3.5 The integration suite -- the one that matters  (~5 minutes)

```bash
.venv/bin/python -m pytest -q -m integration
```

This starts a real MariaDB, restores a real WordPress into it, renders it in
Chromium, exports it and checks the result. It is the only step that exercises
the paths that differ between platforms, and it would have caught every Linux
bug found so far, in five minutes instead of two hours.

**If this passes, the platform is good.** If it fails, read the error rather
than retrying: they are specific.

| Message | Cause | Fix |
|---|---|---|
| "could not create the temporary database data directory" | AppArmor confining MariaDB | see section 6 |
| "the temporary database did not start" | pid-file or socket permissions | check `<job>/mysql-error.log` |
| Chromium fails to start | missing shared libraries | `sudo .venv/bin/playwright install-deps chromium` |
| "Too many open files" | the limit from 3.1 | `ulimit -n 65535` |

### 3.6 One real site  (~30-100 minutes)

```bash
.venv/bin/python convert.py /srv/backups/site.wpress -o /srv/exports --screenshots
```

Then open `/srv/wpsc-jobs/<job-id>/report/conversion-report.html` and read, in
this order:

1. **Quality check** -- problems first. `source_issues` are faults in the
   original site that the export reproduces faithfully; `problems` are the
   export's own.
2. **Case mismatches** -- references differing from a filename only in
   capitalisation. These resolve on Windows and 404 on Linux, so a site built
   on Windows can show them here for the first time.
3. **Visual similarity** -- below ~95% on a Linux run usually means **fonts**.
   The pages are not broken, they are set in substitute faces, which changes
   every line break. `fc-list | wc -l` should be in the hundreds, not single
   figures.
4. **Step timings** -- write them down. They replace every estimate in the
   time calculator with a measurement from this machine.

Finally, open the exported site itself:

```bash
cd /srv/wpsc-jobs/<job-id>/output && python3 -m http.server 8080
```

and look at the home page, a page with a slider, and one with a form. The
report cannot tell you the site looks right; only you can.

### 3.7 Update the estimates from what you measured

The time calculator extrapolates from one conversion on a 2011 desktop. After
3.6, put this machine's numbers into `app/services/estimator.py`:

* `SECONDS_PER_PAGE` = rendering minutes x 60 x pages-at-once / pages
* `FIXED_MINUTES`    = everything except rendering, in minutes

Both are printed in the report's step timings. Every figure the calculator
shows then comes from this machine instead of mine.

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
