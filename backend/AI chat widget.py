<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Asisten NOC - widget</title>
<style>
/* Semua kelas berawalan ai- agar tidak bentrok dengan CSS aplikasi. Ubah warna lewat variabel. */
:root{--ai-ink:#1c2b36;--ai-mute:#5d6f7d;--ai-bg:#fff;--ai-soft:#eef3f7;--ai-line:#d3dde5;
--ai-accent:#0d6e8f;--ai-accent-ink:#fff;--ai-warn:#a15c00;--ai-warn-bg:#fdf3e1}
.ai-demo-note{font:14px/1.5 system-ui,sans-serif;color:#5d6f7d;padding:24px;max-width:46ch}
#ai-fab{position:fixed;right:20px;bottom:20px;width:52px;height:52px;border-radius:50%;border:0;cursor:pointer;
background:var(--ai-accent);color:var(--ai-accent-ink);display:grid;place-items:center;box-shadow:0 4px 14px rgba(13,110,143,.35);z-index:1000}
#ai-panel{position:fixed;right:20px;bottom:84px;width:min(380px,calc(100vw - 24px));height:min(540px,calc(100vh - 110px));
background:var(--ai-bg);border:1px solid var(--ai-line);border-radius:12px;display:none;flex-direction:column;overflow:hidden;
font:14px/1.5 ui-sans-serif,system-ui,sans-serif;color:var(--ai-ink);box-shadow:0 12px 32px rgba(28,43,54,.18);z-index:1000}
#ai-panel.open{display:flex}
.ai-head{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--ai-line);font-weight:600}
.ai-head span{flex:1}.ai-head button{border:0;background:none;font-size:20px;line-height:1;cursor:pointer;color:var(--ai-mute)}
#ai-list{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.ai-msg{max-width:88%;padding:9px 12px;border-radius:10px;white-space:pre-wrap;word-break:break-word}
.ai-user{align-self:flex-end;background:var(--ai-accent);color:var(--ai-accent-ink);border-bottom-right-radius:3px}
.ai-bot{align-self:flex-start;background:var(--ai-soft);border-bottom-left-radius:3px;font-variant-numeric:tabular-nums}
.ai-bot.err{background:var(--ai-warn-bg);color:var(--ai-warn)}
.ai-meta{align-self:flex-start;margin-top:-6px;font-size:12px;color:var(--ai-mute)}
.ai-chip{display:inline-block;padding:1px 8px;border:1px solid var(--ai-line);border-radius:999px;margin-right:6px}
.ai-empty{color:var(--ai-mute)}.ai-empty p{margin:0 0 10px}
.ai-sug{display:flex;flex-direction:column;gap:6px;align-items:flex-start}
.ai-sug button{border:1px solid var(--ai-line);background:var(--ai-bg);border-radius:8px;padding:6px 10px;cursor:pointer;font:inherit;color:var(--ai-ink)}
.ai-sug button:hover{background:var(--ai-soft)}
.ai-dots::after{content:"";animation:ai-d 1.2s steps(4) infinite}
@keyframes ai-d{0%{content:""}25%{content:"."}50%{content:".."}75%,100%{content:"..."}}
#ai-form{display:flex;gap:8px;padding:10px;border-top:1px solid var(--ai-line)}
#ai-input{flex:1;border:1px solid var(--ai-line);border-radius:8px;padding:8px 10px;font:inherit;color:inherit}
#ai-send{border:0;border-radius:8px;padding:0 14px;background:var(--ai-accent);color:var(--ai-accent-ink);font:inherit;font-weight:600;cursor:pointer}
#ai-send:disabled,#ai-input:disabled{opacity:.55;cursor:not-allowed}
#ai-fab:focus-visible,#ai-panel button:focus-visible,#ai-input:focus-visible{outline:2px solid var(--ai-accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){.ai-dots::after{animation:none;content:"..."}}
</style>
</head>
<body>
<p class="ai-demo-note">Pratinjau widget. Buka dengan <code>?demo</code> di URL untuk memakai jawaban contoh tanpa backend.</p>

<button id="ai-fab" type="button" aria-label="Buka asisten AI" aria-expanded="false" hidden>
  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg>
</button>

<section id="ai-panel" role="dialog" aria-label="Asisten AI monitoring OLT">
  <div class="ai-head"><span>Asisten NOC</span><button type="button" id="ai-close" aria-label="Tutup">&times;</button></div>
  <div id="ai-list" aria-live="polite"></div>
  <div id="ai-form">
    <input id="ai-input" type="text" maxlength="300" placeholder="Tanya data OLT, mis. berapa ONU online?" autocomplete="off" aria-label="Pertanyaan">
    <button id="ai-send" type="button">Kirim</button>
  </div>
