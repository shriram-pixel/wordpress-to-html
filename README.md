# WordPress → Static HTML converter

Turn an All-in-One WP Migration **`.wpress`** backup into a **deployable static
website**, without touching the live site.

```
website.wpress  ─▶  [ restore locally → render in Chromium → localise → validate ]  ─▶  website-static.zip
```

You do not need the live site to be online. You do not need hosting credentials.
You do not need to install WordPress, PHP, MySQL, Apache or Nginx yourself.
Select the file, press Start, download the ZIP.

---

## How it works, and why

The hard part of converting WordPress to static HTML is not extracting files —
it is reproducing what a visitor actually sees. A page's final appearance is the
product of the theme, the plugins, the page builder and the JavaScript that runs
in the browser. Any tool that tries to re-implement Elementor, Divi, WPBakery or
Gutenberg markup by hand will get it subtly and endlessly wrong.

So this tool does not do that. It **runs your WordPress site** and photographs
the result:

```
.wpress backup
      │
      ▼  extract (pure Python, no external binary)
extracted files + database.sql
      │
      ▼  restore into a private, throwaway WordPress
temporary WordPress on http://127.0.0.1:<random port>
      │
      ▼  render every page in real Chromium, after JavaScript has run
final DOM, exactly as a visitor's browser builds it
      │
      ▼  download assets, rewrite every URL to a local path
static directory
      │
      ▼  validate links, assets, console errors, screenshots
website-static.zip
```

Whatever your theme and builder produce is what gets saved. No design is
reconstructed, no markup is simplified, no CSS is generated, and no JavaScript
is stripped.

---

## Quick start (Windows)

```powershell
.\setup.ps1     # one-time: virtualenv, dependencies, Chromium, PHP + MariaDB
.\run.ps1       # starts the app and opens http://127.0.0.1:8000
```

Then: **choose your `.wpress` file → Start conversion → wait → Download ZIP.**

Check the machine at any time with:

```powershell
.\run.ps1 -Doctor
```

### What setup installs

Nothing system-wide. No services, no PATH changes, no registry entries, and no
modification to any PHP, MySQL or WordPress already on the machine.

| Component | Where it goes | Size |
|---|---|---|
| Python packages | `.venv\` | ~90 MB |
| Chromium (Playwright) | `%LOCALAPPDATA%\ms-playwright` | ~170 MB |
| PHP 8.2 (portable, NTS) | `runtime\php\` | ~30 MB |
| MariaDB 11.4 (portable) | `runtime\mariadb\` | ~400 MB unpacked |
| WordPress core | `runtime\wordpress\` | ~40 MB |

If XAMPP, Laragon, WAMP, MAMP or a PHP/MySQL on `PATH` is already present, it is
detected and used instead of downloading anything. Point at a specific install
with `WPSC_PHP_BINARY` / `WPSC_MYSQLD_BINARY` in `.env`.

### Other platforms

The pipeline is cross-platform; only the *automatic download* of PHP and MariaDB
is Windows-specific. On Linux or macOS install them yourself and the tool finds
them:

```bash
sudo apt install php-cli php-mysqli php-gd php-curl php-mbstring php-zip mariadb-server
brew install php mariadb

python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python app.py
```

---

## Using it

### Web interface

`http://127.0.0.1:8000` — drag in a `.wpress` file, choose options, watch the
live progress, download the result. The dependency state is shown in the header.

### Command line (recommended for large backups)

```powershell
.venv\Scripts\python.exe convert.py C:\backups\site.wpress
```

This runs the identical pipeline **against the file where it already is**. The
web interface has to receive the backup over HTTP, which means it exists twice
before work starts — once in the framework's staging area, once in the job
workspace. For a 3 GB backup that is 6 GB of pure duplication. The CLI has
neither copy.

```powershell
convert.py site.wpress -o D:\exports            # put the ZIP somewhere specific
convert.py site.wpress --jobs-dir D:\wpsc-jobs  # work on a roomier drive
convert.py site.wpress --no-media               # skip video and audio
convert.py site.wpress --tags --authors -c 5    # more content, more concurrency
convert.py --help                               # every option
```

