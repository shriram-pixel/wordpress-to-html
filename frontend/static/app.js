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
                source: 'local', localPath: null,
                // Paths ticked in the list, in the order they were ticked, and
                // the jobs they became. One selection is the old behaviour;
                // several become a batch that the server queues.
                selected: [], batch: null, batchTimer: null };

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
      // Typing a path is a choice of one file, so it replaces any ticks.
      state.selected = value ? [value] : [];
      markSelected();
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

  /* ------------------------------------------------------------- selection */
  function isSelected(path) { return state.selected.indexOf(path) !== -1; }

  function toggleSelected(path) {
    var at = state.selected.indexOf(path);
    if (at === -1) state.selected.push(path);
    else state.selected.splice(at, 1);
    // The single-file path, the pasted-path box and the upload tab all still
    // work off localPath; a selection of one keeps them in step.
    state.localPath = state.selected.length === 1 ? state.selected[0] : null;
    if (state.selected.length === 1) el('local-path').value = state.selected[0];
    markSelected();
    refreshStartButton();
  }

  function markSelected() {
    var items = el('local-list').querySelectorAll('.backup-item');
    items.forEach(function (item) {
      var on = isSelected(item.dataset.path);
      item.classList.toggle('selected', on);
      item.setAttribute('aria-pressed', on ? 'true' : 'false');
      var tick = item.querySelector('.tick');
      if (tick) tick.checked = on;
    });

    var all = el('select-all');
    if (all) {
      all.checked = items.length > 0 && state.selected.length === items.length;
      all.indeterminate = state.selected.length > 0 && !all.checked;
    }
    var label = el('select-all-label');
    if (label) {
      label.textContent = state.selected.length
        ? state.selected.length + ' selected'
        : 'Select all';
    }
  }

  function selectAll(on) {
    state.selected = on
      ? Array.prototype.map.call(
          el('local-list').querySelectorAll('.backup-item'),
          function (item) { return item.dataset.path; })
      : [];
    state.localPath = state.selected.length === 1 ? state.selected[0] : null;
    markSelected();
    refreshStartButton();
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
        item.setAttribute('aria-pressed', 'false');
        var tick = document.createElement('input');
        tick.type = 'checkbox';
        tick.className = 'tick';
        tick.tabIndex = -1;            // the row itself is the control
        tick.setAttribute('aria-hidden', 'true');
        var name = document.createElement('span');
        name.className = 'name';
        name.textContent = file.name;
        var meta = document.createElement('span');
        meta.className = 'meta';
        meta.textContent = bytes(file.bytes) + ' · ' + file.folder;
        item.append(tick, name, meta);
        item.addEventListener('click', function () { toggleSelected(file.path); });
        list.appendChild(item);
      });
      el('select-all-wrap').hidden = data.files.length < 2;
      markSelected();
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
    if (state.source !== 'local') return !!state.file;
    return state.selected.length > 0 || !!state.localPath;
  }

  function refreshStartButton() {
    var button = el('start-btn');
    button.disabled = !canStart() || !!state.timer || !!state.batchTimer;
    var many = state.source === 'local' && state.selected.length > 1;
    text(button, many ? 'Convert ' + state.selected.length + ' backups'
                      : 'Start conversion');
  }

  async function submitJob(event) {
    event.preventDefault();
    if (!canStart()) return;

    var button = el('start-btn');
    button.disabled = true;
    showError(null);

    if (state.source === 'local' && state.selected.length > 1) {
      return startBatch(state.selected.slice(), button);
    }

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

  /* ------------------------------------------------------------------ batch */
  var FINISHED = { COMPLETED: 1, FAILED: 1, CANCELLED: 1 };

  async function startBatch(paths, button) {
    text(button, 'Queueing…');
    el('queue-panel').hidden = false;
    state.batch = { jobs: [], failed: [] };

    // Submitted one at a time and in order, so the queue reads the way the
    // list did, and so one rejected path (deleted, unreadable, no disk) is
    // that one backup's problem rather than the batch's.
    for (var i = 0; i < paths.length; i++) {
      var path = paths[i];
      var name = path.split(/[\\/]/).pop();
      try {
        var created = await api('/api/jobs/local', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: path, options: collectOptions() })
        });
        state.batch.jobs.push({ id: created.id, name: name, status: 'QUEUED', pct: 0 });
      } catch (e) {
        state.batch.failed.push({ name: name, why: e.message });
      }
      renderQueue();
    }

    text(button, 'Start conversion');
    if (!state.batch.jobs.length) {
      showError('None of the selected backups could be queued.');
      refreshStartButton();
      return;
    }

    selectAll(false);
    loadHistory();
    el('queue-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    pollBatch();
    state.batchTimer = setInterval(pollBatch, 2000);
    refreshStartButton();
  }

  async function pollBatch() {
    if (!state.batch) return;
    var mine = {};
    state.batch.jobs.forEach(function (job) { mine[job.id] = job; });

    try {
      var data = await api('/api/jobs?limit=100');
      data.jobs.forEach(function (record) {
        var job = mine[record.id];
        if (!job) return;
        job.status = record.status;
        job.pct = Math.round((record.overall_progress || 0) * 100);
        job.stage = record.stage_detail || '';
        job.pages = record.summary && record.summary.pages;
        job.problems = (record.summary && record.summary.problems) || 0;
      });
    } catch (e) {
      return;                     // a missed poll is not worth reporting
    }

    renderQueue();
    var done = state.batch.jobs.every(function (job) { return FINISHED[job.status]; });
    if (done) {
      clearInterval(state.batchTimer);
      state.batchTimer = null;
      refreshStartButton();
      loadHistory();
    }
  }

  function renderQueue() {
    if (!state.batch) return;
    var jobs = state.batch.jobs, list = el('queue');
    list.innerHTML = '';

    jobs.forEach(function (job, index) {
      var row = document.createElement('li');
      var running = !FINISHED[job.status];
      row.className = job.status === 'COMPLETED' ? (job.problems ? 'warn' : 'done')
                    : job.status === 'FAILED' ? 'bad'
                    : job.status === 'CANCELLED' ? 'bad'
                    : running && job.status !== 'QUEUED' ? 'running' : '';

      var n = document.createElement('span');
      n.className = 'n';
      n.textContent = (index + 1) + '.';

      var who = document.createElement('span');
      who.className = 'who';
      var nm = document.createElement('span');
      nm.className = 'nm';
      nm.textContent = job.name;
      var st = document.createElement('span');
      st.className = 'st';
      st.textContent = job.status === 'COMPLETED'
        ? (job.pages ? job.pages + ' pages' : 'finished') +
          (job.problems ? ' · ' + job.problems + ' to look at' : '')
        : (job.stage || job.status.toLowerCase().replace(/_/g, ' '));
      who.append(nm, st);

      if (running && job.status !== 'QUEUED') {
        var bar = document.createElement('span');
        bar.className = 'bar';
        var fill = document.createElement('span');
        fill.style.width = job.pct + '%';
        bar.appendChild(fill);
        who.appendChild(bar);
      }

      var right = document.createElement('span');
      if (job.status === 'COMPLETED') {
        right.className = 'links';
        var zip = document.createElement('a');
        zip.href = '/api/' + job.id + '/download';
        zip.textContent = 'Download';
        var report = document.createElement('a');
        report.href = '/api/' + job.id + '/report';
        report.target = '_blank';
        report.rel = 'noopener';
        report.textContent = 'Report';
        right.append(zip, report);
      } else {
        right.className = 'pill';
        right.textContent = job.status === 'QUEUED' ? 'waiting' : job.status.replace(/_/g, ' ');
      }

      row.append(n, who, right);
      list.appendChild(row);
    });

    state.batch.failed.forEach(function (bad) {
      var row = document.createElement('li');
      row.className = 'bad';
      row.innerHTML = '<span class="n">!</span><span class="who"><span class="nm"></span>' +
                      '<span class="st"></span></span><span class="pill">rejected</span>';
      row.querySelector('.nm').textContent = bad.name;
      row.querySelector('.st').textContent = bad.why;
      list.appendChild(row);
    });

    var finished = jobs.filter(function (j) { return FINISHED[j.status]; }).length;
    text(el('queue-summary'), finished + ' of ' + jobs.length + ' finished');
    el('queue-cancel').hidden = finished === jobs.length;
    text(el('queue-note'), finished === jobs.length
      ? 'All done. Each ZIP is on its row, and the reports say what needs a look.'
      : 'Conversions run a few at a time; the rest wait their turn. Closing this ' +
        'page does not stop them.');
  }

  async function cancelBatch() {
    if (!state.batch) return;
    el('queue-cancel').disabled = true;
    for (var i = 0; i < state.batch.jobs.length; i++) {
      var job = state.batch.jobs[i];
      if (FINISHED[job.status]) continue;
      try {
        await api('/api/jobs/' + job.id + '/cancel', { method: 'POST' });
      } catch (e) { /* already finished between poll and click */ }
    }
    el('queue-cancel').disabled = false;
    pollBatch();
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
    el('select-all').addEventListener('change', function () { selectAll(this.checked); });
    el('queue-cancel').addEventListener('click', cancelBatch);
    el('refresh-history').addEventListener('click', loadHistory);
    checkHealth();
    loadHistory();
    setInterval(checkHealth, 60000);
  });
})();