</section>

<script>
(() => {
  const API = '/api/v1/ai';
  const DEMO = location.search.includes('demo');
  const token = () => localStorage.getItem('token') || '';   // ADAPT: sesuaikan dengan cara app menyimpan JWT
  const $ = (id) => document.getElementById(id);
  const fab = $('ai-fab'), panel = $('ai-panel'), list = $('ai-list'), input = $('ai-input'), send = $('ai-send');
  const SUGGEST = ['Berapa ONU online?', 'Ada alert aktif?', 'ONU dengan RX terburuk'];
  let busy = false;

  function el(cls, text) { const d = document.createElement('div'); d.className = cls; if (text) d.textContent = text; return d; }
  function scroll() { list.scrollTop = list.scrollHeight; }

  function showEmpty() {
    const box = el('ai-empty');
    const p = document.createElement('p'); p.textContent = 'Tanyakan status OLT, ONU, VLAN, atau alert. Aku hanya membaca data milikmu, tidak mengubah apa pun.';
    const sug = el('ai-sug');
    SUGGEST.forEach(q => { const b = document.createElement('button'); b.type = 'button'; b.textContent = q; b.onclick = () => ask(q); sug.append(b); });
    box.append(p, sug); list.append(box);
  }

  function add(cls, text) { const m = el('ai-msg ' + cls, text); list.append(m); scroll(); return m; }   // textContent: aman dari XSS

  function meta(d) {
    const m = el('ai-meta');
    if (d.source) { const c = document.createElement('span'); c.className = 'ai-chip'; c.textContent = 'Data dari OLT: ' + d.source; m.append(c); }
    const how = d.provider === 'rule' ? 'aturan sederhana' : d.cached ? 'cache' : (d.provider || '');
    m.append(document.createTextNode([how, d.latency_ms ? (d.latency_ms / 1000).toFixed(1).replace('.', ',') + ' dtk' : ''].filter(Boolean).join(' - ')));
    list.append(m);
  }

  async function demoAsk() {
    await new Promise(r => setTimeout(r, 800));
    return {answer: 'ZXAN: 2 online, 1 offline, 0 LOS, 0 dying gasp (total 3)', source: 'ZXAN', provider: 'gemini', latency_ms: 1180};
  }

  async function ask(q) {
    q = (q || '').trim(); if (!q || busy) return;
    busy = true; input.value = ''; input.disabled = send.disabled = true;
    const first = list.querySelector('.ai-empty'); if (first) first.remove();
    add('ai-user', q);
    const wait = add('ai-bot', 'Memeriksa data'); wait.classList.add('ai-dots');
    try {
      let d;
      if (DEMO) d = await demoAsk();
      else {
        const r = await fetch(API + '/ask', {method: 'POST', headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + token()}, body: JSON.stringify({question: q})});
        if (r.status === 401) throw new Error('Sesi habis. Login ulang, lalu kirim lagi pertanyaanmu.');
        if (r.status === 503) throw new Error('Fitur AI belum diaktifkan. Hubungi admin.');
        if (!r.ok) throw new Error('Server menolak permintaan (kode ' + r.status + '). Coba lagi sebentar lagi.');
        d = await r.json();
      }
      wait.remove(); add('ai-bot' + (d.degraded && !d.tool_used ? ' err' : ''), d.answer); meta(d);
    } catch (e) {
      wait.remove();
      add('ai-bot err', e instanceof TypeError ? 'Tidak bisa menghubungi server. Periksa koneksi, lalu kirim ulang.' : e.message);
    } finally { busy = false; input.disabled = send.disabled = false; input.focus(); }
  }

  function toggle(open) {
    panel.classList.toggle('open', open); fab.setAttribute('aria-expanded', open);
    if (open) { if (!list.children.length) showEmpty(); input.focus(); } else fab.focus();
  }

  fab.onclick = () => toggle(!panel.classList.contains('open'));
  $('ai-close').onclick = () => toggle(false);
  send.onclick = () => ask(input.value);
  input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) ask(input.value); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && panel.classList.contains('open')) toggle(false); });

  // Tombol hanya muncul bila backend AI aktif; tanpa API key/endpoint, aplikasi inti tidak berubah.
  if (DEMO) fab.hidden = false;
  else fetch(API + '/status', {headers: {Authorization: 'Bearer ' + token()}}).then(r => r.ok ? r.json() : null)
        .then(s => { if (s && s.enabled) fab.hidden = false; }).catch(() => {});
})();
</script>
</body>
</html>
