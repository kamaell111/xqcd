/*
 * wa.js — WA-1: halaman WhatsApp Bot (setting gateway, template test, test kirim).
 *
 * Mandiri: membangun DOM sendiri di <section id="page-wa">.
 * API publik:  WaBot.open()
 * Bergantung pada global app.js: api(), toast(), CURRENT_USER, CURRENT_OLT_ID.
 */
(function () {
  'use strict';

  var state = { scope: null, providers: [], cfg: null, seq: 0, busy: false, lastDefaultUrl: '', tab: 'setting', masterSeq: 0, master: null, masterBusy: false, masterTimer: null };

  // --------------------------------------------------------------- util
  function h(tag, attrs) {
    var el = document.createElement(tag);
    for (var k in (attrs || {})) {
      if (k === 'class') el.className = attrs[k];
      else if (k === 'text') el.textContent = attrs[k];
      else if (k.indexOf('on') === 0) el.addEventListener(k.slice(2), attrs[k]);
      else el.setAttribute(k, attrs[k]);
    }
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c) el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return el;
  }
  function root() { return document.getElementById('page-wa'); }
  function q(id) { return document.getElementById(id); }
  function call(path, method, body) {
    var opts = { method: method || 'GET' };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return api(path, opts);
  }
  function sq(path) { return path + '?scope=' + encodeURIComponent(state.scope); }

  function scopeOptions() {
    var out = [];
    var u = (typeof CURRENT_USER !== 'undefined') ? CURRENT_USER : null;
    if (u && u.is_super_admin) out.push({ v: 'global', label: 'Global (semua OLT)' });
    if (typeof CURRENT_OLT_ID !== 'undefined' && CURRENT_OLT_ID) {
      out.push({ v: 'olt:' + CURRENT_OLT_ID, label: 'OLT aktif (olt:' + CURRENT_OLT_ID + ')' });
    }
    if (u && u.id) out.push({ v: 'user:' + u.id, label: 'Akun saya (user:' + u.id + ')' });
    return out;
  }

  function setBanner(kind, text) {
    var b = q('wa-banner');
    if (!b) return;
    if (!text) { b.hidden = true; b.textContent = ''; return; }
    b.hidden = false;
    b.className = 'dp-banner dp-banner-' + kind;   // info | warn | error
    b.textContent = text;
  }

  function busy(on) {
    state.busy = on;
    ['wa-save', 'wa-test', 'wa-save-tpl'].forEach(function (id) {
      var el = q(id);
      if (el) el.disabled = on;
    });
  }

  // --------------------------------------------------------------- skeleton
  function field(label, input, hint, wide) {
    return h('div', { class: 'dp-field' + (wide ? ' dp-field-wide' : '') },
      h('label', { 'for': input.id, class: 'dp-field-label', text: label }),
      input,
      hint ? h('small', { id: input.id + '-hint', class: 'dp-hint', text: hint }) : null);
  }

  function build() {
    var r = root();
    if (!r) { console.error('[wa] #page-wa tidak ada di index.html'); return false; }
    r.textContent = '';

    var scopeSel = h('select', { id: 'wa-scope', class: 'dp-input', onchange: function () {
      state.scope = this.value;
      state.seq++;
      state.masterSeq++;
      state.master = null;
      state.masterBusy = false;
      renderMaster();
      loadAll();
      switchTab(state.tab || 'setting');
      loadMaster();
    } });
    scopeOptions().forEach(function (o) {
      scopeSel.appendChild(h('option', { value: o.v, text: o.label }));
    });
    if (!state.scope || !scopeOptions().some(function (o) { return o.v === state.scope; })) {
      state.scope = scopeOptions().length ? scopeOptions()[0].v : null;
    }
    if (state.scope) scopeSel.value = state.scope;

    r.appendChild(h('div', { class: 'dp-head' },
      h('h2', { class: 'dp-title', text: 'WhatsApp Bot' }),
      h('div', { class: 'dp-actions' },
        h('label', { 'for': 'wa-scope', class: 'dp-hint', text: 'Scope:' }), scopeSel)));
    r.appendChild(h('div', { id: 'wa-banner', class: 'dp-banner', hidden: 'hidden' }));

    // Master toggle (WA-2)
    r.appendChild(buildMasterCard());

    var TABS = [
      { id: 'setting', label: 'Setting' },
      { id: 'rules', label: 'Alert Rules' }
    ];
    var tabBar = h('div', { class: 'dp-tabs', style: 'margin: 12px 0;' });
    TABS.forEach(function (t) {
      tabBar.appendChild(h('button', {
        class: 'dp-tab' + (state.tab === t.id ? ' is-active' : ''),
        type: 'button', 'data-tab': t.id, text: t.label,
        onclick: function () { switchTab(t.id); }
      }));
    });
    r.appendChild(tabBar);

    var paneSetting = h('div', { id: 'wa-pane-setting' });
    r.appendChild(paneSetting);

    // --- form setting
    var provider = h('select', { id: 'wa-provider', class: 'dp-input', onchange: onProviderChange });
    var url = h('input', { id: 'wa-url', class: 'dp-input', type: 'url', maxlength: '500',
      autocomplete: 'off', spellcheck: 'false', placeholder: 'https://…' });
    var token = h('input', { id: 'wa-token', class: 'dp-input', type: 'password', maxlength: '512',
      autocomplete: 'new-password', spellcheck: 'false' });
    var clr = h('input', { type: 'checkbox', id: 'wa-clear-token' });
    var target = h('input', { id: 'wa-target', class: 'dp-input', type: 'text', maxlength: '255',
      autocomplete: 'off', spellcheck: 'false', placeholder: '62812xxxxxxx atau ID grup' });
    var enabled = h('input', { type: 'checkbox', id: 'wa-enabled' });

    var form = h('div', { class: 'dp-form dp-form-grid wa-form' },
      field('Provider', provider, null, false),
      field('URL gateway', url, 'Pilih provider untuk melihat petunjuk.', false),
      field('API token', token, '', true),
      h('label', { class: 'dp-ackrow dp-field-wide', 'for': 'wa-clear-token', id: 'wa-clear-row', hidden: 'hidden' },
        clr, h('span', { text: 'Hapus token tersimpan' })),
      field('Target (nomor / ID grup)', target, 'Beberapa target pisahkan dengan koma. Tanpa spasi.', true),
      h('label', { class: 'dp-ackrow dp-field-wide', 'for': 'wa-enabled' },
        enabled, h('span', { text: 'Bot aktif' })));

    var actions = h('div', { class: 'wa-actions' },
      h('button', { id: 'wa-save', class: 'dp-btn dp-btn-primary', type: 'button',
        text: 'Simpan Setting', onclick: save }),
      h('button', { id: 'wa-test', class: 'dp-btn', type: 'button',
        text: 'Test Kirim', onclick: test }));

    paneSetting.appendChild(h('div', { class: 'wa-card' },
      h('h3', { class: 'wa-card-title', text: 'Gateway' }), form, actions,
      h('div', { id: 'wa-result', class: 'dp-preview' })));

    // --- template + preview
    var tpl = h('textarea', { id: 'wa-tpl', class: 'dp-input wa-tpl', rows: '6', maxlength: '1000',
      spellcheck: 'false' });

    // Preset template: klik untuk isi textarea
    var PRESETS = [
      { id: 'test',      label: '\uD83E\uDDEA Test Connection',
        body: '\uD83E\uDDEA *Test Connection*\n\nStatus: \u2705 Terhubung ke WhatsApp Gateway\nWaktu: {time}' },
      { id: 'los',       label: '\uD83D\uDD34 ONU Putus (LOS)',
        body: '\uD83D\uDD34 *ONU Putus (LOS)*\n\nOLT: {olt_name}\nONU: {onu_name}\nSN: {sn}\nStatus: {status}\nWaktu: {time}' },
      { id: 'offline',   label: '\u26AB ONU Offline',
        body: '\u26AB *ONU Offline*\n\nOLT: {olt_name}\nONU: {onu_name}\nSN: {sn}\nSejak: {since}\nWaktu: {time}' },
      { id: 'uncfg',     label: '\uD83C\uDD95 ONU Baru',
        body: '\uD83C\uDD95 *ONU Baru Terdeteksi*\n\nOLT: {olt_name}\nSN: {sn}\nLokasi: {onu_name}\nWaktu: {time}' },
      { id: 'sysalert',  label: '\u26A0\uFE0F Alert Sistem',
        body: '\u26A0\uFE0F *Alert Sistem*\n\n{title}\n{message}\nOLT: {olt_name}\nWaktu: {time}' },
      { id: 'custom',    label: '\u270F\uFE0F Custom (tulis sendiri)', body: '' }
    ];

    var presetBar = h('div', { class: 'wa-presets', style: 'display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px;' });
    PRESETS.forEach(function (p) {
      presetBar.appendChild(h('button', {
        class: 'dp-btn', type: 'button', text: p.label,
        style: 'font-size:12px; padding:6px 10px;',
        onclick: function () { tpl.value = p.body; }
      }));
    });

    paneSetting.appendChild(h('div', { class: 'wa-card' },
      h('h3', { class: 'wa-card-title', text: 'Pesan test' }),
      h('div', { class: 'dp-hint', style: 'margin-bottom:6px;', text: 'Pilih template cepat:' }),
      presetBar,
      field('Template (bisa diedit)', tpl, 'Placeholder: {total_onu}, {time}', true),
      h('div', { class: 'wa-actions' },
        h('button', { id: 'wa-save-tpl', class: 'dp-btn dp-btn-primary', type: 'button', text: 'Simpan Template', onclick: saveTpl }),
        h('button', { id: 'wa-reset-tpl', class: 'dp-btn', type: 'button', text: 'Kembalikan default', onclick: resetTpl }),
        h('button', { class: 'dp-btn', type: 'button', text: 'Segarkan preview', onclick: loadPreview })),
      h('div', { class: 'dp-hint', text: 'Preview (angka ONU asli dari database):' }),
      h('pre', { id: 'wa-preview', class: 'dp-code wa-preview', text: '\u2026' })));

    r.appendChild(h('div', { id: 'wa-pane-rules', hidden: 'hidden' }));

    startMasterPoll();
    loadMaster();
    return true;
  }

  // --------------------------------------------------------------- data
  function providerById(id) {
    for (var i = 0; i < state.providers.length; i++) if (state.providers[i].id === id) return state.providers[i];
    return null;
  }

  function onProviderChange() {
    var p = providerById(q('wa-provider').value);
    var url = q('wa-url');
    if (p && (!url.value.trim() || url.value.trim() === state.lastDefaultUrl)) {
      url.value = p.default_url || '';
    }
    state.lastDefaultUrl = p ? (p.default_url || '') : '';
    var hint = q('wa-url-hint');
    if (hint) hint.textContent = p ? p.hint : '';
    var th = q('wa-token-hint');
    if (th) th.textContent = p && !p.token_required ? 'Opsional untuk provider ini.' : 'Wajib untuk provider ini.';
    updateTokenHint();
  }

  function updateTokenHint() {
    var cfg = state.cfg, th = q('wa-token-hint');
    if (!th) return;
    if (cfg && cfg.has_token) {
      th.textContent = 'Tersimpan (' + (cfg.token_preview || '••••') + '). Kosongkan kolom ini untuk tetap memakai token lama.';
    }
  }

  function fill(cfg) {
    state.cfg = cfg;
    var sel = q('wa-provider');
    if (!cfg.configured && state.providers.length && !sel.value) sel.value = state.providers[0].id;
    if (cfg.configured) sel.value = cfg.provider;
    q('wa-url').value = cfg.gateway_url || '';
    q('wa-target').value = cfg.target || '';
    q('wa-enabled').checked = !!cfg.enabled;
    q('wa-token').value = '';
    q('wa-clear-token').checked = false;
    q('wa-clear-row').hidden = !cfg.has_token;
    state.lastDefaultUrl = '';
    onProviderChange();
    if (!cfg.configured) setBanner('info', 'Scope ini belum punya setting WhatsApp.');
    else setBanner(null);
  }

  async function loadAll() {
    if (!state.scope) { setBanner('info', 'Tidak ada scope yang bisa dipakai. Pilih OLT dulu.'); return; }
    var my = ++state.seq;
    q('wa-result').textContent = '';
    try {
      var res = await Promise.all([
        call(sq('/wa/settings')),
        call(sq('/wa/templates')),
      ]);
      if (my !== state.seq) return;
      fill(res[0]);
      var t = res[1].items.filter(function (x) { return x.event === 'test'; })[0];
      if (t) { q('wa-tpl').value = t.message_template; q('wa-tpl').dataset.def = t.default_template; }
      loadPreview();
    } catch (e) {
      if (my !== state.seq) return;
      setBanner('error', 'Gagal memuat: ' + (e.message || 'error'));
    }
  }

  async function loadPreview() {
    var box = q('wa-preview');
    if (!box || !state.scope) return;
    var my = state.seq;
    try {
      var d = await call(sq('/wa/preview'));
      if (my !== state.seq) return;
      box.textContent = d.message;
    } catch (e) {
      box.textContent = 'Preview gagal: ' + (e.message || 'error');
    }
  }

  // --------------------------------------------------------------- aksi
  function showResult(kind, text) {
    var box = q('wa-result');
    box.textContent = '';
    box.appendChild(h('div', { class: 'dp-banner dp-banner-' + kind, text: text }));
  }

  async function save() {
    if (state.busy) return;
    var body = {
      provider: q('wa-provider').value,
      gateway_url: q('wa-url').value.trim(),
      target: q('wa-target').value.trim(),
      enabled: q('wa-enabled').checked,
      api_token: q('wa-token').value.trim() || null,
      clear_token: q('wa-clear-token').checked,
    };
    if (!body.provider) { showResult('error', 'Pilih provider dulu.'); return; }
    if (!/^https?:\/\//i.test(body.gateway_url)) { showResult('error', 'URL gateway harus diawali http:// atau https://'); return; }
    if (!/^[0-9A-Za-z@._,+-]{1,255}$/.test(body.target)) { showResult('error', 'Target tidak valid (tanpa spasi).'); return; }
    busy(true);
    showResult('info', 'Menyimpan…');
    try {
      var cfg = await call(sq('/wa/settings'), 'PUT', body);
      fill(cfg);
      showResult('info', 'Setting disimpan.');
      toast('Setting WhatsApp disimpan', 'success');
    } catch (e) {
      showResult('error', e.message || 'Gagal menyimpan');
    } finally { busy(false); }
  }

  async function test() {
    if (state.busy) return;
    busy(true);
    showResult('info', 'Mengirim pesan test…');
    try {
      var d = await call(sq('/wa/test'), 'POST');
      showResult('info', d.detail + '. Pesan: ' + d.message.replace(/\n+/g, ' / '));
      toast('Test terkirim', 'success');
    } catch (e) {
      showResult('error', e.message || 'Test gagal');
    } finally { busy(false); }
  }

  async function saveTpl() {
    if (state.busy) return;
    busy(true);
    try {
      await call(sq('/wa/templates/test'), 'PUT', { message_template: q('wa-tpl').value });
      toast('Template disimpan', 'success');
      loadPreview();
    } catch (e) {
      showResult('error', e.message || 'Gagal menyimpan template');
    } finally { busy(false); }
  }

  function resetTpl() {
    var d = q('wa-tpl').dataset.def;
    if (d) q('wa-tpl').value = d;
  }

  // --------------------------------------------------------------- WA-2-2 tabs & alert
  function switchTab(tabId) {
    if (tabId === 'log') tabId = 'setting';   // log dipindah ke halaman Alerts
    state.tab = tabId;
    ['setting', 'rules'].forEach(function (t) {
      var p = q('wa-pane-' + t);
      if (p) p.hidden = (t !== tabId);
    });
    var btns = root().querySelectorAll('.dp-tab');
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      if (b.getAttribute('data-tab') === tabId) b.classList.add('is-active');
      else b.classList.remove('is-active');
    }
    if (tabId === 'rules') loadAlertRules();
  }

  function loadAlertStatus() {
    return call('/wa/alerts-status?scope=' + encodeURIComponent(state.scope));
  }

  function loadAlertRules() {
    var pane = q('wa-pane-rules');
    if (!pane) return;
    var mySeq = ++state.seq;
    var myScope = state.scope;
    pane.textContent = '';
    pane.appendChild(h('div', { class: 'dp-hint', text: 'Memuat rule...' }));
    Promise.all([
      loadAlertStatus(),
      call('/wa/alert-rules?scope=' + encodeURIComponent(myScope))
    ])
      .then(function (res) {
        if (mySeq !== state.seq) return;
        renderAlertRules(pane, res[1], res[0], myScope);
      })
      .catch(function (e) {
        if (mySeq !== state.seq) return;
        pane.textContent = '';
        pane.appendChild(h('div', { class: 'dp-banner dp-banner-error',
          text: 'Gagal memuat rule: ' + (e && e.message || 'error') }));
      });
  }

  function renderAlertRules(pane, rules, status, scope) {
    pane.textContent = '';
    if (status && !status.effective) {
      var _why = (status.blocked_by === 'master_off')
        ? 'Alert otomatis sedang mati. Nyalakan master toggle di atas agar rule ini mengirim WA.'
        : 'Rule belum bisa mengirim: ' + (BLOCK_LABEL[status.blocked_by] || status.blocked_by || 'alasan tidak diketahui') + '.';
      pane.appendChild(h('div', { class: 'dp-banner dp-banner-warn', style: 'margin-bottom: 12px;', text: _why }));
    }
    if (!rules || !rules.length) {
      pane.appendChild(h('div', { class: 'dp-hint', text: 'Belum ada rule.' }));
      return;
    }
    rules.forEach(function (r) { pane.appendChild(renderRuleCard(r, scope)); });
  }

  // Label awam + deskripsi + default info per event
  var RULE_META = {
    onu_los: {
      emoji: '\uD83D\uDD34',
      label: 'ONU Putus (LOS)',
      desc: 'Kirim WA saat kabel fiber pelanggan putus.',
      defaults: 'Default: tunggu 2 menit, jeda 30 menit, ringkas jika \u2265 10 putus bersamaan.'
    },
    onu_offline: {
      emoji: '\u26AB',
      label: 'ONU Offline',
      desc: 'Kirim WA saat ONU hilang dari jaringan (mungkin modem dimatikan pelanggan).',
      defaults: 'Default: tunggu 5 menit, jeda 30 menit, ringkas jika \u2265 10 offline bersamaan.'
    },
    onu_uncfg: {
      emoji: '\uD83C\uDD95',
      label: 'ONU Baru Belum Terdaftar',
      desc: 'Kirim WA saat ada ONU baru terdeteksi tapi belum didaftarkan.',
      defaults: 'Default: langsung kirim, jeda 60 menit.'
    },
    system_alert: {
      emoji: '\u26A0\uFE0F',
      label: 'Alert Sistem (CPU / Suhu)',
      desc: 'Kirim WA saat CPU atau suhu OLT tinggi, atau alert penting dari sistem.',
      defaults: 'Default: langsung kirim, jeda 30 menit, minimal severity warning.'
    }
  };

  function ruleMeta(event) {
    return RULE_META[event] || { emoji: '\u2139\uFE0F', label: event, desc: '', defaults: '' };
  }

  function renderRuleCard(r, myScope) {
    var meta = ruleMeta(r.event);
    var enabled = h('input', { type: 'checkbox', id: 'rule-en-' + r.event });
    enabled.checked = !!r.enabled;
    var minDur = h('input', { type: 'number', min: '0', max: '86400', class: 'dp-input',
      id: 'rule-min-' + r.event, value: r.min_duration_sec });
    var cooldown = h('input', { type: 'number', min: '0', max: '10080', class: 'dp-input',
      id: 'rule-cd-' + r.event, value: r.cooldown_min });
    var remind = h('input', { type: 'number', min: '0', max: '10080', class: 'dp-input',
      id: 'rule-rem-' + r.event, value: r.remind_after_min });
    var mass = h('input', { type: 'number', min: '0', max: '1000', class: 'dp-input',
      id: 'rule-mass-' + r.event, value: r.mass_threshold });
    var tgt = h('input', { type: 'text', class: 'dp-input', maxlength: '255',
      id: 'rule-tgt-' + r.event, value: r.target_override || '',
      placeholder: '(kosong = pakai default)' });
    var statusEl = h('div', { class: 'dp-hint', style: 'margin-top: 8px;' });
    var saveBtn = h('button', { class: 'dp-btn dp-btn-primary', type: 'button', text: 'Simpan' });
    var testBtn = h('button', { class: 'dp-btn', type: 'button', text: 'Test Kirim Contoh' });
    var toggleBtn = h('button', { class: 'dp-btn', type: 'button', text: '\u2699\uFE0F Atur lebih lanjut' });
    var resetBtn = h('button', { class: 'dp-btn', type: 'button', text: '\u21BA Kembalikan default' });
    resetBtn.onclick = function () {
      if (!window.confirm('Kembalikan rule ' + r.event + ' ke default pabrik? (enabled=OFF, semua setting direset)')) return;
      resetBtn.disabled = true;
      statusEl.textContent = 'Reset...';
      call('/wa/alert-rules/' + encodeURIComponent(r.event) + '/reset?scope=' + encodeURIComponent(myScope), 'POST')
        .then(function (res) {
          statusEl.textContent = 'Dikembalikan ke default.';
          if (typeof toast === 'function') toast('Rule direset');
          if (res && res.rule) {
            enabled.checked = !!res.rule.enabled;
            minDur.value = res.rule.min_duration_sec;
            cooldown.value = res.rule.cooldown_min;
            remind.value = res.rule.remind_after_min;
            mass.value = res.rule.mass_threshold;
            tgt.value = res.rule.target_override || '';
          }
        })
        .catch(function (e) {
          statusEl.textContent = 'Gagal reset: ' + (e && e.message || 'error');
        })
        .finally(function () { resetBtn.disabled = false; });
    };

    // Advanced fields (hidden default)
    var advancedBox = h('div', { id: 'rule-adv-' + r.event, hidden: 'hidden',
      style: 'margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--border, #ccc);' },
      h('div', { class: 'dp-form-grid' },
        field('\u23F1\uFE0F Tunggu dulu sebelum kirim (detik)', minDur,
              'Biarkan ONU benar-benar putus sebelum kirim WA.', false),
        field('\uD83D\uDD01 Jeda minimum antar WA (menit)', cooldown,
              'Biar ONU yang sering flapping tidak spam WA.', false),
        field('\uD83D\uDCE2 Kirim 1 ringkasan jika \u2265 N ONU sekaligus', mass,
              'Mis. fiber putus \u2014 jadikan 1 WA ringkasan, bukan N WA.', false),
        field('\uD83D\uDD14 Ingatkan lagi setelah (menit)', remind,
              '0 = tidak pernah diingatkan. Minimal 30 menit kalau aktif. '
              + 'Reminder terlalu pendek bisa membuat nomor WA kena ban.', false),
        field('\uD83C\uDFAF Kirim ke nomor lain (opsional)', tgt,
              'Kosongkan = pakai nomor target dari tab Setting.', true)));

    toggleBtn.onclick = function () {
      if (advancedBox.hidden) {
        advancedBox.hidden = false;
        toggleBtn.textContent = '\u2699\uFE0F Sembunyikan';
      } else {
        advancedBox.hidden = true;
        toggleBtn.textContent = '\u2699\uFE0F Atur lebih lanjut';
      }
    };

    function collect() {
      return {
        enabled: enabled.checked,
        min_duration_sec: parseInt(minDur.value, 10) || 0,
        cooldown_min: parseInt(cooldown.value, 10) || 0,
        remind_after_min: parseInt(remind.value, 10) || 0,
        mass_threshold: parseInt(mass.value, 10) || 0,
        target_override: (tgt.value || '').trim() || null
      };
    }

    saveBtn.onclick = function () {
      saveBtn.disabled = true;
      testBtn.disabled = true;
      toggleBtn.disabled = true;
      statusEl.textContent = 'Menyimpan...';
      call('/wa/alert-rules/' + encodeURIComponent(r.event) + '?scope=' + encodeURIComponent(myScope),
        'PUT', collect())
        .then(function (res) {
          var msg = 'Tersimpan.';
          if (res && res.baseline) msg += ' Baseline dicatat.';
          if (res && res.pending_cancelled) msg += ' Pending dibatalkan: ' + res.pending_cancelled;
          statusEl.textContent = msg;
          if (typeof toast === 'function') toast(msg);
          if (res && res.rule) {
            enabled.checked = !!res.rule.enabled;
            minDur.value = res.rule.min_duration_sec;
            cooldown.value = res.rule.cooldown_min;
            remind.value = res.rule.remind_after_min;
            mass.value = res.rule.mass_threshold;
            tgt.value = res.rule.target_override || '';
          }
        })
        .catch(function (e) {
          statusEl.textContent = 'Gagal: ' + (e && e.message || 'error');
        })
        .finally(function () {
          saveBtn.disabled = false;
          testBtn.disabled = false;
          toggleBtn.disabled = false;
        });
    };

    testBtn.onclick = function () {
      saveBtn.disabled = true;
      testBtn.disabled = true;
      toggleBtn.disabled = true;
      statusEl.textContent = 'Mengirim contoh...';
      call('/wa/alert-rules/' + encodeURIComponent(r.event) + '/test?scope=' + encodeURIComponent(myScope), 'POST')
        .then(function () {
          statusEl.textContent = 'Contoh terkirim. Cek WA.';
          if (typeof toast === 'function') toast('Contoh ' + r.event + ' terkirim');
        })
        .catch(function (e) {
          statusEl.textContent = 'Gagal test: ' + (e && e.message || 'error');
        })
        .finally(function () {
          saveBtn.disabled = false;
          testBtn.disabled = false;
          toggleBtn.disabled = false;
        });
    };

    return h('div', { class: 'wa-card' },
      // Header: checkbox + emoji + label
      h('label', { style: 'display:flex; align-items:flex-start; gap:10px; cursor:pointer;' },
        h('div', { style: 'margin-top: 2px;' }, enabled),
        h('div', { style: 'flex: 1;' },
          h('div', { style: 'font-size: 1.05em; font-weight: 600;' },
            meta.emoji + ' ' + meta.label),
          h('div', { class: 'dp-hint', style: 'margin-top: 4px; font-style: italic;' },
            meta.desc))),
      // Keterangan default
      h('div', { style: 'margin: 8px 0 0 30px; padding: 6px 10px; font-size: 12px; ' +
                        'background: rgba(127,127,127,0.07); border-radius: 6px; color: inherit; opacity: 0.85;' },
        meta.defaults),
      // Advanced fields (hidden default)
      advancedBox,
      // Actions
      h('div', { class: 'wa-actions', style: 'margin-top: 12px;' }, toggleBtn, testBtn, saveBtn, resetBtn),
      statusEl);
  }

  function loadAlertLog() {
    var pane = q('wa-pane-log');
    if (!pane) return;
    var mySeq = ++state.seq;
    var myScope = state.scope;
    pane.textContent = '';
    pane.appendChild(h('div', { class: 'dp-hint', text: 'Memuat log...' }));
    call('/wa/alerts-log?limit=50&scope=' + encodeURIComponent(myScope))
      .then(function (res) {
        if (mySeq !== state.seq) return;
        renderAlertLog(pane, res, myScope);
      })
      .catch(function (e) {
        if (mySeq !== state.seq) return;
        pane.textContent = '';
        pane.appendChild(h('div', { class: 'dp-banner dp-banner-error',
          text: 'Gagal memuat log: ' + (e && e.message || 'error') }));
      });
  }

  function renderAlertLog(pane, data, myScope) {
    pane.textContent = '';
    var items = (data && data.items) || [];

    var selectAll = h('input', { type: 'checkbox', id: 'wa-log-select-all',
      style: 'accent-color: var(--accent, #38bdf8); width: 16px; height: 16px; cursor: pointer;' });
    var delBtn = h('button', { class: 'dp-btn', type: 'button',
      style: 'background: rgba(248,113,113,.15); border: 1px solid rgba(248,113,113,.4); color: #f87171;',
      text: 'Hapus Terpilih', disabled: 'disabled' });
    var countLabel = h('span', { class: 'dp-hint', style: 'margin-left: 6px;' });

    function updateBulk() {
      var checked = pane.querySelectorAll('.wa-log-cb:checked').length;
      delBtn.disabled = (checked === 0);
      countLabel.textContent = checked > 0 ? '(' + checked + ')' : '';
    }

    selectAll.onchange = function () {
      var on = selectAll.checked;
      pane.querySelectorAll('.wa-log-cb').forEach(function (cb) { cb.checked = on; });
      updateBulk();
    };

    delBtn.onclick = function () {
      var ids = [];
      pane.querySelectorAll('.wa-log-cb:checked').forEach(function (cb) {
        ids.push(parseInt(cb.getAttribute('data-id'), 10));
      });
      if (!ids.length) return;
      if (!window.confirm('Hapus ' + ids.length + ' log dari WA Alert Log? Tindakan ini tidak bisa dibatalkan.')) return;
      delBtn.disabled = true;
      delBtn.textContent = 'Menghapus...';
      call('/wa/alerts-log/bulk-delete?scope=' + encodeURIComponent(myScope), 'POST', { ids: ids })
        .then(function (res) {
          if (typeof toast === 'function') toast('Terhapus: ' + (res.deleted || ids.length) + ' log', 'success');
          loadAlertLog();
        })
        .catch(function (e) {
          alert('Gagal hapus: ' + (e && e.message || 'error'));
          delBtn.disabled = false;
          delBtn.textContent = 'Hapus Terpilih';
        });
    };

    pane.appendChild(h('div', { class: 'wa-actions' },
      h('button', { class: 'dp-btn', type: 'button', text: 'Segarkan', onclick: loadAlertLog }),
      h('label', { class: 'dp-hint', style: 'display: inline-flex; align-items: center; gap: 6px; cursor: pointer; margin-left: 12px;' },
        selectAll, h('span', { text: 'Pilih semua' })),
      delBtn,
      countLabel));

    if (!items.length) {
      pane.appendChild(h('div', { class: 'dp-hint', text: 'Belum ada log.' }));
      return;
    }

    var tbody = h('tbody', {});
    items.forEach(function (it) {
      var cb = h('input', { type: 'checkbox', class: 'wa-log-cb', 'data-id': String(it.id),
        style: 'accent-color: var(--accent, #38bdf8); width: 16px; height: 16px; cursor: pointer;' });
      cb.onchange = updateBulk;

      var actCell = h('td', {});
      if (it.status === 'failed' || it.status === 'expired') {
        var retryBtn = h('button', { class: 'dp-btn', type: 'button', text: 'Retry' });
        retryBtn.onclick = function () {
          retryBtn.disabled = true;
          retryBtn.textContent = 'Retry...';
          call('/wa/alerts-log/' + it.id + '/retry?scope=' + encodeURIComponent(myScope), 'POST')
            .then(function () { loadAlertLog(); })
            .catch(function (e) {
              alert('Gagal retry: ' + (e && e.message || 'error'));
              retryBtn.disabled = false;
              retryBtn.textContent = 'Retry';
            });
        };
        actCell.appendChild(retryBtn);
      }

      var delOneBtn = h('button', { class: 'dp-btn', type: 'button', text: 'Hapus',
        style: 'background: rgba(248,113,113,.12); border: 1px solid rgba(248,113,113,.35); color: #f87171; margin-left: 6px;' });
      delOneBtn.onclick = function () {
        if (!window.confirm('Hapus log ini? Tidak bisa dibatalkan.')) return;
        delOneBtn.disabled = true;
        call('/wa/alerts-log/' + it.id + '?scope=' + encodeURIComponent(myScope), 'DELETE')
          .then(function () {
            if (typeof toast === 'function') toast('Log dihapus', 'success');
            loadAlertLog();
          })
          .catch(function (e) {
            alert('Gagal hapus: ' + (e && e.message || 'error'));
            delOneBtn.disabled = false;
          });
      };
      actCell.appendChild(delOneBtn);

      tbody.appendChild(h('tr', {},
        h('td', {}, cb),
        h('td', { text: (it.created_at || '').replace('T', ' ').slice(0, 19) }),
        h('td', { text: it.event || '' }),
        h('td', { text: it.device_label || it.device_key || '' }),
        h('td', { text: it.status || '' }),
        actCell));
    });

    var table = h('table', { class: 'dp-table' },
      h('thead', {}, h('tr', {},
        h('th', { text: '' }),
        h('th', { text: 'Waktu' }),
        h('th', { text: 'Event' }),
        h('th', { text: 'Device' }),
        h('th', { text: 'Status' }),
        h('th', { text: 'Aksi' }))),
      tbody);
    pane.appendChild(h('div', { class: 'dp-tablewrap' }, table));
  }

  // --------------------------------------------------------------- WA-2: master toggle
  var MASTER_POLL_MS = 30000;
  var BLOCK_LABEL = {
    env_kill:        'dimatikan darurat oleh admin server',
    global_kill:     'dimatikan darurat oleh admin',
    no_gateway:      'gateway belum diatur (tab Setting)',
    gateway_off:     'Bot belum diaktifkan (tab Setting)',
    no_token:        'token belum diisi (tab Setting)',
    no_target:       'nomor tujuan belum diisi (tab Setting)',
    scope_orphan:    'akun/OLT untuk scope ini tidak aktif',
    locked_by_admin: 'dikunci oleh admin',
    master_off:      'master toggle OFF'
  };

  function scopeLabel(scope) {
    var opts = scopeOptions();
    for (var i = 0; i < opts.length; i++) if (opts[i].v === scope) return opts[i].label;
    return scope;
  }

  function buildMasterCard() {
    var chk = h('input', { type: 'checkbox', id: 'wa-master-chk', role: 'switch',
                           'aria-labelledby': 'wa-master-title', class: 'wa-switch-input' });
    chk.disabled = true;
    chk.addEventListener('change', onMasterToggle);
    return h('div', { id: 'wa-master-card', class: 'wa-master is-off' },
      h('div', { class: 'wa-master-row' },
        h('div', {},
          h('div', { id: 'wa-master-title', class: 'wa-master-title', text: '\uD83D\uDEA8 Alert WhatsApp Otomatis' }),
          h('div', { id: 'wa-master-scope', class: 'dp-hint' })),
        h('label', { class: 'wa-switch', 'for': 'wa-master-chk' },
          chk, h('span', { class: 'wa-switch-track' }))),
      h('div', { id: 'wa-master-line', class: 'dp-hint', 'aria-live': 'polite', text: 'Memuat status...' }));
  }

  function setMasterLine(text) {
    var el = q('wa-master-line');
    if (el) el.textContent = text;
  }

  function renderMaster(errMsg) {
    var card = q('wa-master-card'), chk = q('wa-master-chk');
    if (!card || !chk) return;
    var scopeEl = q('wa-master-scope');
    if (scopeEl) scopeEl.textContent = 'Scope: ' + scopeLabel(state.scope);

    var st = state.master;
    if (!st) {
      chk.disabled = true;
      card.className = 'wa-master is-off';
      setMasterLine(errMsg ? 'Gagal memuat status: ' + errMsg : 'Memuat status...');
      return;
    }
    var why = st.blocked_by;
    var label = BLOCK_LABEL[why] || '';
    var forcedOff = !!(st.env_override || st.global_kill || st.engine_locked);

    chk.checked = !!st.engine_enabled;
    chk.setAttribute('aria-checked', chk.checked ? 'true' : 'false');
    chk.disabled = !!state.masterBusy || (!st.engine_enabled && forcedOff);

    var text;
    if (st.effective) {
      card.className = 'wa-master is-on';
      text = 'Aktif. Alert otomatis dikirim sesuai rule yang menyala di tab Alert Rules.';
    } else if (st.engine_enabled) {
      card.className = 'wa-master is-warn';
      text = 'Dinyalakan, tapi belum bisa mengirim: ' + (label || why || 'alasan tidak diketahui') + '.';
    } else {
      card.className = 'wa-master is-off';
      text = 'Mati. Tidak ada alert otomatis. (Test Kirim tetap bisa dipakai.)' +
             (label ? ' Catatan: ' + label + '.' : '');
    }
    if (errMsg) text = 'Gagal memperbarui: ' + errMsg + '. ' + text;
    setMasterLine(text);
  }

  function loadMaster() {
    var scope = state.scope, seq = ++state.masterSeq;
    return call('/wa/alerts-status?scope=' + encodeURIComponent(scope))
      .then(function (st) {
        if (seq !== state.masterSeq || scope !== state.scope) return;
        state.master = st;
        renderMaster();
      })
      .catch(function (e) {
        if (seq !== state.masterSeq || scope !== state.scope) return;
        renderMaster((e && e.message) || 'error');
      });
  }

  function onMasterToggle(ev) {
    var chk = ev.target, want = chk.checked, scope = state.scope, st = state.master || {};
    if (want) {
      var n = (st.rules_enabled || []).length;
      var msg = n
        ? 'Alert WhatsApp otomatis akan dikirim untuk ' + n + ' rule yang aktif. Lanjutkan?'
        : 'Belum ada rule yang aktif di tab Alert Rules, jadi belum ada WA yang terkirim. Tetap nyalakan?';
      if (!window.confirm(msg)) { chk.checked = false; return; }
    }
    state.masterBusy = true;
    chk.disabled = true;
    setMasterLine(want ? 'Menyalakan...' : 'Mematikan...');

    call('/wa/engine?scope=' + encodeURIComponent(scope), 'PUT', { enabled: want })
      .then(function (res) {
        if (scope !== state.scope) return;
        state.master = Object.assign({}, state.master || {}, {
          engine_enabled: res.engine_enabled, engine_locked: res.engine_locked,
          effective: res.effective, blocked_by: res.blocked_by
        });
        state.masterBusy = false;
        renderMaster();
      })
      .catch(function (e) {
        chk.checked = !want;
        state.masterBusy = false;
        if (scope === state.scope) renderMaster((e && e.message) || 'error');
      })
      .then(function () {
        state.masterBusy = false;
        if (scope === state.scope) loadMaster();
      });
  }

  function stopMasterPoll() {
    if (state.masterTimer) { clearInterval(state.masterTimer); state.masterTimer = null; }
  }

  function startMasterPoll() {
    stopMasterPoll();
    state.masterTimer = setInterval(function () {
      if (!q('wa-master-card')) { stopMasterPoll(); return; }
      if (document.hidden || state.masterBusy) return;
      loadMaster();
    }, MASTER_POLL_MS);
  }

  // --------------------------------------------------------------- publik
  window.WaBot = {
    open: function () {
      state.seq++;
      if (!build()) return;
      call('/wa/providers').then(function (d) {
        state.providers = d.items;
        var sel = q('wa-provider');
        if (!sel) return;
        sel.textContent = '';
        d.items.forEach(function (p) { sel.appendChild(h('option', { value: p.id, text: p.label })); });
        loadAll();
      }).catch(function (e) {
        setBanner('error', 'Gagal memuat provider: ' + (e.message || 'error'));
      });
      switchTab(state.tab || 'setting');
    },

    // WA-2: expose untuk tab di halaman Alerts
    mountAlertLog: function (containerEl, scope) {
      if (!containerEl) return;
      var mySeq = ++state.seq;
      var myScope = scope || 'global';
      containerEl.textContent = '';
      containerEl.appendChild(h('div', { class: 'dp-hint', text: 'Memuat log...' }));
      call('/wa/alerts-log?limit=50&scope=' + encodeURIComponent(myScope))
        .then(function (res) {
          if (mySeq !== state.seq) return;
          renderAlertLog(containerEl, res, myScope);
        })
        .catch(function (e) {
          if (mySeq !== state.seq) return;
          containerEl.textContent = '';
          containerEl.appendChild(h('div', { class: 'dp-banner dp-banner-error',
            text: 'Gagal memuat log: ' + (e && e.message || 'error') }));
        });
    },
  };
})();