### HTTP API

The UI is a thin client over a normal REST API.

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/jobs` | Upload a `.wpress` and start a conversion |
| `GET` | `/api/jobs` | List recent jobs |
| `GET` | `/api/jobs/{id}` | Status, progress, per-URL counts, summary |
| `GET` | `/api/jobs/{id}/logs?after=N` | Structured events since event `N` |
| `GET` | `/api/jobs/{id}/urls` | Every discovered URL and its checkpoint state |
| `GET` | `/api/jobs/{id}/report` | `conversion-report.html` |
| `GET` | `/api/jobs/{id}/report.json` | The same report as JSON |
| `GET` | `/api/jobs/{id}/download` | The finished ZIP |
| `GET` | `/api/jobs/{id}/log` | Raw job log |
| `GET` | `/api/jobs/{id}/screenshots` | Screenshot and diff index |
| `POST` | `/api/jobs/{id}/cancel` | Stop a running job |
| `DELETE` | `/api/jobs/{id}` | Delete a job and its workspace |
| `GET` | `/api/health` | Dependency diagnostics |

```bash
curl -F "file=@website.wpress" \
     -F 'options={"include_tags":true,"render_concurrency":5}' \
     http://127.0.0.1:8000/api/jobs
```

Job status values: `QUEUED`, `EXTRACTING`, `RESTORING`, `STARTING_WORDPRESS`,
`DISCOVERING_URLS`, `RENDERING`, `GENERATING_HTML`, `DOWNLOADING_ASSETS`,
`VALIDATING`, `ZIPPING`, `COMPLETED`, `FAILED`, `CANCELLED`.

---

## What you get

```
website-static.zip
├── index.html
├── about/index.html
├── about/accessibility/index.html      ← hierarchy preserved
├── services/index.html
├── contact/index.html
├── journal/index.html
├── journal/page/2/index.html           ← pagination becomes directories
├── category/<slug>/index.html
├── projects/<slug>/index.html          ← custom post types, at their real URLs
├── wp-content/uploads/…                ← original asset paths kept
├── wp-content/themes/…
├── wp-includes/…                       ← core front-end CSS and JS
├── robots.txt
└── sitemap.xml
```

URLs keep their structure: `https://example.com/about/` becomes
`about/index.html`, so the exported site answers on the same paths as the
original. Links between pages are **relative**, so the export works equally well
at a domain root or in a subdirectory.

Deploy by unzipping into any static host — Apache, Nginx, cPanel `public_html`,
Netlify, Vercel, Cloudflare Pages, GitHub Pages, S3. No PHP, no database, no
configuration.

### The conversion report

Every job produces `conversion-report.html`: what was found, what was exported,
how many assets of each kind, broken links, missing assets, console errors,
screenshot similarity, and — most importantly — an explicit list of features
that **will not work** without a server.

---

## Limitations

A static site has no backend. This tool is candid about what that costs rather
than pretending otherwise: the front-end markup and styling of these features is
preserved exactly, so pages still *look* right, but the behaviour is gone and
each one is named in the report.

### Cannot work without a server

| Feature | What happens |
|---|---|
| **Contact forms** (CF7, WPForms, Gravity, Elementor, Ninja, Formidable) | The form renders and can be typed into. Submitting does nothing. Repoint it at a third-party form service, or add a serverless endpoint. |
| **WordPress search** | The search box renders. There is no index to query. Add a client-side search (Lunr, Pagefind) or a hosted one (Algolia). |
| **Comments** | Existing comments are exported as page content. New comments cannot be posted. Use Disqus, Giscus or similar. |
| **WooCommerce** | Product pages export as pages. Cart, checkout and payment are server-side and cannot be made static. |
| **Login, membership, paywalls** | Authentication needs PHP. Anything gated is either exported in full or not at all, depending on what the crawler could see. |
| **AJAX / REST calls** | Scripts calling `admin-ajax.php` or `/wp-json/` get no response. Detected and reported per page. |
| **Scheduled or personalised content** | The export is a snapshot. "Popular this week", countdowns driven server-side, and per-visitor content freeze at capture time. |

