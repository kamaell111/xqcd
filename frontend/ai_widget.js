/*!
 * ai_widget.js — Widget chat AI untuk aplikasi monitoring OLT
 *
 * Vanilla JS, tanpa dependensi, tidak menyentuh app.js / style.css / index.html.
 * Semua class berawalan "aiw-", semua id berawalan "aiw-".
 *
 * Pasang di index.html (setelah app.js):
 *   <script src="ai_widget.js?v=20261001"></script>
 *
 * Opsional, kalau API tidak di origin yang sama dengan frontend, set SEBELUM script ini:
 *   <script>window.AIW_API_BASE = "http://192.168.1.10:8000";</script>
 *
 * Debug lewat console: AIWidget.open(), AIWidget.close(), AIWidget.clear(), AIWidget.destroy()
 */
(function () {
  "use strict";

  // Cegah instance ganda (script ter-include dua kali / hot reload).
  if (window.__aiwLoaded) return;
  window.__aiwLoaded = true;

  /* ------------------------------------------------------------------ *
   * Konfigurasi
   * ------------------------------------------------------------------ */
  var CFG = {
    apiBase: String(window.AIW_API_BASE || "").replace(/\/+$/, ""),
    askPath: "/api/v1/ai/ask",
    statusPath: "/api/v1/ai/status",
    mainScreenId: "main-screen",   // widget hanya tampil saat elemen ini tidak ber-class hiddenClass
    hiddenClass: "hidden",
    timeoutMs: 30000,              // batas tunggu jawaban
    maxQuestion: 300,              // panjang pertanyaan maksimum (sesuaikan dengan batas backend)
    maxStored: 40,                 // jumlah pesan yang disimpan di localStorage
    pollMs: 2000,                  // jaring pengaman deteksi login/logout
    storagePrefix: "aiw:history:"  // + user.id
  };

  var T = {
    title: "Asisten OLT",
    open: "Buka asisten OLT",
    minimize: "Minimalkan asisten",
    clear: "Hapus percakapan",
    send: "Kirim pertanyaan",
    inputLabel: "Pertanyaan untuk asisten",
    placeholder: "Tulis pertanyaan, Enter untuk kirim",
    emptyText: "Tanya data OLT dengan kalimat biasa.",
    typing: "Asisten sedang mencari jawaban",
    cleared: "Percakapan dihapus",
    retry: "Coba lagi",
    noAnswer: "Asisten tidak menghasilkan jawaban. Coba ubah pertanyaannya.",
    you: "Anda: ",
    bot: "Asisten: ",
    fromSource: "Data dari ",
    cached: "Dari cache",
    degraded: "Mode terbatas",
    degradedTip: "Sebagian sumber data tidak bisa dijangkau, jawaban mungkin tidak lengkap."
  };

  var SUGGESTIONS = [
    "Berapa ONU online?",
    "ONU dengan redaman terburuk",
    "Alert apa yang aktif?",
    "Status OLT"
  ];

  var ERR_NETWORK = { text: "Tidak bisa terhubung ke server. Periksa koneksi, lalu coba lagi.", retry: true };
  var ERR_TIMEOUT = { text: "Jawaban terlalu lama (lebih dari 30 detik). Coba lagi.", retry: true };

  function errorFor(status) {
    switch (status) {
      case 401: return { text: "Sesi login berakhir. Login ulang, lalu kirim pertanyaan lagi.", retry: false };
      case 403: return { text: "Akun ini tidak punya akses ke asisten.", retry: false };
      case 404: return { text: "Fitur asisten belum tersedia di server ini.", retry: false };
      case 422: return { text: "Pertanyaan tidak bisa diproses. Coba tulis dengan kalimat lain.", retry: false };
      case 429: return { text: "Terlalu banyak pertanyaan dalam waktu singkat. Tunggu sebentar, lalu coba lagi.", retry: true };
      case 503: return { text: "Layanan AI sedang tidak tersedia. Coba lagi beberapa saat lagi.", retry: true };
      default:
        if (status >= 500) return { text: "Terjadi kesalahan di server (kode " + status + "). Coba lagi.", retry: true };
        return { text: "Permintaan gagal (kode " + status + ").", retry: false };
    }
  }

  /* ------------------------------------------------------------------ *
   * CSS (memakai variabel design system yang sudah ada, dengan fallback)
   * ------------------------------------------------------------------ */
  var CSS = `
.aiw-root{
  --aiw-bg:var(--surface,#0b0e17);
  --aiw-bg-2:var(--surface-2,#10141f);
  --aiw-line:var(--border,rgba(255,255,255,.07));
  --aiw-text:var(--text,#f4f6fc);
  --aiw-text-2:var(--text-2,#99a2ba);
  --aiw-dim:var(--text-dim,#5a6178);
  --aiw-accent:var(--accent,#2dd4ee);
  --aiw-green:var(--green,#33e0a3);
  --aiw-amber:var(--amber,#fbbf3f);
  --aiw-red:var(--red,#fb5468);
  --aiw-r:var(--r-md,14px);
  --aiw-r-lg:var(--r-lg,20px);
  --aiw-ui:var(--font-ui,'Inter',system-ui,sans-serif);
  --aiw-mono:var(--font-mono,'JetBrains Mono',ui-monospace,monospace);
  font-family:var(--aiw-ui);
  color:var(--aiw-text);
}
.aiw-root[hidden]{display:none!important}
.aiw-root *,.aiw-root *::before,.aiw-root *::after{box-sizing:border-box}
.aiw-root button{font:inherit;color:inherit}

.aiw-sr{position:absolute!important;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}

/* FAB */
.aiw-fab{
  position:fixed;z-index:900;
  right:calc(20px + env(safe-area-inset-right,0px));
  bottom:calc(20px + env(safe-area-inset-bottom,0px));
  width:52px;height:52px;border-radius:50%;border:0;padding:0;cursor:pointer;
  display:grid;place-items:center;
  background:var(--aiw-accent);color:#041016;
  box-shadow:0 8px 24px rgba(0,0,0,.35);
  transition:transform .15s ease;
}
.aiw-fab:hover{transform:scale(1.05)}
.aiw-fab:active{transform:scale(.97)}
.aiw-fab:focus-visible,.aiw-btn:focus-visible,.aiw-chip-btn:focus-visible,.aiw-retry:focus-visible,.aiw-send:focus-visible{
  outline:2px solid var(--aiw-accent);outline-offset:3px;
}
.aiw-fab svg{width:24px;height:24px}
.aiw-dot{
  position:absolute;top:2px;right:2px;width:12px;height:12px;border-radius:50%;
  background:var(--aiw-green);border:2px solid var(--aiw-bg);
}
.aiw-dot[hidden]{display:none}

/* Panel */
.aiw-panel{
  position:fixed;z-index:950;
  right:calc(20px + env(safe-area-inset-right,0px));
  bottom:calc(84px + env(safe-area-inset-bottom,0px));
  width:380px;max-width:calc(100vw - 24px);
  height:560px;max-height:calc(100vh - 108px);
  max-height:calc(100dvh - 108px);
  display:flex;flex-direction:column;
  background:var(--aiw-bg);
  border:1px solid var(--aiw-line);
  border-radius:var(--aiw-r-lg);
  box-shadow:0 18px 48px rgba(0,0,0,.4);
  overflow:hidden;
}
.aiw-panel[hidden]{display:none}
.aiw-panel:not([hidden]){animation:aiw-in .16s ease-out}
@keyframes aiw-in{from{opacity:0;transform:translateY(8px) scale(.98)}to{opacity:1;transform:none}}

.aiw-head{
  display:flex;align-items:center;gap:8px;
  padding:12px 12px 12px 16px;border-bottom:1px solid var(--aiw-line);
}
.aiw-title{flex:1;font-size:15px;font-weight:600;letter-spacing:-.01em}
.aiw-btn{
  width:32px;height:32px;display:grid;place-items:center;border-radius:10px;
  border:0;background:transparent;color:var(--aiw-text-2);cursor:pointer;padding:0;
}
.aiw-btn:hover{background:var(--aiw-bg-2);color:var(--aiw-text)}
.aiw-btn svg{width:18px;height:18px}

.aiw-body{flex:1;min-height:0;overflow-y:auto;padding:16px 14px;display:flex;flex-direction:column;gap:12px;overscroll-behavior:contain}

/* Keadaan kosong */
.aiw-empty{display:flex;flex-direction:column;gap:6px;padding:8px 2px 0}
.aiw-empty[hidden]{display:none}
.aiw-empty-title{margin:0;font-size:17px;font-weight:600;letter-spacing:-.01em;overflow-wrap:anywhere}
.aiw-empty-text{margin:0 0 10px;font-size:13px;line-height:1.45;color:var(--aiw-text-2)}
.aiw-suggest{display:flex;flex-wrap:wrap;gap:8px}
.aiw-chip-btn{
  border:1px solid var(--aiw-line);background:var(--aiw-bg-2);color:var(--aiw-text);
  border-radius:999px;padding:7px 12px;font-size:13px;cursor:pointer;
}
.aiw-chip-btn:hover{border-color:var(--aiw-accent)}

/* Pesan */
.aiw-log{display:flex;flex-direction:column;gap:12px}
.aiw-msg{display:flex;flex-direction:column;gap:6px;max-width:88%}
.aiw-msg--user{align-self:flex-end;align-items:flex-end}
.aiw-msg--ai{align-self:flex-start;align-items:flex-start}
.aiw-bubble{
  position:relative;padding:9px 12px;border-radius:var(--aiw-r);
  font-size:14px;line-height:1.5;color:var(--aiw-text);
  white-space:pre-wrap;overflow-wrap:anywhere;
}
.aiw-msg--user .aiw-bubble{
  background:rgba(45,212,238,.14);
  background:color-mix(in srgb,var(--aiw-accent) 16%,transparent);
  border-bottom-right-radius:4px;
}
.aiw-msg--ai .aiw-bubble{
  background:var(--aiw-bg-2);border:1px solid var(--aiw-line);border-bottom-left-radius:4px;
}
.aiw-meta{display:flex;flex-wrap:wrap;gap:6px}
.aiw-tag{
  font-size:11px;line-height:1;padding:4px 8px;border-radius:999px;
  border:1px solid var(--aiw-line);color:var(--aiw-text-2);
}
.aiw-tag--mono{font-family:var(--aiw-mono)}
.aiw-tag--warn{color:var(--aiw-amber);border-color:rgba(251,191,63,.4)}

/* Indikator mengetik */
.aiw-typing{display:flex;align-items:center;gap:4px;padding:13px 14px}
.aiw-typing i{width:6px;height:6px;border-radius:50%;background:var(--aiw-text-2);animation:aiw-blink 1.1s infinite ease-in-out both}
.aiw-typing i:nth-child(3){animation-delay:.16s}
.aiw-typing i:nth-child(4){animation-delay:.32s}
@keyframes aiw-blink{0%,80%,100%{opacity:.25;transform:scale(.8)}40%{opacity:1;transform:scale(1)}}

/* Error */
.aiw-err{
  align-self:stretch;padding:10px 12px;border-radius:10px;font-size:13px;line-height:1.45;
  border:1px solid rgba(251,84,104,.35);border-left:3px solid var(--aiw-red);
  background:rgba(251,84,104,.08);color:var(--aiw-text);
}
.aiw-retry{
  display:inline-block;margin-top:8px;padding:5px 10px;border-radius:8px;cursor:pointer;
  border:1px solid var(--aiw-line);background:var(--aiw-bg-2);font-size:12px;
}
.aiw-retry:hover{border-color:var(--aiw-accent)}

/* Input */
.aiw-compose{display:flex;gap:8px;align-items:flex-end;padding:10px 12px 12px;border-top:1px solid var(--aiw-line)}
.aiw-input{
  flex:1;min-width:0;resize:none;min-height:40px;max-height:120px;
  padding:9px 12px;border-radius:12px;border:1px solid var(--aiw-line);
  background:var(--aiw-bg-2);color:var(--aiw-text);
  font:inherit;font-size:14px;line-height:1.4;
}
.aiw-input::placeholder{color:var(--aiw-dim)}
.aiw-input:focus-visible{outline:2px solid var(--aiw-accent);outline-offset:1px}
.aiw-send{
  width:40px;height:40px;flex:none;border-radius:12px;border:0;padding:0;cursor:pointer;
  display:grid;place-items:center;background:var(--aiw-accent);color:#041016;
}
.aiw-send svg{width:18px;height:18px}
.aiw-send:disabled{opacity:.4;cursor:not-allowed}

/* Mobile: panel penuh layar */
@media (max-width:480px){
  .aiw-panel{
    left:0;right:0;top:0;bottom:0;width:auto;max-width:none;height:auto;max-height:none;
    border:0;border-radius:0;
  }
  .aiw-head{padding-top:calc(12px + env(safe-area-inset-top,0px))}
  .aiw-compose{padding-bottom:calc(12px + env(safe-area-inset-bottom,0px))}
  .aiw-open .aiw-fab{display:none}
  .aiw-input{font-size:16px}
}

@media (prefers-reduced-motion:reduce){
  .aiw-fab,.aiw-panel:not([hidden]){animation:none;transition:none}
  .aiw-disabled .aiw-fab {
    background: linear-gradient(135deg, #6b7280, #4b5563) !important;
    opacity: .85;
  }
  .aiw-disabled .aiw-fab::after {
    content: "!";
    position: absolute;
    top: -4px;
    right: -4px;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: #f59e0b;
    color: #fff;
    font-size: 11px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
  }
  .aiw-disabled .aiw-input {
    background: rgba(255,255,255,.03) !important;
    color: #6b7280 !important;
    cursor: not-allowed;
  }
  .aiw-disabled .aiw-send {
    opacity: .4;
    cursor: not-allowed;
  }
  .aiw-disabled .aiw-empty-title {
    color: #f59e0b !important;
  }
  .aiw-typing i{animation:none;opacity:.6}
}
@media print{.aiw-root{display:none!important}}
`;

  /* ------------------------------------------------------------------ *
   * Ikon (SVG inline statis, bukan input pengguna)
   * ------------------------------------------------------------------ */
  function icon(inner) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + inner + "</svg>";
  }
  var ICON_CHAT = icon('<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1.1-4.2A8 8 0 1 1 21 12z"/><path d="M9 11h6M9 14.5h3.5"/>');
  var ICON_DOWN = icon('<path d="M6 9l6 6 6-6"/>');
  var ICON_TRASH = icon('<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/>');
  var ICON_SEND = icon('<path d="M20 4L4 11l6.5 2.5L13 20z"/>');

  /* ------------------------------------------------------------------ *
   * Util
   * ------------------------------------------------------------------ */
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function getToken() {
    try { return localStorage.getItem("token"); } catch (e) { return null; }
  }
  function getUser() {
    try { return JSON.parse(localStorage.getItem("user") || "null"); } catch (e) { return null; }
  }
  function userKey() {
    var u = getUser();
    return CFG.storagePrefix + (u && u.id != null ? u.id : "anon");
  }

  function fmtLatency(ms) {
    if (typeof ms !== "number" || !isFinite(ms)) return "";
    return ms < 1000 ? Math.round(ms) + " ms" : (ms / 1000).toFixed(1).replace(".", ",") + " dtk";
  }

  /* ------------------------------------------------------------------ *
   * State
   * ------------------------------------------------------------------ */
  var state = {
    visible: false,   // main-screen aktif + ada token
    enabled: true,    // hasil /ai/status (false hanya jika server bilang enabled:false)
    open: false,
    pending: false,
    unread: false,
    key: null,        // key localStorage riwayat untuk user aktif
    messages: [],
    gen: 0,           // naik tiap logout/clear, untuk membuang respons basi
    abort: null,
    lastQuestion: "",
    errEl: null,
    typingEl: null,
    mo: null,
    timer: null
  };

  var listeners = [];
  function on(target, type, fn, opts) {
    target.addEventListener(type, fn, opts);
    listeners.push([target, type, fn, opts]);
  }

  /* ------------------------------------------------------------------ *
   * DOM
   * ------------------------------------------------------------------ */
  var styleEl, root, fab, dot, panel, titleEl, clearBtn, minBtn, body, empty, emptyTitle, log, input, sendBtn, srStatus;

  function build() {
    styleEl = el("style");
    styleEl.id = "aiw-style";
    styleEl.textContent = CSS;
    document.head.appendChild(styleEl);

    root = el("div", "aiw-root");
    root.id = "aiw-root";
    root.hidden = true;

    fab = el("button", "aiw-fab");
    fab.type = "button";
    fab.setAttribute("aria-label", T.open);
    fab.setAttribute("aria-expanded", "false");
    fab.setAttribute("aria-controls", "aiw-panel");
    fab.innerHTML = ICON_CHAT;
    dot = el("span", "aiw-dot");
    dot.hidden = true;
    fab.appendChild(dot);

    panel = el("section", "aiw-panel");
    panel.id = "aiw-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-labelledby", "aiw-title");
    panel.hidden = true;

    var head = el("div", "aiw-head");
    titleEl = el("div", "aiw-title", T.title);
    titleEl.id = "aiw-title";
    clearBtn = el("button", "aiw-btn");
    clearBtn.type = "button";
    clearBtn.title = T.clear;
    clearBtn.setAttribute("aria-label", T.clear);
    clearBtn.innerHTML = ICON_TRASH;
    minBtn = el("button", "aiw-btn");
    minBtn.type = "button";
    minBtn.title = T.minimize;
    minBtn.setAttribute("aria-label", T.minimize);
    minBtn.innerHTML = ICON_DOWN;
    head.appendChild(titleEl);
    head.appendChild(clearBtn);
    head.appendChild(minBtn);

    body = el("div", "aiw-body");
    empty = el("div", "aiw-empty");
    emptyTitle = el("p", "aiw-empty-title");
    var emptyText = el("p", "aiw-empty-text", T.emptyText);
    var suggest = el("div", "aiw-suggest");
    SUGGESTIONS.forEach(function (s) {
      var b = el("button", "aiw-chip-btn", s);
      b.type = "button";
      b.addEventListener("click", function () { submit(s); });
      suggest.appendChild(b);
    });
    empty.appendChild(emptyTitle);
    empty.appendChild(emptyText);
    empty.appendChild(suggest);

    log = el("div", "aiw-log");
    log.setAttribute("role", "log");
    log.setAttribute("aria-live", "polite");
    log.setAttribute("aria-relevant", "additions");
    log.setAttribute("aria-label", "Percakapan");
    body.appendChild(empty);
    body.appendChild(log);

    var compose = el("div", "aiw-compose");
    input = el("textarea", "aiw-input");
    input.id = "aiw-input";
    input.rows = 1;
    input.maxLength = CFG.maxQuestion;
    input.placeholder = T.placeholder;
    input.setAttribute("aria-label", T.inputLabel);
    input.setAttribute("autocomplete", "off");
    sendBtn = el("button", "aiw-send");
    sendBtn.type = "button";
    sendBtn.title = T.send;
    sendBtn.setAttribute("aria-label", T.send);
    sendBtn.innerHTML = ICON_SEND;
    sendBtn.disabled = true;
    compose.appendChild(input);
    compose.appendChild(sendBtn);

    srStatus = el("div", "aiw-sr");
    srStatus.setAttribute("role", "status");

    panel.appendChild(head);
    panel.appendChild(body);
    panel.appendChild(compose);
    panel.appendChild(srStatus);
    root.appendChild(fab);
    root.appendChild(panel);
    document.body.appendChild(root);
  }

  /* ------------------------------------------------------------------ *
   * Riwayat (localStorage per user)
   * ------------------------------------------------------------------ */
  function validMsg(m) {
    return m && (m.role === "user" || m.role === "ai") && typeof m.text === "string";
  }
  function loadHistory() {
    try {
      var a = JSON.parse(localStorage.getItem(state.key) || "[]");
      return Array.isArray(a) ? a.filter(validMsg).slice(-CFG.maxStored) : [];
    } catch (e) { return []; }
  }
  function saveHistory() {
    try {
      localStorage.setItem(state.key, JSON.stringify(state.messages.slice(-CFG.maxStored)));
    } catch (e) { /* kuota penuh / mode privat: abaikan, chat tetap jalan di memori */ }
  }
  function purgeAllHistory() {
    try {
      var del = [];
      for (var i = 0; i < localStorage.length; i++) {
        var k = localStorage.key(i);
        if (k && k.indexOf(CFG.storagePrefix) === 0) del.push(k);
      }
      del.forEach(function (k) { localStorage.removeItem(k); });
    } catch (e) { /* abaikan */ }
  }

  /* ------------------------------------------------------------------ *
   * Render
   * ------------------------------------------------------------------ */
  function scrollEnd() {
    body.scrollTop = body.scrollHeight;
  }

  function msgEl(m) {
    var isUser = m.role === "user";
    var wrap = el("div", "aiw-msg " + (isUser ? "aiw-msg--user" : "aiw-msg--ai"));
    var bubble = el("div", "aiw-bubble");
    bubble.appendChild(el("span", "aiw-sr", isUser ? T.you : T.bot));
    bubble.appendChild(document.createTextNode(m.text)); // textContent saja: aman dari XSS
    wrap.appendChild(bubble);

    if (!isUser) {
      var meta = el("div", "aiw-meta");
      if (m.source) meta.appendChild(el("span", "aiw-tag", T.fromSource + m.source));
      var lat = fmtLatency(m.ms);
      if (lat) meta.appendChild(el("span", "aiw-tag aiw-tag--mono", lat));
      if (m.cached) meta.appendChild(el("span", "aiw-tag", T.cached));
      if (m.degraded) {
        var w = el("span", "aiw-tag aiw-tag--warn", T.degraded);
        w.title = T.degradedTip;
        meta.appendChild(w);
      }
      if (meta.childNodes.length) wrap.appendChild(meta);
    }
    return wrap;
  }

  function updateEmpty() {
    // Kalau AI belum dikonfigurasi, tampilkan notice — bukan empty state biasa
    if (!state.enabled) {
      empty.hidden = false;
      emptyTitle.textContent = "AI Assistant belum dikonfigurasi";
      var body = document.getElementById("aiw-empty-text");
      if (!body) {
        body = empty.querySelector(".aiw-empty-text");
      }
      if (body) {
        body.textContent = "Isi GEMINI_API_KEY atau GROQ_API_KEY di file .env backend, lalu restart uvicorn.";
      }
      // Sembunyikan suggestions
      var sug = empty.querySelector(".aiw-suggest");
      if (sug) sug.hidden = true;
      return;
    }
    // Normal
    empty.hidden = state.messages.length > 0;
    var sugN = empty.querySelector(".aiw-suggest");
    if (sugN) sugN.hidden = false;
    updateGreeting();
  }

  function updateGreeting() {
    var u = getUser();
    emptyTitle.textContent = u && u.username ? "Halo, " + u.username : "Halo";
  }

  function renderAll() {
    log.textContent = "";
    state.errEl = null;
    state.typingEl = null;
    state.messages.forEach(function (m) { log.appendChild(msgEl(m)); });
    if (state.pending) showTyping();
    updateEmpty();
    updateGreeting();
    scrollEnd();
  }

  function addMessage(m) {
    state.messages.push(m);
    saveHistory();
    // sisipkan sebelum indikator mengetik bila ada
    var node = msgEl(m);
    if (state.typingEl && state.typingEl.parentNode === log) log.insertBefore(node, state.typingEl);
    else log.appendChild(node);
    updateEmpty();
    scrollEnd();
    if (m.role === "ai" && !state.open) {
      state.unread = true;
      dot.hidden = false;
    }
  }

  function showTyping() {
    if (state.typingEl) return;
    var wrap = el("div", "aiw-msg aiw-msg--ai");
    var b = el("div", "aiw-bubble aiw-typing");
    b.appendChild(el("span", "aiw-sr", T.typing));
    b.appendChild(el("i"));
    b.appendChild(el("i"));
    b.appendChild(el("i"));
    wrap.appendChild(b);
    state.typingEl = wrap;
    log.appendChild(wrap);
    scrollEnd();
  }
  function hideTyping() {
    if (state.typingEl && state.typingEl.parentNode) state.typingEl.parentNode.removeChild(state.typingEl);
    state.typingEl = null;
  }

  function clearError() {
    if (state.errEl && state.errEl.parentNode) state.errEl.parentNode.removeChild(state.errEl);
    state.errEl = null;
  }
  function showError(err) {
    clearError();
    var box = el("div", "aiw-err");
    box.setAttribute("role", "alert");
    box.appendChild(document.createTextNode(err.text));
    if (err.retry && state.lastQuestion) {
      var b = el("button", "aiw-retry", T.retry);
      b.type = "button";
      b.addEventListener("click", function () { submit(state.lastQuestion, true); });
      box.appendChild(document.createElement("br"));
      box.appendChild(b);
    }
    state.errEl = box;
    log.appendChild(box);
    scrollEnd();
  }

  function updateSend() {
    if (!state.enabled) {
      if (input) { input.disabled = true; input.placeholder = 'AI belum dikonfigurasi'; }
      if (sendBtn) sendBtn.disabled = true;
      return;
    }
    if (input) input.disabled = false;
    if (sendBtn) sendBtn.disabled = false;
    sendBtn.disabled = state.pending || !input.value.trim();
  }
  function setPending(v) {
    state.pending = v;
    log.setAttribute("aria-busy", v ? "true" : "false");
    if (v) showTyping(); else hideTyping();
    updateSend();
  }
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  }
  function announce(text) {
    srStatus.textContent = "";
    setTimeout(function () { srStatus.textContent = text; }, 30);
  }

  /* ------------------------------------------------------------------ *
   * Buka / tutup
   * ------------------------------------------------------------------ */
  function openPanel() {
    if (!state.visible) return;
    // Kalau disabled, tetap boleh dibuka — panel menampilkan notice
    state.open = true;
    state.unread = false;
    dot.hidden = true;
    panel.hidden = false;
    root.classList.add("aiw-open");
    fab.setAttribute("aria-expanded", "true");
    fab.setAttribute("aria-label", T.minimize);
    scrollEnd();
    input.focus();
  }
  function closePanel(returnFocus) {
    state.open = false;
    panel.hidden = true;
    root.classList.remove("aiw-open");
    fab.setAttribute("aria-expanded", "false");
    fab.setAttribute("aria-label", T.open);
    if (returnFocus) fab.focus();
  }
  function togglePanel() {
    if (state.open) closePanel(false); else openPanel();
  }

  function cancelPending() {
    state.gen++;
    if (state.abort) { try { state.abort.abort(); } catch (e) { /* abaikan */ } }
    state.abort = null;
    setPending(false);
  }

  function clearChat() {
    cancelPending();
    clearError();
    state.messages = [];
    state.lastQuestion = "";
    saveHistory();
    renderAll();
    announce(T.cleared);
    input.focus();
  }

  /* ------------------------------------------------------------------ *
   * Kirim pertanyaan
   * ------------------------------------------------------------------ */
  function submit(text, isRetry) {
    var q = String(text != null ? text : input.value).trim();
    if (!q || state.pending) return;
    if (q.length > CFG.maxQuestion) q = q.slice(0, CFG.maxQuestion);
    if (!getToken()) { sync(); return; }

    clearError();
    if (!isRetry) {
      addMessage({ role: "user", text: q });
      input.value = "";
      autosize();
    }
    state.lastQuestion = q;
    ask(q);
  }

  async function ask(q) {
    var gen = state.gen;
    var ctrl = new AbortController();
    var timedOut = false;
    var timer = setTimeout(function () { timedOut = true; ctrl.abort(); }, CFG.timeoutMs);
    state.abort = ctrl;
    setPending(true);

    try {
      var res = await fetch(CFG.apiBase + CFG.askPath, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer " + (getToken() || "")
        },
        body: JSON.stringify({ question: q }),
        signal: ctrl.signal
      });
      var data = null;
      try { data = await res.json(); } catch (e) { /* body bukan JSON */ }
      if (gen !== state.gen) return; // logout / clear saat menunggu: buang hasil

      if (!res.ok) { showError(errorFor(res.status)); return; }

      // Write tool: tampilkan kartu konfirmasi, bukan bubble teks
      if (data && data.needs_confirm) {
        addConfirmCard(data);
        return;
      }

      var answer = data && typeof data.answer === "string" ? data.answer.trim() : "";
      addMessage({
        role: "ai",
        text: answer || T.noAnswer,
        source: data && data.source ? String(data.source) : "",
        ms: data ? data.latency_ms : null,
        cached: !!(data && data.cached),
        degraded: !!(data && data.degraded)
      });
    } catch (err) {
      if (gen !== state.gen) return;
      showError(timedOut ? ERR_TIMEOUT : ERR_NETWORK);
    } finally {
      clearTimeout(timer);
      if (gen === state.gen) {
        state.abort = null;
        setPending(false);
      }
    }
  }

  /* ------------------------------------------------------------------ *
   * Login / logout / visibilitas
   * ------------------------------------------------------------------ */
  /* ------------------------------------------------------------------ *
   * Kartu konfirmasi aksi tulis (write tool)
   * ------------------------------------------------------------------ */
  var AIW_CONFIRM_CSS = `
.aiw-confirm-card{margin:8px 0;padding:12px 14px;border:1px solid var(--aiw-line);
  border-left:4px solid var(--aiw-warn,#f59e0b);border-radius:var(--aiw-r,14px);
  background:var(--aiw-bg-2,#10141f);font-size:13px;line-height:1.45}
.aiw-confirm-card[data-kind="add"]{border-left-color:var(--aiw-accent,#2dd4ee)}
.aiw-confirm-card[data-kind="danger"]{border-left-color:var(--aiw-red,#fb5468)}
.aiw-confirm-card[data-kind="warn"]{border-left-color:var(--aiw-amber,#fbbf3f)}
.aiw-confirm-card[data-state="cancelled"]{opacity:.55}
.aiw-confirm-title{font-weight:600;font-size:14px;margin-bottom:8px;overflow-wrap:anywhere}
.aiw-confirm-details{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;margin-bottom:8px}
.aiw-confirm-key{opacity:.65;font-size:12px}
.aiw-confirm-val{word-break:break-all;font-family:var(--aiw-mono,monospace);font-size:12.5px}
.aiw-confirm-warn{color:var(--aiw-amber,#fbbf3f);margin:3px 0;font-size:12.5px}
.aiw-confirm-actions{display:flex;gap:8px;margin-top:10px}
.aiw-confirm-btn{padding:6px 12px;border-radius:8px;border:1px solid var(--aiw-line);
  background:transparent;color:inherit;cursor:pointer;font:inherit;font-size:13px}
.aiw-confirm-btn:hover{border-color:var(--aiw-accent)}
.aiw-confirm-btn-primary{background:var(--aiw-accent,#2dd4ee);border-color:var(--aiw-accent,#2dd4ee);
  color:#041016;font-weight:600}
.aiw-confirm-card[data-kind="danger"] .aiw-confirm-btn-primary{background:var(--aiw-red,#fb5468);
  border-color:var(--aiw-red,#fb5468);color:#fff}
.aiw-confirm-btn:disabled{opacity:.5;cursor:not-allowed}
.aiw-confirm-status{margin-top:8px;font-size:12px;opacity:.9}
.aiw-confirm-status.aiw-confirm-err{color:var(--aiw-red,#fb5468);opacity:1}
`;

  function aiwInjectConfirmCss() {
    if (document.getElementById("aiw-confirm-css")) return;
    var st = document.createElement("style");
    st.id = "aiw-confirm-css";
    st.textContent = AIW_CONFIRM_CSS;
    document.head.appendChild(st);
  }

  function addConfirmCard(d) {
    aiwInjectConfirmCss();

    var card = el("div", "aiw-confirm-card");
    card.dataset.kind = d.kind || "warn";
    card.setAttribute("role", "group");

    var icons = { add: "\u2795", danger: "\u{1F534}", warn: "\u26A0" };
    card.appendChild(el("div", "aiw-confirm-title",
      (icons[d.kind] || "") + " " + (d.title || "Konfirmasi aksi")));

    var grid = el("div", "aiw-confirm-details");
    var details = d.details || {};
    Object.keys(details).forEach(function (k) {
      grid.appendChild(el("div", "aiw-confirm-key", k));
      grid.appendChild(el("div", "aiw-confirm-val", String(details[k])));
    });
    card.appendChild(grid);

    (d.warnings || []).forEach(function (w) {
      card.appendChild(el("div", "aiw-confirm-warn", "\u26A0 " + w));
    });

    var actions = el("div", "aiw-confirm-actions");
    var btnYes = el("button", "aiw-confirm-btn aiw-confirm-btn-primary",
      "\u2713 Ya, " + (d.confirm_label || "Lanjut"));
    var btnNo = el("button", "aiw-confirm-btn", "Batal");
    btnYes.type = "button";
    btnNo.type = "button";
    actions.appendChild(btnYes);
    actions.appendChild(btnNo);
    card.appendChild(actions);

    var status = el("div", "aiw-confirm-status");
    status.hidden = true;
    card.appendChild(status);

    function setStatus(txt, isErr) {
      status.hidden = false;
      status.textContent = txt;
      status.classList.toggle("aiw-confirm-err", !!isErr);
    }
    function lock() { btnYes.disabled = true; btnNo.disabled = true; }

    btnYes.addEventListener("click", async function () {
      lock();
      setStatus("\u23F3 Mengirim...");
      try {
        var r = await fetch(CFG.apiBase + "/api/v1/ai/confirm", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + (getToken() || "")
          },
          body: JSON.stringify({ confirm_token: d.confirm_token })
        });
        var rd = await r.json().catch(function () { return {}; });
        if (!r.ok || !rd.ok) {
          setStatus("\u274C " + (rd.error || ("HTTP " + r.status)), true);
          return;
        }
        if (rd.job_id) {
          setStatus("\u25B6 Dijalankan. Cek job card di bawah.");
        } else {
          setStatus("\u2705 " + (d.done_text || "Selesai"));
        }
      } catch (e) {
        setStatus("\u274C " + e.message, true);
      }
    });

    btnNo.addEventListener("click", async function () {
      lock();
      try {
        await fetch(CFG.apiBase + "/api/v1/ai/cancel", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + (getToken() || "")
          },
          body: JSON.stringify({ confirm_token: d.confirm_token })
        });
      } catch (e) { /* ignore */ }
      actions.remove();
      card.dataset.state = "cancelled";
      setStatus("Dibatalkan");
    });

    // Append ke container chat
    log.appendChild(card);
    scrollEnd();
    if (!state.open) {
      state.unread = true;
      dot.hidden = false;
    }
  }

  function mainScreenActive() {
    var ms = document.getElementById(CFG.mainScreenId);
    if (!ms) return true; // tidak ada elemen: andalkan token saja
    return !ms.classList.contains(CFG.hiddenClass) && !ms.hidden;
  }
  function shouldShow() {
    return !!getToken() && mainScreenActive();
  }

  function applyVisibility() {
    // Widget selalu tampil kalau sudah login (state.visible).
    // Kalau AI belum dikonfigurasi (state.enabled=false), panel akan
    // menampilkan pesan khusus dan input di-disable.
    var show = state.visible;
    root.hidden = !show;
    root.classList.toggle("aiw-disabled", !state.enabled);
    if (!show && state.open) closePanel(false);
  }

  function checkStatus() {
    var token = getToken();
    if (!token) return;
    fetch(CFG.apiBase + CFG.statusPath, { headers: { "Authorization": "Bearer " + token } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && d.enabled === false) {
          state.enabled = false;
        } else {
          state.enabled = true;
        }
        applyVisibility();
        updateEmpty();
        updateSend();
      })
      .catch(function () { /* abaikan */ });
  }

  function onLogin() {
    state.visible = true;
    state.enabled = true;
    state.key = userKey();
    state.messages = loadHistory();
    renderAll();
    applyVisibility();
    checkStatus();
  }

  function onLogout() {
    cancelPending();
    clearError();
    state.visible = false;
    state.enabled = true;
    state.unread = false;
    dot.hidden = true;
    state.messages = [];
    state.lastQuestion = "";
    input.value = "";
    autosize();
    updateSend();
    purgeAllHistory();
    renderAll();
    applyVisibility();
  }

  function sync() {
    var show = shouldShow();
    if (show && !state.visible) {
      onLogin();
    } else if (!show && state.visible) {
      onLogout();
    } else if (show && state.visible && userKey() !== state.key) {
      // ganti akun tanpa lewat keadaan "tersembunyi" yang sempat terdeteksi
      onLogout();
      onLogin();
    }
  }

  function watchMainScreen() {
    var ms = document.getElementById(CFG.mainScreenId);
    if (!ms || state.mo || typeof MutationObserver === "undefined") return;
    state.mo = new MutationObserver(sync);
    state.mo.observe(ms, { attributes: true, attributeFilter: ["class", "style", "hidden"] });
  }

  /* ------------------------------------------------------------------ *
   * Init & teardown
   * ------------------------------------------------------------------ */
  function init() {
    build();

    on(fab, "click", togglePanel);
    on(minBtn, "click", function () { closePanel(true); });
    on(clearBtn, "click", clearChat);
    on(sendBtn, "click", function () { submit(); });

    on(input, "input", function () { autosize(); updateSend(); });
    on(input, "keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        submit();
      }
    });

    // Esc hanya berlaku saat fokus di dalam panel, dan tidak diteruskan ke handler Esc milik app.js.
    on(panel, "keydown", function (e) {
      if (e.key === "Escape") {
        e.stopPropagation();
        closePanel(true);
      }
    });

    // Tab lain: login/logout, atau riwayat berubah.
    on(window, "storage", function (e) {
      if (e.key === null || e.key === "token" || e.key === "user") { sync(); return; }
      if (state.visible && e.key === state.key) {
        state.messages = loadHistory();
        renderAll();
      }
    });
    on(document, "visibilitychange", function () { if (!document.hidden) sync(); });

    watchMainScreen();
    // Jaring pengaman: logout/login di tab yang sama mengubah localStorage tanpa event "storage".
    state.timer = setInterval(function () {
      if (!state.mo) watchMainScreen();
      sync();
    }, CFG.pollMs);

    sync();
  }

  function destroy() {
    cancelPending();
    if (state.mo) state.mo.disconnect();
    clearInterval(state.timer);
    listeners.forEach(function (l) { l[0].removeEventListener(l[1], l[2], l[3]); });
    listeners = [];
    if (root && root.parentNode) root.parentNode.removeChild(root);
    if (styleEl && styleEl.parentNode) styleEl.parentNode.removeChild(styleEl);
    delete window.AIWidget;
    window.__aiwLoaded = false;
  }

  window.AIWidget = { open: openPanel, close: function () { closePanel(false); }, clear: clearChat, destroy: destroy };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
