/*
 * deployment.js — Fase 2: halaman Deployment (T-CONT Profile & Traffic Profile), read-only.
 *
 * Mandiri: membangun DOM sendiri di dalam <div id="page-deployment">.
 * API publik:
 *   Deployment.open('tcont' | 'traffic')   tampilkan & muat halaman
 *   Deployment.reload()                    muat ulang dari cache server
 *   Deployment.fetchProfiles(oltId, kind)  untuk dropdown wizard (Fase 4)
 *
 * Ada 2 fungsi adapter di bawah (dpApi, dpGetOltId) yang HARUS disamakan
 * dengan cara app.js / vlanif.js memanggil API dan memilih OLT.
 */
(function () {
  'use strict';

  // ===================== ADAPTER (PATCHED) =====================
  async function dpApi(path, opts) {
    opts = opts || {};
    // Stringify body kalau masih object
    if (opts.body && typeof opts.body === 'object') {
      opts = Object.assign({}, opts, { body: JSON.stringify(opts.body) });
    }
    return await api(path, opts);
  }

  // OLT yang sedang dipilih di aplikasi. Ganti dengan variabel/fungsi milik app.js.
  function dpGetOltId() {
    if (typeof CURRENT_OLT_ID !== 'undefined' && CURRENT_OLT_ID) return CURRENT_OLT_ID;
    if (window.currentOltId) return window.currentOltId;
    if (window.currentOLT && window.currentOLT.id) return window.currentOLT.id;
    return localStorage.getItem('olt_id') || null;
  }

  // Tombol tulis ditampilkan? Server tetap memaksa privilege 15 (403 kalau kurang).
  function dpCanWrite() { return true; }
  // ================================================================

  const KINDS = {
    tcont: {
      title: 'T-CONT Profile',
      cols: [
        { key: 'name', label: 'Nama' },
        { key: 'type', label: 'Type', fmt: fmtType },
        { key: 'fixed', label: 'Fixed', fmt: bwOrDash, num: true },
        { key: 'assured', label: 'Assured', fmt: bwOrDash, num: true },
        { key: 'maximum', label: 'Maximum', fmt: bwOrDash, num: true },
        { key: 'priority', label: 'Priority', fmt: plainOrDash, num: true },
      ],
    },
    traffic: {
      title: 'Traffic Profile',
      cols: [
        { key: 'name', label: 'Nama' },
        { key: 'sir', label: 'SIR', fmt: bwAlways, num: true },
        { key: 'pir', label: 'PIR', fmt: bwAlways, num: true },
        { key: 'cbs', label: 'CBS', fmt: burst, num: true },
        { key: 'pbs', label: 'PBS', fmt: burst, num: true },
      ],
    },
    package: { title: 'Service Package', pkg: true },
  };

  const state = { kind: 'tcont', busy: false, seq: 0 };
  function isPkg() { return !!(KINDS[state.kind] && KINDS[state.kind].pkg); }

  // ---------------------------------------------------------- format nilai
  function fmtKbps(n) {
    if (n >= 1000000) return trim(n / 1000000) + ' Gbps';
    if (n >= 1000) return trim(n / 1000) + ' Mbps';
    return n + ' kbps';
  }
  function trim(x) { return String(Math.round(x * 100) / 100); }
  function bwOrDash(v) { return (typeof v === 'number' && v > 0) ? fmtKbps(v) : '—'; }
  function bwAlways(v) { return typeof v === 'number' ? fmtKbps(v) : '—'; }
  function plainOrDash(v) { return (v === null || v === undefined) ? '—' : String(v); }
  function burst(v) {
    if (v === 'default') return 'default';
    return typeof v === 'number' ? v + ' KB' : '—';
  }
  function fmtType(v) {
    const names = { 1: 'Fixed', 2: 'Assured', 3: 'Assured + Max', 4: 'Best-effort', 5: 'Mixed' };
    return v + ' · ' + (names[v] || '?');
  }

  // --------------------------------------------------------------- DOM util
  function h(tag, attrs) {
    const el = document.createElement(tag);
    for (const k in (attrs || {})) {
      if (k === 'class') el.className = attrs[k];
      else if (k === 'text') el.textContent = attrs[k];
      else if (k.indexOf('on') === 0) el.addEventListener(k.slice(2), attrs[k]);
      else el.setAttribute(k, attrs[k]);
    }
    for (let i = 2; i < arguments.length; i++) {
      const c = arguments[i];
      if (c) el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return el;
  }
  function root() { return document.getElementById('page-deployment'); }

  function buildSkeleton() {
    const r = root();
    if (!r) { console.error('[deployment] #page-deployment tidak ada di index.html'); return null; }
    r.textContent = '';
    const tabs = h('div', { class: 'dp-tabs', role: 'tablist' });
    Object.keys(KINDS).forEach(function (k) {
      tabs.appendChild(h('button', {
        class: 'dp-tab' + (k === state.kind ? ' is-active' : ''),
        role: 'tab', 'aria-selected': k === state.kind ? 'true' : 'false',
        text: KINDS[k].title,
        onclick: function () { Deployment.open(k); },
      }));
    });
    r.appendChild(h('div', { class: 'dp-head' },
      h('h2', { class: 'dp-title', text: 'Deployment' }),
      tabs,
      h('div', { class: 'dp-actions' },
        h('span', { id: 'dp-syncinfo', class: 'dp-syncinfo' }),
        dpCanWrite() ? h('button', { id: 'dp-add', class: 'dp-btn dp-btn-primary', type: 'button',
          text: isPkg() ? '+ Tambah Paket' : '+ Tambah',
          onclick: isPkg() ? function () { openPackageForm(null); } : openCreate }) : null,
        isPkg() ? null : h('button', { id: 'dp-sync', class: 'dp-btn', text: 'Sync dari OLT', onclick: doSync })
      )
    ));
    r.appendChild(h('div', { id: 'dp-banner', class: 'dp-banner', hidden: 'hidden' }));
    r.appendChild(h('div', { id: 'dp-body', class: 'dp-tablewrap' }));
    return r;
  }

  function setBanner(kind, text) {
    const b = document.getElementById('dp-banner');
    if (!b) return;
    if (!text) { b.hidden = true; b.textContent = ''; return; }
    b.hidden = false;
    b.className = 'dp-banner dp-banner-' + kind;   // info | warn | error
    b.textContent = text;
  }

  function renderTable(data) {
    const body = document.getElementById('dp-body');
    if (!body) return;
    body.textContent = '';
    const cfg = KINDS[data.kind];
    if (!data.items.length) {
      body.appendChild(h('p', { class: 'dp-empty',
        text: 'Belum ada profile di cache. Klik "Sync dari OLT" untuk mengambilnya.' }));
      return;
    }
    const head = h('tr');
    cfg.cols.forEach(function (c) {
      head.appendChild(h('th', { class: c.num ? 'dp-num' : '', scope: 'col', text: c.label }));
    });
    if (dpCanWrite()) head.appendChild(h('th', { scope: 'col', text: 'Aksi' }));
    const tbody = h('tbody');
    data.items.forEach(function (it) {
      const tr = h('tr');
      cfg.cols.forEach(function (c) {
        const raw = it[c.key];
        const td = h('td', { class: c.num ? 'dp-num' : '' });
        td.textContent = c.fmt ? c.fmt(raw) : String(raw);   // textContent: aman dari XSS
        if (typeof raw === 'number' && (c.key === 'fixed' || c.key === 'assured' ||
            c.key === 'maximum' || c.key === 'sir' || c.key === 'pir')) {
          td.title = raw + ' kbps';
        }
        if (raw === 'default') td.classList.add('dp-muted');
        tr.appendChild(td);
      });
      if (dpCanWrite()) {
        const del = h('button', { class: 'dp-btn dp-btn-danger dp-btn-sm', type: 'button', text: 'Hapus',
          onclick: function () { openDelete(it.name); } });
        if (String(it.name).toLowerCase() === 'default') {
          del.disabled = true;
          del.title = 'Profile bawaan OLT tidak boleh dihapus';
        }
        tr.appendChild(h('td', { class: 'dp-actions-cell' }, del));
      }
      tbody.appendChild(tr);
    });
    body.appendChild(h('table', { class: 'dp-table' }, h('thead', null, head), tbody));
  }

  function renderStatus(data) {
    const info = document.getElementById('dp-syncinfo');
    if (info) {
      info.textContent = data.synced_at
        ? 'Terakhir sync: ' + new Date(data.synced_at).toLocaleString('id-ID')
        : 'Belum pernah sync';
    }
    if (data.status === 'error') {
      setBanner('error', 'Sync terakhir gagal' +
        (data.synced_at ? ' — menampilkan data lama. ' : '. ') + (data.error || ''));
    } else if (data.stale) {
      setBanner('warn', 'Data sudah lebih dari 6 jam. Klik "Sync dari OLT" untuk memperbarui.');
    } else if (data.status === 'never') {
      setBanner('info', 'Belum pernah sync dengan OLT ini.');
    } else {
      setBanner(null);
    }
  }

  function setBusy(on) {
    state.busy = on;
    const b = document.getElementById('dp-sync');
    if (b) { b.disabled = on; b.textContent = on ? 'Menyinkronkan…' : 'Sync dari OLT'; }
  }

  // -------------------------------------------------------------- aksi
  async function load() {
    const oltId = dpGetOltId();
    const body = document.getElementById('dp-body');
    if (!oltId) {
      setBanner('info', 'Pilih OLT dulu.');
      if (body) body.textContent = '';
      return;
    }
    const mySeq = ++state.seq;
    if (body) body.textContent = 'Memuat…';
    try {
      if (isPkg()) {
        const pk = await dpApi(pkgBase(oltId));
        if (mySeq !== state.seq) return;
        renderPackages(pk);
        const info = document.getElementById('dp-syncinfo');
        if (info) info.textContent = '';
        setBanner(null);
        return;
      }
      const data = await dpApi('/olts/' + encodeURIComponent(oltId) + '/profiles/' + state.kind);
      if (mySeq !== state.seq) return;          // respons basi (user pindah tab/OLT)
      renderTable(data);
      renderStatus(data);
    } catch (e) {
      if (mySeq !== state.seq) return;
      if (body) body.textContent = '';
      setBanner('error', 'Gagal memuat profile: ' + e.message);
    }
  }

  async function doSync() {
    const oltId = dpGetOltId();
    if (!oltId || state.busy) return;
    const mySeq = ++state.seq;
    setBusy(true);
    try {
      const data = await dpApi(
        '/olts/' + encodeURIComponent(oltId) + '/profiles/' + state.kind + '/sync',
        { method: 'POST' });
      if (mySeq !== state.seq) return;
      renderTable(data);
      renderStatus(data);
      if (data.throttled) setBanner('info', 'Baru saja disinkronkan; menampilkan data terbaru dari cache.');
    } catch (e) {
      if (mySeq !== state.seq) return;
      setBanner('error', e.status === 403
        ? 'Akun ini tidak punya izin untuk sync.'
        : 'Sync gagal: ' + e.message + ' Data lama tetap ditampilkan.');
    } finally {
      setBusy(false);
    }
  }

  // ===================== FASE 3a: TULIS (tambah / hapus) =====================
  var NAME_RE = /^[A-Za-z0-9_.-]{1,64}$/;
  var T_TYPES = [
    { v: 1, label: '1 · Fixed',       field: 'fixed',   bw: 'Fixed bandwidth (kbps)' },
    { v: 2, label: '2 · Assured',     field: 'assured', bw: 'Assured bandwidth (kbps)' },
    { v: 4, label: '4 · Best-effort', field: 'maximum', bw: 'Maximum bandwidth (kbps)', priority: true },
  ];

  function enc(x) { return encodeURIComponent(x); }

  function onEsc(ev) { if (ev.key === 'Escape') closeModal(); }
  function closeModal() {
    var o = document.getElementById('dp-overlay');
    if (o) o.remove();
    document.removeEventListener('keydown', onEsc);
  }
  function openModal(title, content) {
    closeModal();
    var box = h('div', { class: 'dp-modal', role: 'dialog', 'aria-modal': 'true' },
      h('div', { class: 'dp-modal-head' },
        h('h3', { class: 'dp-modal-title', text: title }),
        h('button', { class: 'dp-x', type: 'button', 'aria-label': 'Tutup', text: '×', onclick: closeModal })),
      content);
    document.body.appendChild(h('div', { id: 'dp-overlay', class: 'dp-overlay' }, box));
    document.addEventListener('keydown', onEsc);
  }

  function inputEl(id, attrs) {
    var a = { id: id, class: 'dp-input', autocomplete: 'off', spellcheck: 'false' };
    for (var k in (attrs || {})) a[k] = attrs[k];
    return h('input', a);
  }
  function fieldEl(label, input, hint) {
    return h('div', { class: 'dp-field' },
      h('label', { 'for': input.id, class: 'dp-field-label', text: label }),
      input,
      hint ? h('small', { class: 'dp-hint', text: hint }) : null);
  }
  function showError(box, msg) {
    box.textContent = '';
    box.appendChild(h('div', { class: 'dp-banner dp-banner-error', text: msg }));
  }
  function renderChecks(box, data) {
    box.textContent = '';
    var ul = h('ul', { class: 'dp-checks' });
    data.checks.forEach(function (c) {
      var cls = c.ok ? 'is-ok' : (c.pending ? 'is-pending' : 'is-bad');
      var icon = c.ok ? '✓' : (c.pending ? '…' : '✕');
      ul.appendChild(h('li', { class: 'dp-check ' + cls },
        h('span', { class: 'dp-check-icon', text: icon }),
        h('span', { class: 'dp-check-text' },
          h('strong', { text: c.label }),
          c.detail ? h('small', { text: ' — ' + c.detail }) : null)));
    });
    box.appendChild(ul);
    if (data.sequence && data.sequence.length) {
      box.appendChild(h('div', { class: 'dp-hint', text: 'Perintah yang akan dikirim ke OLT:' }));
      box.appendChild(h('pre', { class: 'dp-code', text: data.sequence.join('\n') }));
    }
    if (data.write_enabled === false) {
      box.appendChild(h('div', { class: 'dp-banner dp-banner-warn',
        text: 'Penulisan ke OLT dimatikan di server (PROFILE_WRITE_ENABLED). Hanya pratinjau.' }));
    }
  }

  function openCreate() {
    var oltId = dpGetOltId();
    if (!oltId) { setBanner('info', 'Pilih OLT dulu.'); return; }
    var kind = state.kind;
    var path = '/olts/' + enc(oltId) + '/profiles/' + kind;
    var plan = null;
    var out = h('div', { class: 'dp-preview' });
    var nameIn = inputEl('dp-f-name', { type: 'text', maxlength: '64', placeholder: 'mis. GOLD-100M' });
    var form = h('div', { class: 'dp-form' },
      fieldEl('Nama profile', nameIn, '1-64 karakter: huruf, angka, _ . -'));
    var typeSel, bwIn, prIn, bwField, prField, sirIn, pirIn, cbsIn, pbsIn;
    var numAttrs = { type: 'number', step: '1', inputmode: 'numeric' };
    function na(ph) { var a = { placeholder: ph }; for (var k in numAttrs) a[k] = numAttrs[k]; return a; }

    function curType() {
      return T_TYPES.filter(function (x) { return String(x.v) === typeSel.value; })[0];
    }
    function invalidate() { plan = null; createBtn.disabled = true; out.textContent = ''; }
    function syncType() {
      var t = curType();
      bwField.firstChild.textContent = t.bw;
      prField.hidden = !t.priority;
      if (!t.priority) prIn.value = '';
      invalidate();
    }

    if (kind === 'tcont') {
      typeSel = h('select', { id: 'dp-f-type', class: 'dp-input' });
      T_TYPES.forEach(function (t) { typeSel.appendChild(h('option', { value: String(t.v), text: t.label })); });
      bwIn = inputEl('dp-f-bw', na('64 - 9953280'));
      prIn = inputEl('dp-f-pr', na('1 - 4 (opsional)'));
      bwField = fieldEl(T_TYPES[0].bw, bwIn);
      prField = fieldEl('Priority', prIn);
      prField.hidden = true;
      form.appendChild(fieldEl('Type', typeSel));
      form.appendChild(bwField);
      form.appendChild(prField);
      typeSel.addEventListener('change', syncType);
    } else {
      sirIn = inputEl('dp-f-sir', na('0 - 10000000'));
      pirIn = inputEl('dp-f-pir', na('64 - 10000000 (opsional)'));
      cbsIn = inputEl('dp-f-cbs', na('0 - 1023 (opsional)'));
      pbsIn = inputEl('dp-f-pbs', na('0 - 1023 (opsional)'));
      form.appendChild(fieldEl('SIR (kbps)', sirIn));
      form.appendChild(fieldEl('PIR (kbps)', pirIn));
      form.appendChild(fieldEl('CBS (kbytes)', cbsIn));
      form.appendChild(fieldEl('PBS (kbytes)', pbsIn));
    }

    function collect() {
      var errs = [], body = { name: nameIn.value.trim() };
      if (!NAME_RE.test(body.name)) errs.push('Nama: 1-64 karakter, hanya huruf, angka, _ . -');
      function num(el, label, required) {
        var v = el.value.trim();
        if (v === '') { if (required) errs.push(label + ' wajib diisi'); return undefined; }
        var n = Number(v);
        if (!Number.isInteger(n)) { errs.push(label + ' harus bilangan bulat'); return undefined; }
        return n;
      }
      if (kind === 'tcont') {
        var t = curType();
        body.type = t.v;
        var bw = num(bwIn, t.bw, true);
        if (bw !== undefined) body[t.field] = bw;
        if (t.priority) {
          var pr = num(prIn, 'Priority', false);
          if (pr !== undefined) body.priority = pr;
        }
      } else {
        var sir = num(sirIn, 'SIR', true);
        if (sir !== undefined) body.sir = sir;
        [['pir', pirIn], ['cbs', cbsIn], ['pbs', pbsIn]].forEach(function (p) {
          var v = num(p[1], p[0].toUpperCase(), false);
          if (v !== undefined) body[p[0]] = v;
        });
      }
      return { errs: errs, body: body };
    }

    async function run(dry) {
      var c = collect();
      if (c.errs.length) { showError(out, c.errs.join(' · ')); return; }
      c.body.dry_run = dry;
      previewBtn.disabled = true;
      createBtn.disabled = true;
      out.textContent = dry ? 'Memeriksa OLT…' : 'Menulis ke OLT…';
      try {
        var data = await dpApi(path, { method: 'POST', body: c.body });
        if (dry) {
          plan = data;
          renderChecks(out, data);
          createBtn.disabled = !data.ready;
        } else {
          closeModal();
          await load();
          setBanner('info', 'Profile "' + c.body.name + '" berhasil dibuat.' +
            (data.warning ? ' (catatan: ' + data.warning + ')' : ''));
        }
      } catch (e) {
        showError(out, e.status === 403 ? 'Akun ini tidak punya izin untuk membuat profile.'
          : (e.message || 'Gagal'));
      } finally {
        previewBtn.disabled = false;
      }
    }

    var previewBtn = h('button', { class: 'dp-btn', type: 'button', text: 'Pratinjau',
      onclick: function () { run(true); } });
    var createBtn = h('button', { class: 'dp-btn dp-btn-primary', type: 'button', text: 'Buat profile',
      onclick: function () { if (plan && plan.ready) run(false); } });
    createBtn.disabled = true;
    [nameIn, bwIn, prIn, sirIn, pirIn, cbsIn, pbsIn].forEach(function (el) {
      if (el) el.addEventListener('input', invalidate);
    });

    openModal('Tambah ' + KINDS[kind].title,
      h('div', null,
        h('div', { class: 'dp-modal-body' }, form, out),
        h('div', { class: 'dp-modal-foot' },
          h('button', { class: 'dp-btn', type: 'button', text: 'Batal', onclick: closeModal }),
          previewBtn, createBtn)));
    nameIn.focus();
  }

  async function openDelete(name) {
    var oltId = dpGetOltId();
    if (!oltId) { setBanner('info', 'Pilih OLT dulu.'); return; }
    var kind = state.kind;
    var path = '/olts/' + enc(oltId) + '/profiles/' + kind + '/' + enc(name);
    var plan = null;
    var info = h('div', { class: 'dp-preview', text: 'Memeriksa pemakaian dan kondisi OLT…' });
    var ack = h('input', { type: 'checkbox', id: 'dp-f-ack' });
    var confIn = inputEl('dp-f-confirm', { type: 'text', placeholder: name });
    var delBtn = h('button', { class: 'dp-btn dp-btn-danger', type: 'button', text: 'Hapus profile',
      onclick: function () { go(); } });
    delBtn.disabled = true;

    function refresh() {
      delBtn.disabled = !(plan && plan.allowed && ack.checked && confIn.value === name);
    }
    ack.addEventListener('change', refresh);
    confIn.addEventListener('input', refresh);

    async function go() {
      delBtn.disabled = true;
      info.textContent = 'Menghapus di OLT…';
      try {
        var res = await dpApi(path + '/delete', { method: 'POST',
          body: { confirm_name: confIn.value, manual_ack: ack.checked, dry_run: false } });
        closeModal();
        await load();
        setBanner('info', res.message || 'Profile dihapus.');
      } catch (e) {
        showError(info, (e.message || 'Gagal menghapus') + ' — tutup dan buka lagi dialog untuk memeriksa ulang.');
        plan = null;
        refresh();
      }
    }

    openModal('Hapus ' + KINDS[kind].title,
      h('div', null,
        h('div', { class: 'dp-modal-body' },
          h('p', { class: 'dp-lead' }, 'Profile ', h('strong', { text: name }),
            ' akan dihapus dari OLT. Tindakan ini tidak bisa dibatalkan.'),
          info,
          h('label', { class: 'dp-ackrow', 'for': 'dp-f-ack' }, ack,
            h('span', { text: 'Saya sudah memverifikasi di OLT bahwa profile ini tidak dipakai ONU atau paket mana pun. ' +
              '(DB aplikasi hanya mengenal ONU yang diprovision lewat aplikasi.)' })),
          fieldEl('Ketik nama profile untuk konfirmasi', confIn)),
        h('div', { class: 'dp-modal-foot' },
          h('button', { class: 'dp-btn', type: 'button', text: 'Batal', onclick: closeModal }),
          delBtn)));

    try {
      var usage = await dpApi(path + '/usage');
      var data = await dpApi(path + '/delete', { method: 'POST', body: { dry_run: true } });
      plan = data;
      renderChecks(info, data);
      if (usage.samples && usage.samples.length) {
        info.appendChild(h('div', { class: 'dp-hint',
          text: 'Dipakai oleh: ' + usage.samples.map(function (s) { return s.name || s.interface; }).join(', ') }));
      }
      refresh();
    } catch (e) {
      showError(info, 'Gagal memeriksa: ' + (e.message || 'error'));
    }
  }

  // ===================== FASE 4b-1: SERVICE PACKAGE (DB only) =====================
  function pkgBase(oltId) { return '/olts/' + enc(oltId) + '/service-packages'; }

  function renderPackages(data) {
    var body = document.getElementById('dp-body');
    if (!body) return;
    body.textContent = '';
    if (!data.items.length) {
      body.appendChild(h('p', { class: 'dp-empty',
        text: 'Belum ada paket. Klik "+ Tambah Paket" untuk membuat paket pertama.' }));
      return;
    }
    var head = h('tr');
    [['Nama', ''], ['T-CONT', ''], ['Traffic', ''], ['VLAN', ''], ['GEMPORT', 'dp-num']].forEach(function (c) {
      head.appendChild(h('th', { class: c[1], scope: 'col', text: c[0] }));
    });
    if (dpCanWrite()) head.appendChild(h('th', { scope: 'col', text: 'Aksi' }));
    var tbody = h('tbody');
    data.items.forEach(function (it) {
      var tr = h('tr');
      var nameTd = h('td');
      nameTd.textContent = it.name;
      if (it.description) nameTd.title = it.description;
      if (it.issues && it.issues.length) {
        nameTd.appendChild(h('span', { class: 'dp-pkg-warn', title: it.issues.join('; '), text: ' ⚠' }));
      }
      tr.appendChild(nameTd);
      tr.appendChild(h('td', { text: it.tcont_profile }));
      tr.appendChild(h('td', { text: it.traffic_profile }));
      tr.appendChild(h('td', { title: 'User VLAN / VLAN', text: it.user_vlan + ' / ' + it.vlan }));
      tr.appendChild(h('td', { class: 'dp-num', text: String(it.gemport_id) }));
      if (dpCanWrite()) {
        tr.appendChild(h('td', { class: 'dp-actions-cell' },
          h('button', { class: 'dp-btn dp-btn-sm', type: 'button', text: 'Edit',
            onclick: function () { openPackageForm(it); } }),
          h('button', { class: 'dp-btn dp-btn-danger dp-btn-sm', type: 'button', text: 'Hapus',
            onclick: function () { openPackageDelete(it); } })));
      }
      tbody.appendChild(tr);
    });
    body.appendChild(h('table', { class: 'dp-table' }, h('thead', null, head), tbody));
  }

  function selectEl(id, opts, current) {
    var sel = h('select', { id: id, class: 'dp-input' });
    var found = false;
    opts.forEach(function (o) {
      if (String(o.value) === String(current)) found = true;
      sel.appendChild(h('option', { value: String(o.value), text: o.label }));
    });
    var has = current !== '' && current !== null && current !== undefined;
    if (has && !found) {
      sel.appendChild(h('option', { value: String(current), text: current + ' (tidak ditemukan di cache)' }));
    }
    if (has) sel.value = String(current);
    return sel;
  }

  async function openPackageForm(pkg) {
    var oltId = dpGetOltId();
    if (!oltId) { setBanner('info', 'Pilih OLT dulu.'); return; }
    var editing = !!pkg;
    var p = pkg || {
      name: '', description: '', tcont_profile: '', traffic_profile: '',
      gemport_id: 1, service_port: 1, vport: 1, user_vlan: '', vlan: '',
      pppoe_user_pattern: '{SN}', pppoe_password_pattern: '{RANDOM8}', pppoe_nat: true,
      firewall_level: 'low', security_mgmt_mode: 'forward', wan_service: 'internet',
    };
    var out = h('div', { class: 'dp-preview' });
    var saveBtn = h('button', { class: 'dp-btn dp-btn-primary', type: 'button',
      text: editing ? 'Simpan perubahan' : 'Buat paket' });
    saveBtn.disabled = true;
    var box = h('div', { class: 'dp-modal-body is-scroll' }, 'Memuat pilihan…');
    openModal(editing ? 'Edit Service Package' : 'Tambah Service Package',
      h('div', null, box,
        h('div', { class: 'dp-modal-foot' },
          h('button', { class: 'dp-btn', type: 'button', text: 'Batal', onclick: closeModal }),
          saveBtn)));

    var opt;
    try {
      opt = await dpApi(pkgBase(oltId) + '/options');
    } catch (e) {
      box.textContent = '';
      showError(box, 'Gagal memuat pilihan: ' + (e.message || 'error'));
      return;
    }

    var f = {};
    var numAttrs = { type: 'number', step: '1', min: '1', inputmode: 'numeric' };
    function numIn(id, val) { var el = inputEl(id, numAttrs); el.value = String(val); return el; }
    function txtIn(id, val, max) { var el = inputEl(id, { type: 'text', maxlength: String(max) }); el.value = val || ''; return el; }

    f.name = txtIn('dp-p-name', p.name, 64);
    f.name.placeholder = 'mis. PAKET-100M';
    f.desc = txtIn('dp-p-desc', p.description, 255);
    f.tcont = selectEl('dp-p-tcont', opt.tcont.map(function (n) { return { value: n, label: n }; }), p.tcont_profile);
    f.traffic = selectEl('dp-p-traffic', opt.traffic.map(function (n) { return { value: n, label: n }; }), p.traffic_profile);
    var vlOpts = opt.vlans.map(function (v) { return { value: v.vlan_id, label: v.vlan_id + (v.name ? ' - ' + v.name : '') }; });
    f.uvlan = selectEl('dp-p-uvlan', vlOpts, p.user_vlan);
    f.vlan = selectEl('dp-p-vlan', vlOpts, p.vlan);
    f.gem = numIn('dp-p-gem', p.gemport_id);
    f.sport = numIn('dp-p-sport', p.service_port);
    f.vport = numIn('dp-p-vport', p.vport);
    f.pu = txtIn('dp-p-pu', p.pppoe_user_pattern, 64);
    f.pp = txtIn('dp-p-pp', p.pppoe_password_pattern, 64);
    f.fw = txtIn('dp-p-fw', p.firewall_level, 32);
    f.sm = txtIn('dp-p-sm', p.security_mgmt_mode, 32);
    f.ws = txtIn('dp-p-ws', p.wan_service, 32);
    f.nat = h('input', { type: 'checkbox', id: 'dp-p-nat' });
    f.nat.checked = !!p.pppoe_nat;

    function wide(el) { el.classList.add('dp-field-wide'); return el; }
    var grid = h('div', { class: 'dp-form dp-form-grid' },
      wide(fieldEl('Nama paket', f.name, '1-64 karakter: huruf, angka, _ . -')),
      wide(fieldEl('Deskripsi (opsional)', f.desc)),
      fieldEl('T-CONT Profile', f.tcont),
      fieldEl('Traffic Profile', f.traffic),
      fieldEl('GEMPORT ID', f.gem),
      fieldEl('Service Port', f.sport),
      fieldEl('VPort', f.vport),
      h('div', { class: 'dp-field' }),
      fieldEl('User VLAN', f.uvlan),
      fieldEl('VLAN', f.vlan),
      fieldEl('PPPoE user pattern (opsional)', f.pu, 'Token: {SN}, {RANDOM8}. Kosongkan untuk paket yang tidak perlu PPPoE (mis. router non-OLT).'),
      fieldEl('PPPoE password pattern (opsional)', f.pp, 'Token: {SN}, {RANDOM8}'),
      fieldEl('Firewall level', f.fw),
      fieldEl('Security mgmt mode', f.sm),
      fieldEl('WAN service', f.ws),
      wide(h('label', { class: 'dp-ackrow', 'for': 'dp-p-nat' }, f.nat, h('span', { text: 'PPPoE NAT aktif' }))));

    box.textContent = '';
    if (!opt.tcont.length || !opt.traffic.length || !opt.vlans.length) {
      box.appendChild(h('div', { class: 'dp-banner dp-banner-warn',
        text: 'Cache T-CONT / Traffic / VLAN masih kosong. Sync dulu dari OLT, lalu buka form ini lagi.' }));
    }
    box.appendChild(grid);
    box.appendChild(out);
    saveBtn.disabled = false;
    f.name.focus();

    function collect() {
      var errs = [];
      function int(el, label) {
        var s = el.value.trim(), n = Number(s);
        if (s === '' || !Number.isInteger(n) || n < 1 || n > opt.id_max) {
          errs.push(label + ': bilangan bulat 1-' + opt.id_max);
          return undefined;
        }
        return n;
      }
      function vl(el, label) {
        var n = Number(el.value);
        if (el.value === '' || !Number.isInteger(n)) { errs.push(label + ' wajib dipilih'); return undefined; }
        return n;
      }
      var b = {
        name: f.name.value.trim(),
        description: f.desc.value.trim() || null,
        tcont_profile: f.tcont.value,
        traffic_profile: f.traffic.value,
        gemport_id: int(f.gem, 'GEMPORT'),
        service_port: int(f.sport, 'Service Port'),
        vport: int(f.vport, 'VPort'),
        user_vlan: vl(f.uvlan, 'User VLAN'),
        vlan: vl(f.vlan, 'VLAN'),
        pppoe_user_pattern: f.pu.value.trim(),
        pppoe_password_pattern: f.pp.value.trim(),
        pppoe_nat: f.nat.checked,
        firewall_level: f.fw.value.trim(),
        security_mgmt_mode: f.sm.value.trim(),
        wan_service: f.ws.value.trim(),
      };
      if (!NAME_RE.test(b.name)) errs.push('Nama: 1-64 karakter, hanya huruf, angka, _ . -');
      if (!b.tcont_profile) errs.push('T-CONT Profile wajib dipilih');
      if (!b.traffic_profile) errs.push('Traffic Profile wajib dipilih');
      return { errs: errs, body: b };
    }

    saveBtn.onclick = async function () {
      var c = collect();
      if (c.errs.length) { showError(out, c.errs.join(' · ')); return; }
      saveBtn.disabled = true;
      out.textContent = 'Menyimpan…';
      try {
        await dpApi(editing ? pkgBase(oltId) + '/' + enc(pkg.id) : pkgBase(oltId),
          { method: editing ? 'PUT' : 'POST', body: c.body });
        closeModal();
        await load();
        setBanner('info', 'Paket "' + c.body.name + '" ' + (editing ? 'diperbarui.' : 'dibuat.'));
      } catch (e) {
        showError(out, e.status === 403 ? 'Akun ini tidak punya izin mengelola paket.'
          : (e.message || 'Gagal menyimpan'));
        saveBtn.disabled = false;
      }
    };
  }

  function openPackageDelete(pkg) {
    var oltId = dpGetOltId();
    if (!oltId) { setBanner('info', 'Pilih OLT dulu.'); return; }
    var out = h('div', { class: 'dp-preview' });
    var delBtn = h('button', { class: 'dp-btn dp-btn-danger', type: 'button', text: 'Hapus paket' });
    delBtn.onclick = async function () {
      delBtn.disabled = true;
      out.textContent = 'Menghapus…';
      try {
        await dpApi(pkgBase(oltId) + '/' + enc(pkg.id), { method: 'DELETE' });
        closeModal();
        await load();
        setBanner('info', 'Paket "' + pkg.name + '" dihapus.');
      } catch (e) {
        showError(out, e.status === 403 ? 'Akun ini tidak punya izin mengelola paket.'
          : (e.message || 'Gagal menghapus'));
        delBtn.disabled = false;
      }
    };
    openModal('Hapus Service Package',
      h('div', null,
        h('div', { class: 'dp-modal-body' },
          h('p', { class: 'dp-lead' }, 'Paket ', h('strong', { text: pkg.name }),
            ' akan dihapus dari aplikasi. OLT tidak diubah.'),
          out),
        h('div', { class: 'dp-modal-foot' },
          h('button', { class: 'dp-btn', type: 'button', text: 'Batal', onclick: closeModal }),
          delBtn)));
    dpApi(pkgBase(oltId) + '/' + enc(pkg.id) + '/usage').then(function (u) {
      if (u && u.count > 0) {
        showError(out, 'Paket masih dipakai ' + u.count + ' ONU, tidak bisa dihapus.');
        delBtn.disabled = true;
      }
    }).catch(function () { });
  }

  // ------------------------------------------------------------- API publik
  window.Deployment = {
    open: function (kind) {
      state.kind = KINDS[kind] ? kind : 'tcont';
      state.seq++;                              // batalkan respons yang masih jalan
      if (!buildSkeleton()) return;
      load();
    },
    reload: load,
    // Untuk wizard Provision ONU (Fase 4): daftar profile dari cache server.
    fetchProfiles: function (oltId, kind) {
      return dpApi('/olts/' + encodeURIComponent(oltId) + '/profiles/' + kind);
    },
    fetchPackages: function (oltId) {
      return dpApi(pkgBase(oltId));
    },
  };
})();