### Exported but worth knowing

- **Forms** keep their markup and have `action=""` plus `data-wpsc-static`, so
  submitting reloads the page rather than hitting a 404.
- **Admin links** become `#` with `data-wpsc-removed`, so the layout is unchanged
  and no dead `/wp-admin/` link ships.
- **External resources** (Google Fonts, CDNs, YouTube, Maps, analytics) keep their
  original URLs by default rather than being mirrored — that is usually both
  unnecessary and not ours to do. Set `external_policy` to `download` to localise
  them, or `block` to strip them.
- **Multisite** exports restore and convert the primary site only.
- **Infinite scroll** is followed to the point where the page stops growing, not
  forever.
- **Plain permalinks** (`?p=123`) export to paths like `page_id-7/index.html`.
  Switching to pretty permalinks before taking the backup gives much cleaner URLs.
- A small number of plugins are **deactivated for the render** because they
  actively prevent one: page caches (they would serve HTML built for the old
  domain), `really-simple-ssl`, and host-blocking security plugins. Each is named
  in the report.

### Similarity scores are a diagnostic, not a pass mark

The report gives a per-page pixel similarity between the original and the
exported render. Treat it as a pointer, never a verdict: a page can score 99% and
still be missing its logo, and can score 85% purely because a carousel stopped on
a different slide. Diff images are saved so you can look.

---

## Large backups (multi-gigabyte `.wpress`)

Big backups work, but they are bound by disk rather than by memory or CPU.
Nothing in the pipeline loads a whole file into RAM: the upload, the extraction,
each asset download and the ZIP are all streamed in 1–8 MiB chunks.

Use `convert.py` rather than the browser, and plan for about **3× the backup
size** in free disk space. For a 10 GB backup:

| Stage | Via `convert.py` | Via the browser | Notes |
|---|---|---|---|
| Staging copy | — | 10 GB | The web framework spools the request body to the system temp directory |
| `input/` copy | — | 10 GB | The CLI reads the archive where it already lives |
| Extracted / WordPress | ~10 GB | ~10 GB | `wp-content` is **moved**, not copied, so it exists once |
| Generated site | ~10 GB | ~10 GB | Mostly the media it localises |
| ZIP | ~10 GB | ~10 GB | Media is stored, not recompressed |
| **Peak** | **~30 GB** | **~50 GB** | |

Two things keep the peak down, both automatic:

- **`wp-content` is moved into the WordPress tree, not copied.** On the same
  volume that is a rename, so the site's media never exists twice.
- **The extracted tree is deleted as soon as the database is imported**, before
  rendering begins — which is exactly when the output directory starts growing.

An upload through the browser is refused immediately — before the transfer
starts — when there is not enough room, rather than failing an hour later.

Recommended settings for a large site:

```dotenv
WPSC_JOBS_DIR=D:\wpsc-jobs          # a drive with plenty of room
WPSC_KEEP_JOB_WORKSPACE=false       # discard intermediates as soon as possible
WPSC_RENDER_CONCURRENCY=5           # rendering is the slow part, not the size
```

On Windows, also point the temp directory at the same roomy drive so the upload
is not staged on `C:`:

```powershell
$env:TEMP = 'D:\temp'; $env:TMP = 'D:\temp'; .\run.ps1
```

Other things worth knowing:

- **Conversion time scales with page count, not archive size.** A 10 GB backup
  that is mostly media and has 200 pages takes about as long as a 300 MB one
  with 200 pages — roughly 2–4 seconds per page, plus extraction and packaging.
- **Individual assets above 512 MB are skipped** and listed in the report. A
  single video that large is rarely wanted in a static export.
- **Uncheck "Download video and audio"** (or set `download_media: false`) to
  leave large media referenced but not copied — often the difference between a
  10 GB and a 300 MB export.
