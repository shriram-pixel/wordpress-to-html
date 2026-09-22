/* Time calculator: how long a batch takes here, or on a machine being
   considered. The arithmetic deliberately lives on the server, in the same
   module run_batch.py uses to size a real run -- a planner that disagrees
   with the run it is planning is worse than no planner. This file only
   collects the form, asks, and draws the answer. */

(function () {
  'use strict';

  var el = function (id) { return document.getElementById(id); };
  var dialog = el('calc');
  if (!dialog) return;

  var here = null;        // this machine, as the server measured it
  var baseline = null;    // the conversion every estimate is extrapolated from
  var pending = null;     // debounce timer
  var edited = false;     // has the machine been described by hand?

  var MACHINE_FIELDS = ['c-cpus', 'c-memory', 'c-disk'];

  var FIELDS = ['c-sites', 'c-pages', 'c-backup', 'c-shots', 'c-cpus', 'c-memory',
                'c-disk', 'c-speed', 'c-parallel', 'c-perjob'];

  /* ------------------------------------------------------------------ utils */
  function num(id) {
    var value = parseFloat(el(id).value);
    return isFinite(value) ? value : 0;
  }

  function duration(minutes) {
    if (!minutes || minutes < 1) return 'under a minute';
    if (minutes < 90) return Math.round(minutes) + ' min';
    var hours = Math.floor(minutes / 60), rest = Math.round(minutes % 60);
    return hours + ' h' + (rest ? ' ' + rest + ' min' : '');
  }

  function text(value) {
    return String(value).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function row(label, value, klass) {
    return '<tr' + (klass ? ' class="' + klass + '"' : '') + '><th>' + text(label) +
           '</th><td>' + text(value) + '</td></tr>';
  }

  /* ------------------------------------------------------- this machine */
  function fillMachine(machine) {
    el('c-cpus').value = machine.cpus;
    el('c-memory').value = Math.round(machine.memory_gb);
    el('c-disk').value = Math.round(machine.free_disk_gb);
    el('c-speed').value = '1';
  }

  function describeMachine(machine) {
    return 'This machine: ' + machine.cpus + ' CPU core(s), ' +
           Math.round(machine.memory_gb) + ' GB memory, ' +
           Math.round(machine.free_disk_gb) + ' GB free where jobs are written. ' +
           'Change any figure below to plan for a different one.';
  }

  /* ------------------------------------------------------------- drawing */
  function draw(data) {
    var e = data.estimate, m = data.machine, out = [];

    out.push('<div class="calc-headline"><span class="big">' +
             text(duration(e.total_minutes)) + '</span><span class="sub">' +
             text(num('c-sites') + ' site(s), ' + e.parallel + ' at a time, ' +
                  duration(e.per_site_minutes) + ' each') +
             '</span></div>');

    out.push('<table class="calc-table"><tbody>');
    out.push(row('Conversions at once', e.parallel + '  (' + e.limit + ')'));
    out.push(row('Pages each renders at once', e.pages_per_job));
    out.push(row('Pages rendering in total', e.pages_in_flight));
    out.push(row('Rendering, per site', duration(e.render_minutes)));
    out.push(row('Everything else, per site', duration(e.fixed_minutes)));
    out.push(row('One site, start to finish', duration(e.per_site_minutes)));
    out.push(row('Rounds of ' + e.parallel, e.waves));
    out.push(row('Disk needed while running', Math.round(e.disk_needed_gb) + ' GB of ' +
                 Math.round(e.disk_free_gb) + ' GB free'));
    out.push(row('Whole batch', duration(e.total_minutes), 'total'));
    out.push('</tbody></table>');

    (e.warnings || []).forEach(function (warning) {
      out.push('<p class="calc-note">' + text(warning) + '</p>');
    });

    if (!m.measured) {
      out.push('<p class="calc-note">These are figures you typed, not this ' +
               'machine. Nothing here has been measured on it.</p>');
    }

    if (data.comparison && data.comparison.length) {
      out.push(comparison(data.comparison, num('c-sites')));
    }

    el('calc-result').innerHTML = out.join('');

    if (baseline) {
      el('calc-basis').innerHTML =
        'Extrapolated from one measured conversion: ' + text(baseline.pages) +
        ' pages, ' + text(baseline.backup_gb) + ' GB, ' +
        text(duration(baseline.measured_minutes)) + ' on ' + text(baseline.reference) +
        '. That gives ' + text(baseline.seconds_per_page) + ' s of rendering work per ' +
        'page and ' + text(baseline.fixed_minutes) + ' min per site for everything ' +
        'else. Sites differ, and no run has yet been measured on Linux, so treat ' +
        'this as a plan rather than a promise.';
    }
  }

  /* -------------------------------------------------------------- asking */
  function calculate() {
    var body = {
      sites: Math.max(1, Math.round(num('c-sites'))),
      pages_per_site: Math.max(1, Math.round(num('c-pages'))),
      backup_gb: Math.max(0.1, num('c-backup')),
      cpu_speed: parseFloat(el('c-speed').value) || 1,
      parallel: Math.max(0, Math.round(num('c-parallel'))),
      pages_at_once: Math.max(0, Math.round(num('c-perjob'))),
      screenshots: el('c-shots').checked
    };

    // The machine fields open pre-filled from this machine. Sending them back
    // unchanged would make the server treat its own measurements as guesses,
    // so they travel only once the user has actually edited one.
    if (edited || !here) {
      body.cpus = Math.max(1, Math.round(num('c-cpus')));
      body.memory_gb = Math.max(1, num('c-memory'));
      body.free_disk_gb = Math.max(0, num('c-disk'));
    }

    fetch('/api/estimate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    }).then(draw).catch(function (error) {
      el('calc-result').innerHTML =
        '<p class="calc-note">Could not work that out: ' + text(error.message) + '</p>';
    });
  }

  function schedule() {
    clearTimeout(pending);
    pending = setTimeout(calculate, 180);   // typing a number fires per keystroke
  }

  function comparison(rows, sites) {
    var fastest = Math.min.apply(null, rows.map(function (r) { return r.total_minutes; }));
    var body = rows.map(function (r) {
      var best = r.total_minutes === fastest ? ' class="best"' : '';
      return '<tr' + (r.measured ? ' class="mine"' : best) + '>' +
        '<th>' + text(r.label) + '</th>' +
        '<td>' + text(r.parallel) + ' × ' + text(r.pages_per_job) + '</td>' +
        '<td>' + text(duration(r.per_site_minutes)) + '</td>' +
        '<td>' + text(duration(r.total_minutes)) + '</td>' +
        '</tr>';
    }).join('');

    return '<h3 class="calc-sub">On a bigger machine</h3>' +
      '<table class="calc-table compare"><thead><tr>' +
      '<th>Server</th><th>At once</th><th>Per site</th>' +
      '<th>' + text(sites) + ' site' + (sites === 1 ? '' : 's') + '</th>' +
      '</tr></thead><tbody>' + body + '</tbody></table>' +
      '<p class="calc-hint">&ldquo;At once&rdquo; is conversions × pages each. ' +
      'This machine is always timed at its own measured speed; the servers use ' +
      'the core speed chosen on the left.</p>';
  }

  /* -------------------------------------------------- pages from a backup */
  function loadBackups() {
    fetch('/api/local-backups').then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.allowed || !data.files || !data.files.length) return;
        var picker = el('c-backup-pick');
        data.files.forEach(function (file) {
          var option = document.createElement('option');
          option.value = file.path;
          option.textContent = file.name;
          picker.appendChild(option);
        });
      }).catch(function () { /* the picker is a convenience, not a requirement */ });
  }

  function readBackup(path) {
    var note = el('c-backup-note');
    note.className = 'hint working';
    note.textContent = 'Reading the backup… a large one takes a few seconds.';

    fetch('/api/estimate/pages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: path })
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
        return data;
      });
    }).then(function (data) {
      el('c-pages').value = data.pages;
      el('c-backup').value = data.size_gb;
      edited = false;                     // the machine was not touched
      note.className = 'hint';
      note.innerHTML = text(data.site_url || data.name) + ': about <strong>' +
        text(data.pages) + '</strong> pages, ' + text(data.size_gb) + ' GB' +
        (data.theme ? ', theme ' + text(data.theme) : '') +
        (data.plugins ? ', ' + text(data.plugins) + ' plugins' : '') +
        '. Counted from the backup’s database, so archive and paged URLs ' +
        'are not included — the real crawl usually finds a little more.';
      if (!data.complete && data.note) {
        note.className = 'hint bad';
        note.textContent = 'Could not count pages: ' + data.note;
      }
      calculate();
    }).catch(function (error) {
      note.className = 'hint bad';
      note.textContent = 'Could not read that backup: ' + error.message;
    });
  }

  /* --------------------------------------------------------------- wiring */
  function open() {
    var show = function () {
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', 'open');
      calculate();
    };

    if (here) { show(); return; }

    fetch('/api/estimate/machine').then(function (response) {
      return response.json();
    }).then(function (data) {
      here = data.machine;
      baseline = data.baseline;
      el('calc-machine').textContent = describeMachine(here);
      fillMachine(here);
      // Pages and size default to the measured conversion, which is a more
      // useful starting point than an empty form.
      el('c-pages').value = baseline.pages;
      el('c-backup').value = baseline.backup_gb;
      loadBackups();
      show();
    }).catch(function () {
      el('calc-machine').textContent =
        'Could not read this machine; type its details below.';
      show();
    });
  }

  el('calc-open').addEventListener('click', open);
  el('c-backup-pick').addEventListener('change', function () {
    if (this.value) readBackup(this.value);
  });
  el('c-reset').addEventListener('click', function () {
    if (here) { edited = false; fillMachine(here); calculate(); }
  });
  MACHINE_FIELDS.forEach(function (id) {
    el(id).addEventListener('input', function () { edited = true; });
  });
  FIELDS.forEach(function (id) {
    var field = el(id);
    if (field) field.addEventListener('input', schedule);
  });
  dialog.addEventListener('click', function (event) {
    // A click on the backdrop lands on the dialog itself, never on its content.
    if (event.target === dialog) dialog.close();
  });
})();
