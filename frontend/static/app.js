/* wp-static-converter UI.
   Plain ES modules-free JavaScript: no build step, no dependencies. */

(function () {
  'use strict';

  var STAGES = [
    ['EXTRACTING', 'Extracting the backup'],
    ['RESTORING', 'Restoring WordPress'],
    ['STARTING_WORDPRESS', 'Starting WordPress'],
    ['DISCOVERING_URLS', 'Discovering pages'],
    ['RENDERING', 'Rendering pages'],
    ['GENERATING_HTML', 'Generating HTML'],
    ['DOWNLOADING_ASSETS', 'Collecting assets'],
    ['VALIDATING', 'Validating'],
    ['ZIPPING', 'Packaging']
  ];

  var TERMINAL = { COMPLETED: 1, FAILED: 1, CANCELLED: 1 };

  var el = function (id) { return document.getElementById(id); };
  var state = { jobId: null, lastEventId: 0, timer: null, file: null,
                source: 'local', localPath: null };

  /* ------------------------------------------------------------------ utils */
  function bytes(n) {
    if (!n && n !== 0) return '';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + ' ' + units[i];
  }

  function duration(seconds) {
    seconds = Math.max(0, Math.round(seconds || 0));
    if (seconds < 60) return seconds + 's';
    var m = Math.floor(seconds / 60), s = seconds % 60;
    if (m < 60) return m + 'm ' + String(s).padStart(2, '0') + 's';
    return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm';
  }

  function text(node, value) { node.textContent = value == null ? '' : String(value); }

  async function api(path, options) {
    var response = await fetch(path, options);
    var body = null;
    try { body = await response.json(); } catch (e) { /* not JSON */ }
    if (!response.ok) {
      throw new Error((body && body.detail) || (response.status + ' ' + response.statusText));
    }
    return body;
  }

  /* ----------------------------------------------------------------- health */
  async function checkHealth() {
    var node = el('health');
    try {
      var h = await api('/api/health');
      var missing = [];
      if (!h.php.found) missing.push('PHP');
      if (!h.mysql.found) missing.push('MySQL/MariaDB');
      if (!h.playwright.chromium_ready) missing.push('Chromium');

      if (!missing.length) {
        node.className = 'health ok';
        text(node, 'Ready · PHP ' + h.php.version + ' · ' +
                   h.mysql.flavour + ' ' + h.mysql.version + ' · Chromium');
      } else if (h.auto_provision) {
        node.className = 'health';
        text(node, 'Missing ' + missing.join(', ') + ' — will be downloaded on first run');
      } else {
        node.className = 'health bad';
        text(node, 'Missing ' + missing.join(', ') + ' — run setup.ps1');
      }
    } catch (e) {
      node.className = 'health bad';
      text(node, 'Cannot reach the server');
    }
  }

  /* --------------------------------------------------------------- dropzone */
  function initDropzone() {
    var zone = el('dropzone'), input = el('file-input');

    function choose(file) {
      if (!file) return;
      if (!/\.wpress$/i.test(file.name)) {
        showError('That is not a .wpress file. All-in-One WP Migration backups end in .wpress.');
        return;
      }
      showError(null);
      state.file = file;
      zone.classList.add('has-file');
      zone.querySelector('.dropzone-inner').hidden = true;
      el('dz-selected').hidden = false;
      text(el('dz-name'), file.name);
      text(el('dz-size'), bytes(file.size));
      refreshStartButton();
    }

    function clear() {
      state.file = null;
      input.value = '';
      zone.classList.remove('has-file');
      zone.querySelector('.dropzone-inner').hidden = false;
      el('dz-selected').hidden = true;
      refreshStartButton();
    }

    zone.addEventListener('click', function (e) {
      if (e.target.id === 'dz-clear') { e.stopPropagation(); clear(); return; }
      input.click();
    });
    zone.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
    });
    input.addEventListener('change', function () { choose(input.files[0]); });

    ['dragenter', 'dragover'].forEach(function (name) {
      zone.addEventListener(name, function (e) {
        e.preventDefault(); zone.classList.add('dragover');
      });
    });
    ['dragleave', 'drop'].forEach(function (name) {
      zone.addEventListener(name, function (e) {
        e.preventDefault(); zone.classList.remove('dragover');
      });
    });
    zone.addEventListener('drop', function (e) {
      if (e.dataTransfer.files.length) choose(e.dataTransfer.files[0]);
    });
  }

  function showError(message) {
    var node = el('form-error');
    if (!message) { node.hidden = true; return; }
    node.hidden = false;
    text(node, message);
  }

  /* ---------------------------------------------------------- local source */
  function initLocalSource() {
    el('tab-local').addEventListener('click', function () { setSource('local'); });
    el('tab-upload').addEventListener('click', function () { setSource('upload'); });
    el('refresh-local').addEventListener('click', loadLocalBackups);
    el('local-path').addEventListener('input', function () {
      var value = el('local-path').value.trim().replace(/^"|"$/g, '');
      state.localPath = value || null;
      markSelected(null);
      refreshStartButton();
    });
    setSource('local');
    loadLocalBackups();
  }

  function setSource(source) {
    state.source = source;
    var local = source === 'local';
    el('tab-local').classList.toggle('active', local);
    el('tab-upload').classList.toggle('active', !local);
    el('tab-local').setAttribute('aria-selected', local ? 'true' : 'false');
    el('tab-upload').setAttribute('aria-selected', local ? 'false' : 'true');
    el('local-source').hidden = !local;
    el('dropzone').hidden = local;
    showError(null);
    refreshStartButton();
  }

  function markSelected(path) {
    el('local-list').querySelectorAll('.backup-item').forEach(function (item) {
      item.classList.toggle('selected', item.dataset.path === path);
    });
  }

  async function loadLocalBackups() {
    var list = el('local-list');
    list.innerHTML = '<p class="muted">Looking for backups…</p>';
    try {
      var data = await api('/api/local-backups');
      if (!data.allowed) {
        // The server is reachable from the network, so reading its disk on a
        // visitor's behalf is switched off. Uploading is the only option.
        el('tab-local').hidden = true;
        setSource('upload');
        return;
      }
      text(el('local-folders'), data.folders.length
        ? 'Searched: ' + data.folders.join(' · ') : '');
      if (!data.files.length) {
        list.innerHTML = '<p class="muted">No .wpress files found in those folders. ' +
          'Paste a full path below.</p>';
        return;
      }
      list.innerHTML = '';
      data.files.forEach(function (file) {
        var item = document.createElement('button');
        item.type = 'button';
        item.className = 'backup-item';
        item.dataset.path = file.path;
        var name = document.createElement('span');
        name.className = 'name';
        name.textContent = file.name;
        var meta = document.createElement('span');
        meta.className = 'meta';
        meta.textContent = bytes(file.bytes) + ' · ' + file.folder;
        item.append(name, meta);
        item.addEventListener('click', function () {
          state.localPath = file.path;
          el('local-path').value = file.path;
          markSelected(file.path);
          refreshStartButton();
        });
        list.appendChild(item);
      });
    } catch (e) {
      var message = document.createElement('p');
      message.className = 'muted';
      message.textContent = 'Could not list backups: ' + e.message;
      list.replaceChildren(message);
    }
  }

  /* --------------------------------------------------------------- options */
  function collectOptions() {
    // The screen exposes no settings, so this returns {} and the server
    // applies its defaults (app/config.py). Any field added back to the form
    // is picked up here automatically.
    var options = {};
    el('job-form').querySelectorAll('input[name], select[name]').forEach(function (field) {
      if (field.type === 'checkbox') {
        options[field.name] = field.checked;
      } else if (field.tagName === 'SELECT') {
        var value = field.value;
        options[field.name] = /^\d+$/.test(value) ? parseInt(value, 10) : value;
      }
    });
    return options;
  }

  /* ---------------------------------------------------------------- submit */
  function canStart() {
    return state.source === 'local' ? !!state.localPath : !!state.file;
  }

  function refreshStartButton() {
    el('start-btn').disabled = !canStart() || !!state.timer;
  }

  async function submitJob(event) {
    event.preventDefault();
    if (!canStart()) return;

    var button = el('start-btn');
    button.disabled = true;
    showError(null);

    var created;
    try {
      if (state.source === 'local') {
        // The file is read where it sits on this computer: nothing is sent.
        text(button, 'Starting…');
        created = await api('/api/jobs/local', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: state.localPath, options: collectOptions() })
        });
      } else {
        text(button, 'Uploading…');
        var form = new FormData();
        form.append('file', state.file);
        form.append('options', JSON.stringify(collectOptions()));
        created = await api('/api/jobs', { method: 'POST', body: form });
      }
      text(button, 'Start conversion');
      startWatching(created.id);
      loadHistory();
    } catch (e) {
      showError(e.message);
      button.disabled = false;
      text(button, 'Start conversion');
    }
  }

  /* --------------------------------------------------------------- watching */
  function startWatching(jobId) {
    state.jobId = jobId;
    state.lastEventId = 0;
    el('progress-panel').hidden = false;
    el('result').hidden = true;
    el('log').innerHTML = '';
    el('cancel-btn').hidden = false;
    renderStages(null);
    el('progress-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    poll();
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(poll, 1500);
  }

  async function poll() {
    if (!state.jobId) return;
    try {
      var job = await api('/api/jobs/' + state.jobId);
      updateProgress(job);
      await pollLogs();

      if (TERMINAL[job.status]) {
        clearInterval(state.timer);
        state.timer = null;
        el('cancel-btn').hidden = true;
        await pollLogs();
        showResult(job);
        loadHistory();
        refreshStartButton();
      }
    } catch (e) {
      /* A transient poll failure is not worth surfacing; the next tick retries. */
    }
  }

  function updateProgress(job) {
    var pct = Math.round((job.overall_progress || 0) * 100);
    el('overall-fill').style.width = pct + '%';
    text(el('overall-pct'), pct + '%');
    text(el('stage-detail'), job.stage_detail || '');
    updateTiming(job);

    var pill = el('job-status');
    text(pill, job.status);
    pill.className = 'status-pill ' + (TERMINAL[job.status] ? job.status.toLowerCase() : 'running');

    renderStages(job);
    renderCounters(job);
  }

  function updateTiming(job) {
    var terminal = TERMINAL[job.status];
    var stage = STAGES.find(function (s) { return s[0] === job.status; });
    var position = STAGES.findIndex(function (s) { return s[0] === job.status; });

    text(el('stage-name'), terminal ? job.status.charAt(0) + job.status.slice(1).toLowerCase()
      : stage ? (position + 1) + '/' + STAGES.length + ' ' + stage[1] : job.status);

    // Estimates come from the server, measured from the rate actually being
    // made. Until there is enough progress to measure, say so honestly rather
    // than showing a guess.
    text(el('stage-eta'), terminal ? '—'
      : job.stage_eta_seconds == null ? 'estimating…' : '~' + duration(job.stage_eta_seconds));
    text(el('job-eta'), terminal ? '—'
      : job.eta_seconds == null ? 'estimating…' : '~' + duration(job.eta_seconds));
    text(el('elapsed'), duration(job.run_elapsed_seconds || job.duration_seconds));
  }

  function renderStages(job) {
    var list = el('stages');
    var currentIndex = job ? STAGES.findIndex(function (s) { return s[0] === job.status; }) : -1;
    var failed = job && (job.status === 'FAILED' || job.status === 'CANCELLED');
    var completed = job && job.status === 'COMPLETED';

    list.innerHTML = '';
    STAGES.forEach(function (stage, index) {
      var li = document.createElement('li');
      var done = completed || (currentIndex > -1 && index < currentIndex);
      var active = currentIndex === index;

      var mark = '○';
      if (done) { li.className = 'done'; mark = '✓'; }
      else if (active) { li.className = failed ? 'failed' : 'active'; mark = failed ? '✗' : '●'; }

      li.innerHTML = '<span class="mark">' + mark + '</span><span>' + stage[1] + '</span>' +
        '<span class="pct">' + (active && !failed
          ? Math.round((job.stage_progress || 0) * 100) + '%'
          : (done ? '100%' : '')) + '</span>';
      list.appendChild(li);
    });
  }

  function renderCounters(job) {
    var counts = job.url_counts || {};
    var summary = job.summary || {};
    var items = [];

    var rendered = (counts.RENDERED || 0) + (counts.WRITTEN || 0);
    var total = Object.keys(counts).reduce(function (sum, k) { return sum + counts[k]; }, 0);
    if (total) items.push(['Pages', rendered + ' / ' + total, '']);
    if (counts.FAILED) items.push(['Failed pages', counts.FAILED, 'bad']);
    if (summary.assets) items.push(['Assets', summary.assets, '']);
    if (summary.broken_links != null && TERMINAL[job.status]) {
      items.push(['Broken links', summary.broken_links, summary.broken_links ? 'bad' : 'good']);
    }
    if (summary.missing_assets != null && TERMINAL[job.status]) {
      items.push(['Missing assets', summary.missing_assets, summary.missing_assets ? 'bad' : 'good']);
    }

    var node = el('counters');
    if (!items.length) { node.hidden = true; return; }
    node.hidden = false;
    node.innerHTML = items.map(function (item) {
      return '<div class="counter ' + item[2] + '"><div class="n">' + item[1] +
             '</div><div class="l">' + item[0] + '</div></div>';
    }).join('');
  }

  async function pollLogs() {
    var data = await api('/api/jobs/' + state.jobId + '/logs?after=' + state.lastEventId);
    if (!data.events.length) return;
    state.lastEventId = data.last_id;

    var log = el('log');
    var atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;

    data.events.forEach(function (event) {
      var line = document.createElement('div');
      line.className = 'log-line ' + (event.level === 'WARN' ? 'warn' :
                                      event.level === 'ERROR' ? 'error' : '');
      // Show the viewer's local clock; the server's "time" field is UTC.
      var time = document.createElement('span'); time.className = 't';
      time.textContent = event.ts
        ? new Date(event.ts * 1000).toLocaleTimeString([], { hour12: false })
        : event.time;
      var stage = document.createElement('span'); stage.className = 's'; stage.textContent = event.stage;
      var message = document.createElement('span'); message.className = 'm'; message.textContent = event.message;
      line.append(time, stage, message);
      log.appendChild(line);
    });

    // A long conversion logs thousands of lines. Keeping them all makes the
    // browser tab steadily heavier on the very machine doing the conversion;
    // the full log is on disk and behind "View log" either way.
    var extra = log.childElementCount - 400;
    for (var i = 0; i < extra; i++) log.removeChild(log.firstChild);

    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  /* ---------------------------------------------------------------- result */
  function showResult(job) {
    var node = el('result');
    node.hidden = false;

    var summary = job.summary || {};
    var ok = job.status === 'COMPLETED';

    var problems = summary.problems || 0;
    text(el('result-title'), ok
      ? (problems ? 'Completed with ' + problems + ' problem' + (problems === 1 ? '' : 's') +
                    ' - see the report' : 'Conversion complete')
      : job.status === 'CANCELLED' ? 'Conversion cancelled' : 'Conversion failed');

    if (ok) {
      var parts = [
        summary.pages + ' pages',
        summary.assets + ' assets',
        bytes(summary.zip_bytes) + ' ZIP',
        'took ' + duration(job.duration_seconds)
      ];
      if (summary.repaired) parts.push(summary.repaired + ' missing file(s) repaired automatically');
      if (summary.source_issues) parts.push(summary.source_issues + ' issue(s) in the original site');
      if (!problems && summary.broken_links === 0 && summary.missing_assets === 0) {
        parts.push('no broken links or missing assets');
      }
      text(el('result-summary'), parts.join(' · '));
    } else {
      text(el('result-summary'), job.error || '');
    }

    var base = '/api/jobs/' + job.id;
    el('download-link').href = base + '/download';
    el('download-link').style.display = (job.artifacts && job.artifacts.zip) ? '' : 'none';
    el('report-link').href = base + '/report';
    el('report-link').style.display = (job.artifacts && job.artifacts.report) ? '' : 'none';
    el('log-link').href = base + '/log';

    renderLimitations(summary.dynamic_features || []);
  }

  function renderLimitations(features) {
    var node = el('limitations');
    if (!features.length) { node.hidden = true; return; }
    node.hidden = false;
    node.innerHTML = '<h4>Needs a server &mdash; preserved visually, but will not function</h4>' +
      features.map(function (name) {
        var div = document.createElement('div');
        div.className = 'limitation';
        div.textContent = name;
        return div.outerHTML;
      }).join('');
  }

  /* --------------------------------------------------------------- history */
  async function loadHistory() {
    var node = el('history');
    try {
      var data = await api('/api/jobs?limit=15');
      if (!data.jobs.length) {
        node.innerHTML = '<p class="muted">Nothing yet.</p>';
        return;
      }
      node.innerHTML = '';
      data.jobs.forEach(function (job) {
        var row = document.createElement('div');
        row.className = 'job-row';

        var main = document.createElement('div');
        main.className = 'job-main';
        var name = document.createElement('div');
        name.className = 'job-name';
        name.textContent = job.filename;
        var meta = document.createElement('div');
        meta.className = 'job-meta';
        var problems = job.status === 'COMPLETED' && job.summary ? job.summary.problems : 0;
        var rest = ' · ' + new Date(job.created_at * 1000).toLocaleString() + ' · ' +
          bytes(job.input_bytes) +
          (job.summary && job.summary.pages ? ' · ' + job.summary.pages + ' pages' : '');
        if (problems) {
          var flag = document.createElement('span');
          flag.className = 'flag';
          flag.textContent = 'COMPLETED WITH ' + problems +
            ' PROBLEM' + (problems === 1 ? '' : 'S');
          meta.append(flag, document.createTextNode(rest));
        } else {
          meta.textContent = job.status + rest;
        }
        main.append(name, meta);

        var actions = document.createElement('div');
        actions.className = 'job-actions';

        if (job.status === 'COMPLETED') {
          actions.appendChild(anchor('Download', '/api/jobs/' + job.id + '/download', true));
          actions.appendChild(anchor('Report', '/api/jobs/' + job.id + '/report', false));
        }
        if (!TERMINAL[job.status]) {
          actions.appendChild(button('Watch', function () { startWatching(job.id); }));
        }
        if (job.status === 'FAILED' || job.status === 'CANCELLED') {
          actions.appendChild(button('Resume', function () { resumeJob(job.id, job.filename); }));
        }
        actions.appendChild(button('Delete', function () { removeJob(job.id, job.filename); }));

        row.append(main, actions);
        node.appendChild(row);
      });
    } catch (e) {
      node.innerHTML = '<p class="muted">Could not load history.</p>';
    }
  }

  function anchor(label, href, download) {
    var a = document.createElement('a');
    a.className = 'ghost small';
    a.href = href;
    a.textContent = label;
    if (download) { a.setAttribute('download', ''); } else { a.target = '_blank'; a.rel = 'noopener'; }
    return a;
  }

  function button(label, handler) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'ghost small';
    b.textContent = label;
    b.addEventListener('click', handler);
    return b;
  }

  async function removeJob(jobId, filename) {
    if (!confirm('Delete the conversion of "' + filename + '"?\n\n' +
                 'Its downloaded ZIP, report and logs are removed permanently.')) return;
    try {
      await api('/api/jobs/' + jobId, { method: 'DELETE' });
      if (state.jobId === jobId) {
        if (state.timer) clearInterval(state.timer);
        state.jobId = null;
        el('progress-panel').hidden = true;
      }
      loadHistory();
    } catch (e) {
      alert('Could not delete the job: ' + e.message);
    }
  }

  async function resumeJob(jobId, filename) {
    if (!confirm('Resume "' + filename + '"?\n\n' +
                 'It continues from the restored WordPress: no re-import, and pages ' +
                 'already rendered are reused. The job keeps the settings it started with.')) return;
    try {
      await api('/api/jobs/' + jobId + '/resume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
      });
      startWatching(jobId);
      loadHistory();
    } catch (e) {
      alert('Could not resume: ' + e.message);
    }
  }

  async function cancelJob() {
    if (!state.jobId) return;
    if (!confirm('Cancel this conversion?')) return;

    // Say so immediately. Stopping the database, the PHP workers and the
    // browser takes a few seconds, and a button that looks inert in the
    // meantime reads as broken.
    var button = el('cancel-btn');
    button.disabled = true;
    text(button, 'Cancelling…');
    var pill = el('job-status');
    pill.className = 'status-pill cancelling';
    text(pill, 'CANCELLING');

    try {
      await api('/api/jobs/' + state.jobId + '/cancel', { method: 'POST' });
    } catch (e) {
      button.disabled = false;
      text(button, 'Cancel');
      alert('Could not cancel: ' + e.message);
    }
  }

  /* ------------------------------------------------------------------ init */
  document.addEventListener('DOMContentLoaded', function () {
    initDropzone();
    initLocalSource();
    el('job-form').addEventListener('submit', submitJob);
    el('cancel-btn').addEventListener('click', cancelJob);
    el('refresh-history').addEventListener('click', loadHistory);
    checkHealth();
    loadHistory();
    setInterval(checkHealth, 60000);
  });
})();