- **Trimming the backup is the biggest win.** Excluding `wp-content/uploads`
  from parts of the library you do not publish shrinks every stage at once.

---

## Configuration

Copy `.env.example` to `.env`. Everything is optional.

| Variable | Default | Meaning |
|---|---|---|
| `WPSC_HOST` / `WPSC_PORT` | `127.0.0.1` / `8000` | Where the UI listens |
| `WPSC_ALLOW_EXTERNAL_BIND` | `false` | Allow non-loopback binding (see Security) |
| `WPSC_JOBS_DIR` | `./jobs` | Job workspaces |
| `WPSC_RUNTIME_DIR` | `./runtime` | Cached PHP, MariaDB, WordPress |
| `WPSC_MAX_UPLOAD_BYTES` | 20 GiB | Upload ceiling |
| `WPSC_KEEP_JOB_WORKSPACE` | `true` | Keep intermediates for diagnosis |
| `WPSC_AUTO_PROVISION_RUNTIMES` | `true` | Download PHP/MariaDB when missing |
| `WPSC_PHP_BINARY` / `WPSC_MYSQLD_BINARY` | auto | Use a specific install |
| `WPSC_RENDER_CONCURRENCY` | `3` | Browser pages at once |
| `WPSC_ASSET_CONCURRENCY` | `8` | Parallel asset downloads |
| `WPSC_MAX_URLS` | `5000` | Crawl ceiling |
| `WPSC_PAGE_TIMEOUT_MS` | `45000` | Per-page render budget |
| `WPSC_EXTERNAL_POLICY` | `preserve` | `preserve` / `download` / `block` |

Per-job options (the UI checkboxes) are sent with each conversion: which content
types to export, lazy-asset capture, JavaScript, validation passes, screenshot
comparison, and mobile checks.

---

## Security

`.wpress` files are treated as untrusted input throughout.

- **Path traversal is blocked.** The reference Go extractor builds its
  destination with `path.Clean("./" + prefix + "/" + name)`, which lets a crafted
  archive write anywhere on disk. Every member here is sanitised and re-checked
  for containment, Windows reserved device names are escaped, and drive letters
  and UNC roots are stripped.
- **Every job is isolated** in its own workspace, with its own database and its
  own random port. Jobs cannot see each other.
- **The temporary WordPress is loopback-only**, exists for the duration of one
  job, and is destroyed afterwards. Its database is created fresh and dropped.
- **Outbound HTTP from the restored site is blocked** during the crawl, so a
  plugin in the backup cannot call home, check a licence or fetch an update.
- **No PHP ships in the ZIP.** `wp-config.php`, SQL dumps, `.env`, logs, keys and
  every `*.php` file are excluded, and the archive is verified after packaging.
- **The ZIP is checked for completeness too** — an over-broad exclusion rule
  once dropped real stylesheets while every other check still passed, so the
  packaged file list is now compared against the validated output.
- **Error messages are scrubbed** of host filesystem paths before reaching the
  browser.

Running the restored WordPress means **executing the PHP inside the backup**.
That is unavoidable — it is the only way to render the site faithfully — so
convert backups you trust. The isolation above limits the blast radius; it is not
a sandbox.

There is **no authentication**. Keep it on `127.0.0.1`.

---

## Development

```powershell
.venv\Scripts\python.exe -m pytest -m "not integration"   # fast suite (~3s)
.venv\Scripts\python.exe -m pytest -m integration         # full pipeline (~4min)
.venv\Scripts\python.exe -m pytest                        # everything
```

Integration tests need PHP, MariaDB, Chromium and a demo fixture. Build the
fixture — a real WordPress install, seeded with pages, posts, categories, tags,
a custom post type, a menu and images, then exported exactly as All-in-One WP
Migration does:

```powershell
.venv\Scripts\python.exe scripts\make_fixture.py
```

### Layout

```
app/
├── main.py                      FastAPI app and logging
├── config.py                    Settings and per-job options
├── api/         jobs.py, downloads.py
├── models/      job.py          Job record and SQLite store
├── services/
│   ├── wpress_extractor.py      .wpress format, extraction backends
│   ├── runtime_provisioner.py   find or download PHP and MariaDB
│   ├── wordpress_restorer.py    rebuild WordPress, import the DB, rewrite URLs
│   ├── wordpress_runner.py      MariaDB and PHP process lifecycle
│   ├── url_discovery.py         manifest, permalinks, sitemaps, links
│   ├── browser_renderer.py      Chromium capture and readiness
│   ├── asset_manager.py         asset download and CSS localisation
│   ├── html_processor.py        DOM rewriting, dynamic-feature detection
│   ├── url_rewriter.py          URL → output path, CSS URL rewriting
│   ├── static_validator.py      link, asset and console validation
│   ├── visual_validator.py      screenshot diffing
│   ├── zip_builder.py           packaging and verification
│   ├── report_generator.py      conversion-report.html
│   ├── pipeline.py              stage orchestration
│   └── job_manager.py           background workers
└── utils/       security.py, urls.py, filesystem.py, phpserialize.py
```

### Two pieces worth reading

**`utils/phpserialize.py`** — WordPress stores widgets, menus, theme mods and
Elementor layouts as PHP-serialized strings that encode the *byte length* of
every nested string. A plain search-and-replace of the site URL desynchronises
those lengths and WordPress silently drops the value, which is how a conversion
loses exactly the things it was meant to preserve. Values are parsed, rewritten
leaf by leaf, and re-serialized with recomputed lengths. Data that looks
serialized but will not parse is left untouched rather than damaged further.

**`services/wpress_extractor.py`** — the `.wpress` format, documented from
reading the reference implementation:

| Field | Offset | Length | Contents |
|---|---|---|---|
| Name | 0 | 255 | filename only, NUL-padded |
| Size | 255 | 14 | ASCII decimal byte length |
| Mtime | 269 | 12 | ASCII decimal unix time |
| Prefix | 281 | 4096 | directory path |

4377-byte header, then the raw payload, repeated; EOF is a zero-filled header.
No compression, no checksum, no index. The implementation here is pure Python
behind a `WpressExtractor` interface, so the backend can be swapped without
touching anything else; an adapter for the original Go binary is included.

---

## Troubleshooting

**"Missing PHP / MySQL"** — run `.\setup.ps1`, or `.\run.ps1 -Doctor` to see
exactly what is missing and how to install it.

**PHP downloads but will not run** — install the Microsoft Visual C++
Redistributable: <https://aka.ms/vs/17/release/vc_redist.x64.exe>

**"the restored WordPress did not respond"** — usually an incomplete database
import or a plugin in the backup erroring fatally. `jobs\<id>\logs\job-<id>.log`
has the PHP output.

**Pages missing from the export** — enable *Follow internal links*, turn on the
content types you want (tag and author archives are off by default), and raise
`WPSC_MAX_URLS` if the log reports a cap.

**Conversion is slow** — rendering dominates, at roughly 2–4 seconds per page.
Raise *Concurrent pages* to 5–10 on a capable machine; turn off screenshot
comparison, which is the most expensive validation.

**Out of disk** — a conversion needs roughly three times the `.wpress` size.
Set `WPSC_KEEP_JOB_WORKSPACE=false` to discard intermediates automatically.

---

## Credits and licence

`.wpress` format understood by reading
[fifthsegment/Wpress-Extractor](https://github.com/fifthsegment/Wpress-Extractor)
and the [yani-/wpress](https://github.com/yani-/wpress) library it derives from
(MIT). The extraction code here is an independent Python implementation and
fixes that reader's path-traversal behaviour.

All-in-One WP Migration is a product of ServMask; this project is not affiliated
with or endorsed by them, and does not use their plugin.

##Commands

cd B:\Automation\wordpress-to-html

.\.venv\Scripts\python.exe convert.py "C:\bkp\www.aungmetals.com-20260103-084045-395.wpress" --jobs-dir "C:\wpsc-jobs" -o "C:\wpsc-jobs\exports" -c 4
