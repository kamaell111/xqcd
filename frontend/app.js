// =================== STATE ===================
const API = "/api/v1";
let TOKEN = localStorage.getItem("token") || null;
let CURRENT_USER = JSON.parse(localStorage.getItem("user") || "null");
let CURRENT_OLT_ID = parseInt(localStorage.getItem("olt_id") || "0", 10) || null;
let _currentOltName = "";   // nama OLT aktif untuk kolom OLT di tabel ONU
let refreshTimer = null;
let recoveryTimer = null;
let lastRecoveryTs = 0;   // track timestamp recovery terakhir
let cpuChart = null;
let lastCpuValue = null;
let pageAbortController = null;   // untuk batalkan request saat pindah tab
let currentPage = "dashboard";
const PAGE_ALIAS = {
  pons:       { page: "port", tab: "pon" },
  interfaces: { page: "port", tab: "uplink" },
};
let portTab = "all";
let portFilter = null; // null | "up" | "down" | "shutdown"
let vlanTab = "list";
let uncfgCache = null;   // cache hasil uncfg dari sync-pons
let _inventoryCache = null;   // cache inventory (VLAN, T-CONT, ONU type)
let _gponProfilesCache = { tcont: [], traffic: [] };   // cache GPON profiles (Fase 4)

// =================== HTTP ===================
async function api(path, opts = {}) {
  // Phase B: guard kalau CURRENT_OLT_ID null
  if (path.includes("/null/") || path.includes("/undefined/")) {
    throw new Error("Tidak ada OLT terpilih. Hubungi Super Admin untuk assign OLT.");
  }
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (TOKEN) headers["Authorization"] = `Bearer ${TOKEN}`;
  const res = await fetch(`${API}${path}`, { ...opts, headers, signal: opts.signal });
  if (res.status === 401) {
    logout();
    throw new Error("Sesi berakhir, silakan login kembali");
  }
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!res.ok) {
    const detail = data.detail || data.message || "Request gagal";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

// =================== HELPERS ===================
function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function toast(msg, type = "info") {
  const c = document.getElementById("toast-container");
  const t = document.createElement("div");
  t.className = `toast ${type}`;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function formatUptime(sec) {
  if (!sec) return "N/A";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function setSkeleton(el, text = "--") {
  if (el) el.innerHTML = `<span class="skeleton">${text}</span>`;
}

// =================== AUTH ===================
document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const u = document.getElementById("username").value.trim();
  const p = document.getElementById("password").value;
  const err = document.getElementById("login-error");
  const btn = document.getElementById("login-btn");
  err.textContent = "";
  btn.disabled = true;
  btn.querySelector("span").textContent = "Memuat...";
  try {
    const res = await api("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: u, password: p }),
    });
    TOKEN = res.access_token;
    localStorage.setItem("token", TOKEN);

    // Phase B: fetch /auth/me untuk data lengkap (is_super_admin, owner_user_id)
    let me = {};
    try { me = await api("/auth/me"); } catch(_) {}
    CURRENT_USER = { ...res.user, ...me };
    localStorage.setItem("user", JSON.stringify(CURRENT_USER));
    await showApp();
  } catch (ex) {
    err.textContent = ex.message;
  } finally {
    btn.disabled = false;
    btn.querySelector("span").textContent = "Masuk";
  }
});

// =================== RESET STATE ===================
// Dipanggil saat logout & sebelum showApp — biar tidak ada state nyangkut
function resetAppState() {
  // Timers
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  if (recoveryTimer) { clearInterval(recoveryTimer); recoveryTimer = null; }
  // Time trackers
  lastRecoveryTs = 0;
  lastAlertIds = new Set();
  isFirstAlertPoll = true;
  // Caches
  uncfgCache = null;
  _inventoryCache = null;
  _oltDriversCache = null;
  window._oltOwnersCache = null;
  // Chart — destroy biar tidak nyangkut
  if (cpuChart) {
    try { cpuChart.destroy(); } catch(_) {}
    cpuChart = null;
  }
  lastCpuValue = null;
  // Abort pending request
  if (pageAbortController) {
    try { pageAbortController.abort(); } catch(_) {}
    pageAbortController = null;
  }
  // Reset OLT aktif
  CURRENT_OLT_ID = null;
  localStorage.removeItem("olt_id");
  // Reset halaman aktif
  currentPage = "dashboard";

  // ⭐ Job pollers — matikan semua interval job polling
  if (typeof _jobPollers !== "undefined" && _jobPollers) {
    Object.keys(_jobPollers).forEach(jid => {
      try { clearInterval(_jobPollers[jid]); } catch(_) {}
      delete _jobPollers[jid];
    });
  }

  // ⭐ Tutup modal kalau masih terbuka
  const modal = document.getElementById("modal");
  if (modal) modal.classList.add("hidden");

  // ⭐ Reset sidebar — hapus semua .active
  document.querySelectorAll(".sidebar nav a.active").forEach(a => a.classList.remove("active"));

  // ⭐ Reset halaman — sembunyikan semua, biar loadPage pilih ulang
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));

  console.log("[STATE] reset selesai");
}

function logout() {
  resetAppState();
  TOKEN = null;
  CURRENT_USER = null;
  localStorage.removeItem("token");
  localStorage.removeItem("user");
  document.getElementById("login-screen").classList.remove("hidden");
  document.getElementById("main-screen").classList.add("hidden");
}

document.getElementById("logout-btn").addEventListener("click", logout);

// =================== OLT SWITCHER ===================
async function loadOltSwitcher() {
  const wrap = document.getElementById("olt-switcher-wrap");
  const sel = document.getElementById("olt-switcher");
  if (!wrap || !sel) return;
  try {
    const olts = await api("/olts");
    if (!olts.length) {
      wrap.classList.add("hidden");
      return;
    }
    // Kalau cuma 1 OLT, sembunyikan switcher (tidak berguna)
    if (olts.length === 1) {
      wrap.classList.add("hidden");
      return;
    }
    wrap.classList.remove("hidden");
    sel.innerHTML = olts.map(o => {
      const badge = o.status === "online" ? "● " : "○ ";
      const label = (o.hostname || "OLT") + " (" + (o.ip_address || "?") + ")";
      return '<option value="' + o.id + '">' + badge + label + '</option>';
    }).join("");
    if (CURRENT_OLT_ID) sel.value = String(CURRENT_OLT_ID);
  } catch (e) {
    console.warn("[SWITCHER] gagal load:", e);
  }
}

async function switchOlt(newId) {
  const id = parseInt(newId, 10);
  if (!id || id === CURRENT_OLT_ID) return;
  CURRENT_OLT_ID = id;
  localStorage.setItem("olt_id", String(id));
  // Update nama OLT aktif
  try {
    const olts = await api("/olts");
    const cur = olts.find(o => o.id === id);
    _currentOltName = (cur && cur.hostname) ? cur.hostname : ("OLT-" + id);
  } catch (e) { _currentOltName = "OLT-" + id; }
  console.log("[OLT] switch ke id=" + id + " name=" + _currentOltName);

  // Reset cache
  uncfgCache = null;
  _inventoryCache = null;

  // Reload halaman aktif
  const activePage = document.querySelector(".sidebar nav a.active")?.dataset.page || "dashboard";
  toast("Ganti OLT: memuat ulang...", "info");
  loadPage(activePage);
}

async function initCurrentOltId() {
  try {
    const olts = await api("/olts");
    if (!olts.length) {
      CURRENT_OLT_ID = null;
      localStorage.removeItem("olt_id");
      console.warn("[OLT] tidak ada OLT terdaftar");
      return null;
    }
    const saved = parseInt(localStorage.getItem("olt_id") || "0", 10);
    const found = olts.find(o => o.id === saved);
    CURRENT_OLT_ID = found ? found.id : olts[0].id;
    localStorage.setItem("olt_id", String(CURRENT_OLT_ID));
    const cur = olts.find(o => o.id === CURRENT_OLT_ID);
    _currentOltName = (cur && cur.hostname) ? cur.hostname : ("OLT-" + CURRENT_OLT_ID);
    console.log(`[OLT] aktif: id=${CURRENT_OLT_ID} name=${_currentOltName}`);
    return CURRENT_OLT_ID;
  } catch (e) {
    console.warn("[OLT] initCurrentOltId gagal:", e);
    return null;
  }
}

async function showApp() {
  resetAppState();   // ⭐ bersihkan semua state dari session sebelumnya
  document.getElementById("login-screen").classList.add("hidden");
  document.getElementById("main-screen").classList.remove("hidden");
  if (typeof applySidebarMode === "function") applySidebarMode();
  document.getElementById("user-name").textContent = CURRENT_USER.username;
  document.getElementById("user-role").textContent = `${CURRENT_USER.role} · priv ${CURRENT_USER.privilege}`;

  // Phase B: badge Super Admin
  const mbadge = document.getElementById("multivers-badge");
  if (mbadge) {
    if (CURRENT_USER.is_super_admin) {
      mbadge.classList.remove("hidden");
    } else {
      mbadge.classList.add("hidden");
    }
  }

  if (CURRENT_USER.privilege < 15) {
    const u = document.querySelector('[data-page="users"]');
    if (u) u.style.display = "none";
    const w = document.querySelector('[data-page="wa"]');
    if (w) w.style.display = "none";
  }
  if (CURRENT_USER.privilege < 10) {
    const a = document.querySelector('[data-page="audit"]');
    if (a) a.style.display = "none";
  }

  await initCurrentOltId();
  console.log("[APP] initCurrentOltId selesai, id=" + CURRENT_OLT_ID);

  // Load switcher (kalau >1 OLT)
  await loadOltSwitcher();

  if (CURRENT_OLT_ID == null) {
    const isMulti = CURRENT_USER.is_super_admin === 1;
    const msg = isMulti
      ? "Belum ada OLT terdaftar di sistem."
      : "Belum ada OLT yang di-assign ke Anda. Hubungi Super Admin untuk assign OLT.";
    toast(msg, "warning");
    // Sembunyikan menu yang butuh OLT
    ["dashboard","pons","onus","optical","interfaces","vlans"].forEach(function(p) {
      const el = document.querySelector('[data-page="' + p + '"]');
      if (el) el.style.opacity = "0.35", el.style.pointerEvents = "none";
    });
    // Arahkan ke halaman OLT / Users
    loadPage(isMulti ? "olts" : "users");
    return;
  }

  loadPage("dashboard");
  if (refreshTimer) clearInterval(refreshTimer);

  // ⭐ Recovery polling (notifikasi "ONU sudah pulih")
  if (recoveryTimer) clearInterval(recoveryTimer);
  recoveryTimer = setInterval(() => {
    pollRecoveries();
    pollNewAlerts();       // ⭐ cek alert BARU juga
    loadAlertBadge();      // ⭐ badge selalu refresh
  }, 5000);
  pollRecoveries();
  pollNewAlerts();
  loadAlertBadge();   // instan pertama

  // Auto-refresh konten tiap 5 detik
  refreshTimer = setInterval(() => {
    const modalOpen = !document.getElementById("modal").classList.contains("hidden");
    if (modalOpen) return;

    if (currentPage === "dashboard") {
      loadDashboardStats();
      loadUncfg();
    } else if (currentPage === "onus") {
      loadONUs();
    } else if (currentPage === "port") {
      loadPortPage();
    } else if (currentPage === "vlans") {
      if (vlanTab === "port") loadPortVLANTable();
      else if (vlanTab === "vlanif") loadVlanifTable();
      else loadVLANs();
    } else if (currentPage === "alerts") {
      switchAlertTab(_alertTab || "system");
    }
  }, 5000);
}

// =================== NAVIGATION ===================
const PAGE_TITLES = {
  dashboard: ["Dashboard", "Ringkasan sistem dan status perangkat"],
  olts: ["OLT Management", "Kelola perangkat OLT terdaftar"],
  pons: ["PON Ports", "Status 16 port GPON"],
  port: ["Port", "Status dan kontrol port PON & uplink"],
  onus: ["ONU Management", "Daftar dan provisioning ONU"],
  optical: ["Optical Power", "Monitoring redaman optik semua ONU"],
  "onu-detail": ["Detail ONU", "Informasi lengkap & troubleshooting"],
  interfaces: ["Interfaces", "Status uplink GEI / XGEI"],
  vlans: ["VLAN", "Manajemen VLAN database"],
  deployment: ["Deployment", "GPON Profile Management"],
  wa: ["WhatsApp Bot", "Notifikasi WhatsApp: gateway, template, test kirim"],
  alerts: ["Alert Center", "Peringatan sistem dan jaringan"],
  audit: ["Audit Log", "Riwayat aktivitas pengguna"],
  users: ["User Management", "Kelola akun dan hak akses"],
};

document.querySelectorAll(".sidebar nav a").forEach(a => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    document.querySelectorAll(".sidebar nav a").forEach(x => x.classList.remove("active"));
    a.classList.add("active");
    loadPage(a.dataset.page);
  });
});



function loadPage(page) {
  const alias = PAGE_ALIAS[page];
  if (alias) { page = alias.page; portTab = alias.tab; }
  currentPage = page;
  // Hook halaman Pengaturan
  if (page === "settings" && typeof window.initSettingsPage === "function") {
    window.initSettingsPage();
  }
  if (pageAbortController) pageAbortController.abort();
  pageAbortController = new AbortController();

  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  const el = document.getElementById(`page-${page}`);
  if (el) el.classList.add("active");

  const [title, sub] = PAGE_TITLES[page] || [page, ""];
  document.getElementById("page-title").textContent = title;
  document.getElementById("page-subtitle").textContent = sub;
  // Update breadcrumb
  const crumb = document.getElementById("crumb-current");
  if (crumb) crumb.textContent = title;

  const signal = pageAbortController.signal;
  const opts = { signal };

  if (page === "dashboard") { loadDashboardStats(); loadPONGrid(); loadUplinkGrid(); loadRecentAlerts(); loadONUOptical(); drawChart(); loadAlertBadge(); loadUncfg(); }
  if (page === "olts") loadOLTs(opts);
  if (page === "port") { renderPortPane(); loadPortPage(opts); }
  if (page === "onus") loadONUs(opts);
  if (page === "optical") loadOpticalPage(opts);
  if (page === "traffic") loadTrafficPage(opts);
  if (page === "onu-detail") loadONUDetailPage(opts);
  if (page === "interfaces") loadInterfaces(opts);
  if (page === "deployment") { if (window.Deployment) Deployment.open("tcont"); }
  if (page === "wa") { if (window.WaBot) WaBot.open(); }
  if (page === "vlans") {
    vlanTab = "list";
    renderVlanPane();
    loadVLANs(opts);
  }
  if (page === "alerts") switchAlertTab(_alertTab || "system");
  if (page === "audit") loadAudit(opts);
  if (page === "users") loadUsers(opts);
}

// =================== DASHBOARD ===================
// (NEW) Ambil last_polled untuk oltId & update pill header.
// Aman dipanggil dari page mana pun — tidak sentuh hero/CPU/Mem.
async function refreshFreshness(oltId = CURRENT_OLT_ID) {
  if (!oltId) return;
  try {
    const s = await api(`/olts/${oltId}/status`);
    _lastPolledCache = s.last_polled;
    updateFreshness(s.last_polled);
  } catch (e) {
    // diam-diam; tick berikutnya coba lagi
  }
}

async function loadDashboardStats() {
  try {
    const s = await api(`/olts/${CURRENT_OLT_ID}/status`);

    // Hero
    document.getElementById("hero-hostname").textContent = s.hostname || "ZXAN";
    // Fix: sembunyikan OUTER span kalau inner span kosong (hilangkan bullet nyangkut)
    (function setHeroMeta() {
      var ip = (s.ip_address || "").trim();
      var model = (s.model || "").trim();
      var fw = (s.firmware || "").trim();
      var loc = (s.location || "").trim();
      var fwCombined = [model, fw].filter(Boolean).join(" · ");
      function set(id, v) {
        var inner = document.getElementById(id);
        if (!inner) return;
        inner.textContent = v || "";
        // Sembunyikan parent span kalau inner kosong
        var outer = inner.parentElement;
        if (outer && outer.classList.contains("olt-bar-meta") === false) {
          // outer adalah span pembungkus (bukan div .olt-bar-meta)
          if (!v) {
            outer.style.display = "none";
          } else {
            outer.style.display = "";
          }
        }
      }
      set("hero-ip", ip);
      set("hero-firmware", fwCombined);
      set("hero-location", loc);
    })();
    document.getElementById("hero-uptime").textContent = formatUptime(s.uptime_seconds);

    // OLT status pill
    const pill = document.getElementById("olt-status");
    document.getElementById("olt-status-text").textContent = `${s.hostname} · ${s.status}`;
    pill.className = `status-pill ${s.status}`;

    // Freshness indicator
    _lastPolledCache = s.last_polled;
    updateFreshness(s.last_polled);

    // Circuit breaker indicator
    updateCircuitPill(s.circuit);

    // Bar kesehatan: CPU
    const cpuEl = document.getElementById("stat-cpu");
    if (s.cpu_usage != null) {
      const cpu = Math.round(s.cpu_usage * 10) / 10;
      cpuEl.textContent = `${cpu}%`;
      const bar = document.getElementById("cpu-bar");
      bar.style.width = Math.min(cpu, 100) + "%";
      bar.classList.toggle("warning", cpu > 70 && cpu <= 85);
      bar.classList.toggle("danger", cpu > 85);
    } else {
      cpuEl.textContent = "N/A";
    }

    // Bar kesehatan: Memory
    const memEl = document.getElementById("stat-mem");
    if (s.memory_usage != null) {
      const mem = Math.round(s.memory_usage * 10) / 10;
      memEl.textContent = `${mem}%`;
      const bar = document.getElementById("mem-bar");
      bar.style.width = Math.min(mem, 100) + "%";
      bar.classList.toggle("warning", mem > 70 && mem <= 85);
      bar.classList.toggle("danger", mem > 85);
    } else {
      memEl.textContent = "N/A";
    }

    // Bar kesehatan: Uptime
    const uptEl = document.getElementById("stat-uptime");
    if (uptEl) uptEl.textContent = formatUptime(s.uptime_seconds);

    // ONU stats (online / offline / los)
    try {
      const onus = await api(`/olts/${CURRENT_OLT_ID}/onus`);
      const total = onus.length;
      const online = onus.filter(o => o.status === "online").length;
      const los = onus.filter(o => o.status === "los").length;
      const offline = onus.filter(o => o.status === "offline").length;
      const configuring = total - online - los - offline;

      const onlineEl = document.getElementById("stat-online");
      const onlineSub = document.getElementById("online-sub");
      if (onlineEl) {
        onlineEl.textContent = online;
        const pct = total > 0 ? Math.round((online / total) * 100) : 0;
        onlineEl.style.color = pct >= 90 ? "var(--green)" : pct >= 70 ? "var(--yellow)" : "var(--red)";
        if (onlineSub) onlineSub.textContent = `dari ${total} ONU (${pct}%)`;
      }

      const offEl = document.getElementById("stat-offline");
      const offSub = document.getElementById("offline-sub");
      if (offEl) {
        offEl.textContent = offline;
        offEl.style.color = offline === 0 ? "var(--green)" : "var(--red)";
        if (offSub) offSub.textContent = offline === 0 ? "semua terhubung" : "modem mati / tidak terhubung";
      }

      const losEl = document.getElementById("stat-los");
      const losSub = document.getElementById("los-sub");
      if (losEl) {
        losEl.textContent = los;
        losEl.style.color = los === 0 ? "var(--green)" : "var(--yellow)";
        if (losSub) losSub.textContent = los === 0 ? "tidak ada gangguan fiber" : "fiber putus";
      }

      // Hero counters
      document.getElementById("hero-onus").textContent = online;
    } catch (_) {}

    // Alerts
    const alertCount = s.active_alerts ?? 0;
    document.getElementById("stat-alerts").textContent = alertCount;
    const alertSub = document.getElementById("alert-sub");
    alertSub.textContent = alertCount === 0 ? "Semua aman" : `${alertCount} alert aktif`;
    alertSub.style.color = alertCount === 0 ? "var(--green)" : "var(--yellow)";

    // Hero PON counter
    try {
      const pons = await api(`/olts/${CURRENT_OLT_ID}/pons`);
      document.getElementById("hero-pons").textContent = pons.filter(p => p.status === "up").length;
    } catch (_) {}
  } catch (e) {
    console.warn("Dashboard stats error:", e);
  }
}

function gotoPortTab(tab) {
  portTab = tab;
  portFilter = null; // reset filter biar tabel tidak kosong
  gotoPage("port");
}

// "gei_1/3/1" → { short: "3/1", kind: "gei" }
function uplinkShortName(name) {
  const m = String(name || "").match(/^([a-z]+)[_-]?(\d+(?:\/\d+)*)$/i);
  if (!m) return { short: String(name || "-"), kind: "" };
  return { short: m[2].split("/").slice(-2).join("/"), kind: m[1].toLowerCase() };
}

async function loadUplinkGrid() {
  const grid = document.getElementById("uplink-grid");
  if (!grid) return;
  const msg = t => `<div style="grid-column:1/-1;font-size:12px;color:var(--text-dim)">${t}</div>`;
  try {
    const ifaces = await api(`/olts/${CURRENT_OLT_ID}/interfaces`);
    const numParts = s => (String(s || "").match(/\d+/g) || []).map(Number);
    const sorted = [...ifaces].sort((a, b) => {
      const x = numParts(a.name), y = numParts(b.name);
      for (let k = 0; k < Math.max(x.length, y.length); k++) {
        const d = (x[k] || 0) - (y[k] || 0);
        if (d) return d;
      }
      return 0;
    });
    if (!sorted.length) { grid.innerHTML = msg("Tidak ada port uplink"); return; }
    const cellClass = { up: "active", down: "down", shutdown: "shutdown" };
    grid.innerHTML = sorted.map(i => {
      const state = ifaceState(i);
      const { short, kind } = uplinkShortName(i.name);
      const label = kind || String(i.type || "").toLowerCase();
      const tip = `${i.name} · ${state}${i.speed ? ` · ${i.speed} Mbps` : ""}`;
      return `<div class="pon-cell ${cellClass[state]}" title="${escapeHtml(tip)}" data-port="${escapeHtml(i.name)}" onclick="gotoPortTab('uplink')">
        <div class="pon-num">${escapeHtml(short)}</div>
        <div class="pon-onu">${escapeHtml(label)}</div>
      </div>`;
    }).join("");
  } catch (e) {
    if (!grid.querySelector(".pon-cell")) grid.innerHTML = msg("Gagal memuat port uplink");
  }
}

async function loadPONGrid() {
  const grid = document.getElementById("pon-grid");
  if (!grid) return;
  try {
    const pons = await api(`/olts/${CURRENT_OLT_ID}/pons`);
    // ⭐ Sort numerik berdasarkan nomor port (1,2,3...16) — bukan string
    pons.sort((a, b) => {
      const na = parseInt(String(a.port_no).split("/").pop(), 10) || 0;
      const nb = parseInt(String(b.port_no).split("/").pop(), 10) || 0;
      return na - nb;
    });
    grid.innerHTML = pons.map(p => {
      // ⭐ Tentukan visual state yang BENAR:
      //    shutdown admin      → abu-abu (normal, belum dipakai)
      //    up + onu > 0        → hijau (aktif)
      //    up + onu = 0        → idle (abu-abu terang)
      //    down (bukan shutdown) → merah (fault)
      let state = "idle";
      const admin = (p.admin_state || "").toLowerCase();
      const status = (p.status || "").toLowerCase();
      
      if (admin === "shutdown" || status === "shutdown") {
        state = "shutdown";   // ← abu-abu, BUKAN merah
      } else if (status === "up" && (p.onu_count || 0) > 0) {
        state = "active";      // ← hijau
      } else if (status === "up") {
        state = "idle";        // ← abu-abu terang
      } else if (status === "down") {
        state = "down";        // ← merah (fault)
      }
      
      const portNum = p.port_no.split("/").pop();
      return `
        <div class="pon-cell ${state}" title="PON ${escapeHtml(p.port_no)} · ${escapeHtml(p.status)} · ${p.onu_count} ONU"
             data-port="${escapeHtml(p.port_no)}">
          <div class="pon-num">${escapeHtml(portNum)}</div>
          <div class="pon-onu">PON ${escapeHtml(portNum)}</div>
        </div>
      `;
    }).join("");
    grid.querySelectorAll(".pon-cell").forEach(cell => {
      cell.addEventListener("click", () => gotoPON(cell.dataset.port));
    });
  } catch (e) {
    grid.innerHTML = `<p style="color:var(--text-dim);padding:20px;text-align:center">Gagal memuat data PON</p>`;
  }
}

function gotoPON(port) {
  const link = document.querySelector('[data-page="onus"]');
  if (link) link.click();
  setTimeout(() => {
    const inp = document.getElementById("onu-search");
    if (inp) { inp.value = port; loadONUs(); }
  }, 80);
}

async function loadRecentAlerts() {
  const el = document.getElementById("recent-alerts");
  if (!el) return;
  try {
    const alerts = await api("/alerts?limit=5&resolved=false");
    if (!alerts.length) {
      el.innerHTML = `
        <div style="padding:32px 12px;text-align:center;color:var(--text-dim)">
          <i class="fas fa-check-circle" style="font-size:32px;color:var(--green);margin-bottom:10px;display:block"></i>
          <div style="font-weight:600;color:var(--text)">Semua aman</div>
          <div style="font-size:12px;margin-top:4px">Tidak ada alert aktif</div>
        </div>`;
      return;
    }
    el.innerHTML = alerts.map(a => alertItemHTML(a)).join("");
  } catch (e) {
    el.innerHTML = `<p style="color:var(--text-dim);padding:12px">Gagal memuat alert</p>`;
  }
}

// =================== HALAMAN OPTICAL ===================
function _classifyRX(rx) {
  if (rx == null) return "unknown";
  if (rx < -28) return "critical";
  if (rx < -25) return "warning";
  return "good";
}

function _rxColor(rx) {
  const cls = _classifyRX(rx);
  if (cls === "critical") return "var(--red)";
  if (cls === "warning") return "var(--yellow)";
  if (cls === "good") return "var(--green)";
  return "var(--text-dim)";
}

async function loadOpticalPage(opts = {}) {
  const el = document.getElementById("optical-table");
  if (!el) return;
  try {
    const onus = await api(`/olts/${CURRENT_OLT_ID}/onus`, opts);

    // Cuma yang punya data optical
    const withRX = onus.filter(o => o.optical_rx != null);
    // Urut RX terburuk dulu (paling negatif)
    withRX.sort((a, b) => a.optical_rx - b.optical_rx);

    const crit = withRX.filter(o => o.optical_rx < -28).length;
    const warn = withRX.filter(o => o.optical_rx >= -28 && o.optical_rx < -25).length;
    const good = withRX.filter(o => o.optical_rx >= -25).length;

    // Update summary cards
    document.getElementById("optical-total").textContent = withRX.length;
    document.getElementById("optical-warn").textContent = warn;
    document.getElementById("optical-crit").textContent = crit;
    // Card "RX Terburuk" — always tampilkan ONU dengan RX paling negatif
    const worst = withRX[0];
    const worstEl = document.getElementById("optical-worst");
    const worstSub = document.getElementById("optical-worst-sub");
    if (worst) {
      worstEl.textContent = `${worst.optical_rx.toFixed(1)} dBm`;
      worstEl.style.color = _rxColor(worst.optical_rx);
      worstSub.textContent = `${worst.name || worst.serial_number} · ${worst.pon_port}:${worst.onu_id}`;
    } else {
      worstEl.textContent = "-";
      worstSub.textContent = "-";
    }

    // Filter + search
    const filter = document.getElementById("optical-filter")?.value || "";
    const search = (document.getElementById("optical-search")?.value || "").toLowerCase().trim();
    let filtered = withRX;
    if (filter) filtered = filtered.filter(o => _classifyRX(o.optical_rx) === filter);
    if (search) {
      filtered = filtered.filter(o => {
        const hay = [o.serial_number, o.name, o.pon_port, o.interface_name].filter(Boolean).join(" ").toLowerCase();
        return hay.includes(search);
      });
    }

    if (!filtered.length) {
      el.innerHTML = `
        <div class="widget" style="text-align:center;padding:40px">
          <i class="fas fa-signal" style="font-size:36px;color:var(--text-dim);margin-bottom:12px;display:block"></i>
          <p style="color:var(--text-dim)">Tidak ada ONU dengan optical power</p>
        </div>`;
      return;
    }

    el.innerHTML = `
      <div class="table-wrap"><table class="table-premium">
        <thead><tr>
          <th>#</th><th>Nama</th><th>Serial</th><th>PON</th>
          <th>RX (dBm)</th><th>TX (dBm)</th><th>Distance</th><th>Status</th><th>Kategori</th>
        </tr></thead>
        <tbody>
          ${filtered.map((o, i) => {
            const rx = o.optical_rx;
            const cls = _classifyRX(rx);
            const color = _rxColor(rx);
            const catLabel = cls === "critical" ? "CRITICAL" : cls === "warning" ? "WARNING" : "GOOD";
            return `<tr style="cursor:pointer" onclick="showONUDetail(${o.onu_id})">
              <td><b>${i + 1}</b></td>
              <td>${escapeHtml(o.name || "-")}</td>
              <td><code>${escapeHtml(o.serial_number || "-")}</code></td>
              <td>${escapeHtml(o.pon_port)}:${o.onu_id}</td>
              <td><b style="color:${color}">${rx.toFixed(2)}</b></td>
              <td>${o.optical_tx != null ? o.optical_tx.toFixed(2) : "-"}</td>
              <td>${o.distance != null ? o.distance + " m" : "-"}</td>
              <td><span class="status-badge ${escapeHtml(o.status)}">${escapeHtml(o.status)}</span></td>
              <td><span class="status-badge ${cls === "critical" ? "critical" : cls === "warning" ? "warning" : "online"}">${catLabel}</span></td>
            </tr>`;
          }).join("")}
        </tbody>
      </table></div>
    `;
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}

// Fix widget dashboard: urut RX terburuk dulu
async function loadONUOptical() {
  const el = document.getElementById("onu-optical-list");
  if (!el) return;
  try {
    const onus = await api(`/olts/${CURRENT_OLT_ID}/onus`);
    const online = onus.filter(o => o.status === "online" && o.optical_rx != null);
    online.sort((a, b) => a.optical_rx - b.optical_rx);   // terburuk dulu
    if (!online.length) {
      el.innerHTML = `<p style="color:var(--text-dim);padding:20px;text-align:center">Tidak ada ONU online</p>`;
      return;
    }
    el.innerHTML = online.slice(0, 6).map(o => {
      const rx = o.optical_rx;
      const cls = _classifyRX(rx) === "critical" ? "bad" : _classifyRX(rx) === "warning" ? "warn" : "good";
      return `
        <div class="onu-optical-item">
          <div class="onu-optical-info">
            <div class="onu-optical-name">${escapeHtml(o.name || "-")}</div>
            <div class="onu-optical-sn">${escapeHtml(o.serial_number || "-")} · ${escapeHtml(o.pon_port || "")}</div>
          </div>
          <div class="onu-optical-value">
            <div class="onu-optical-dbm ${cls}">${rx.toFixed(1)} dBm</div>
            <div class="onu-optical-label">RX Power</div>
          </div>
        </div>`;
    }).join("");
  } catch (e) {
    el.innerHTML = `<p style="color:var(--text-dim);padding:12px">Gagal memuat data ONU</p>`;
  }
}

// Bind filter + search (sekali saja)
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("optical-filter")?.addEventListener("change", () => loadOpticalPage());
  document.getElementById("optical-search")?.addEventListener("input", debounce(() => loadOpticalPage(), 300));
});

// Auto-refresh halaman optical tiap 10s (existing auto-refresh 5s tidak include optical)
setInterval(() => {
  if (currentPage === "optical") loadOpticalPage();
}, 10000);

// Auto-refresh halaman traffic tiap 10s (skip kalau tab hidden)
setInterval(() => {
  if (currentPage === "traffic" && !document.hidden) loadTrafficPage();
}, 10000);

// Refresh saat user kembali ke tab
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && currentPage === "traffic") {
    setTimeout(() => loadTrafficPage(), 300);
  }
});
function alertItemHTML(a, withCheckbox = false) {
  const icons = { critical: "fa-exclamation-circle", warning: "fa-exclamation-triangle", info: "fa-info-circle" };
  const time = a.created_at
    ? new Date(a.created_at).toLocaleString("id-ID", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })
    : "-";
  const checkbox = withCheckbox
    ? `<input type="checkbox" class="alert-checkbox" data-id="${a.id}" style="margin-top:8px;accent-color:var(--primary);width:16px;height:16px;flex-shrink:0">`
    : "";
  return `
    <div class="alert-item ${escapeHtml(a.severity)} ${a.resolved ? "resolved" : ""}">
      ${checkbox}
      <div class="alert-icon"><i class="fas ${icons[a.severity] || "fa-bell"}"></i></div>
      <div class="alert-body">
        <div class="alert-title">${escapeHtml(a.title)}</div>
        <div class="alert-message">${escapeHtml(a.message)}</div>
        <div class="alert-meta">
          ${a.source ? `<span><i class="fas fa-tag"></i> ${escapeHtml(a.source)}</span>` : ""}
          <span><i class="fas fa-clock"></i> ${time}</span>
          ${a.acknowledged ? '<span style="color:var(--green)"><i class="fas fa-check"></i> Ack</span>' : ""}
        </div>
      </div>
      <div class="alert-actions">
        ${!a.acknowledged ? `<button class="btn-icon" onclick="ackAlert(${a.id})" title="Acknowledge"><i class="fas fa-check"></i></button>` : ""}
        ${!a.resolved ? `<button class="btn-icon" onclick="resolveAlert(${a.id})" title="Resolve"><i class="fas fa-check-double"></i></button>` : ""}
        ${CURRENT_USER.privilege >= 10 ? `<button class="btn-icon" onclick="deleteAlert(${a.id})" title="Hapus" style="color:var(--red)"><i class="fas fa-trash"></i></button>` : ""}
      </div>
    </div>
  `;
}

async function deleteAlert(id) {
  if (!confirm(`Hapus alert #${id}?`)) return;
  try {
    await api(`/alerts/${id}`, { method: "DELETE" });
    toast("Alert dihapus", "success");
    loadRecentAlerts();
    loadAlerts();
    loadAlertBadge();
  } catch (e) { toast(e.message, "error"); }
}

async function ackAlert(id) {
  try {
    await api(`/alerts/${id}/ack`, { method: "POST" });
    toast("Alert di-acknowledge", "success");
    loadRecentAlerts();
    loadAlerts();
    loadAlertBadge();
  } catch (e) { toast(e.message, "error"); }
}

async function resolveAlert(id) {
  try {
    await api(`/alerts/${id}/resolve`, { method: "POST" });
    toast("Alert resolved", "success");
    loadRecentAlerts();
    loadAlerts();
    loadAlertBadge();
  } catch (e) { toast(e.message, "error"); }
}

let lastAlertIds = new Set();   // track alert ID yang sudah pernah dilihat

let isFirstAlertPoll = true;   // flag: polling pertama kali

async function pollNewAlerts() {
  try {
    // ⭐ Query SEMUA alert (termasuk resolved) — biar lastAlertIds stabil
    const alerts = await api("/alerts?limit=200");
    if (!Array.isArray(alerts)) return;

    // Untuk notifikasi: cuma yang belum resolved
    const activeAlerts = alerts.filter(a => !a.resolved);
    const allIds = new Set(alerts.map(a => a.id));

    // Pertama kali — isi saja, tanpa notif
    if (isFirstAlertPoll) {
      lastAlertIds = allIds;
      isFirstAlertPoll = false;
      return;
    }

    // Cari alert BARU (aktif tapi belum pernah dilihat)
    const newAlerts = activeAlerts.filter(a => !lastAlertIds.has(a.id));

    newAlerts.forEach(a => {
      const emoji = a.severity === "critical" ? "🔴"
                  : a.severity === "warning" ? "⚠️"
                  : "ℹ️";
      const type = a.severity === "critical" ? "error"
                 : a.severity === "warning" ? "warning"
                 : "info";
      toast(`${emoji} ${a.title}`, type);
    });

    lastAlertIds = allIds;
  } catch (_) {}
}


async function pollRecoveries() {
  try {
    const recoveries = await api(`/alerts/recent-recoveries?since=${lastRecoveryTs}`);
    if (!Array.isArray(recoveries) || recoveries.length === 0) return;

    // ⭐ Update timestamp DULU biar nggak dobel kalau polling cepet
    lastRecoveryTs = Math.max(...recoveries.map(r => r.ts));

    // ⭐ Dedupe per-source di frontend (safety net)
    const seen = new Set();
    recoveries.forEach(r => {
      if (seen.has(r.source)) return;
      seen.add(r.source);
      const shortTitle = (r.title || "").replace(/ONU\s+/, "").slice(0, 60);
      toast(`✅ ${shortTitle}`, "success");
    });
  } catch (_) {}
}


async function loadAlertBadge() {
  try {
    // ⭐ Hitung SEMUA alert (termasuk resolved)
    const alerts = await api("/alerts?limit=9999");
    const badge = document.getElementById("alert-badge");
    if (badge) badge.textContent = alerts.length;
  } catch (_) {}
}

// =================== CHART ===================
async function drawChart() {
  const ctx = document.getElementById("chart-cpu");
  if (!ctx) return;

  try {
    const [cpuData, memData] = await Promise.all([
      api(`/olts/${CURRENT_OLT_ID}/metrics?metric=cpu`),
      api(`/olts/${CURRENT_OLT_ID}/metrics?metric=memory`),
    ]);

    const labels = cpuData.points.map(p =>
      new Date(p.ts).toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit" })
    );

    if (cpuChart) cpuChart.destroy();

    const gradientCpu = ctx.getContext("2d").createLinearGradient(0, 0, 0, 260);
    gradientCpu.addColorStop(0, "rgba(79, 140, 255, 0.35)");
    gradientCpu.addColorStop(1, "rgba(79, 140, 255, 0)");

    const gradientMem = ctx.getContext("2d").createLinearGradient(0, 0, 0, 260);
    gradientMem.addColorStop(0, "rgba(16, 185, 129, 0.25)");
    gradientMem.addColorStop(1, "rgba(16, 185, 129, 0)");

    cpuChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "CPU %",
            data: cpuData.points.map(p => p.value),
            borderColor: "#4f8cff",
            backgroundColor: gradientCpu,
            borderWidth: 2,
            fill: true,
            tension: 0.4,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: "#4f8cff",
            pointHoverBorderColor: "#fff",
            pointHoverBorderWidth: 2,
          },
          {
            label: "Memory %",
            data: memData.points.map(p => p.value),
            borderColor: "#10b981",
            backgroundColor: gradientMem,
            borderWidth: 2,
            fill: true,
            tension: 0.4,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: "#10b981",
            pointHoverBorderColor: "#fff",
            pointHoverBorderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: true,
            position: "top",
            align: "end",
            labels: {
              color: "#8896b3",
              font: { size: 11, weight: "600" },
              boxWidth: 8,
              boxHeight: 8,
              usePointStyle: true,
              pointStyle: "circle",
              padding: 12,
            },
          },
          tooltip: {
            backgroundColor: "#1a2336",
            borderColor: "#2a3550",
            borderWidth: 1,
            padding: 10,
            titleColor: "#eef2ff",
            bodyColor: "#c7d2e8",
            titleFont: { size: 12, weight: "700" },
            bodyFont: { size: 12 },
            displayColors: true,
            boxPadding: 4,
          },
        },
        scales: {
          x: {
            ticks: { color: "#8896b3", maxTicksLimit: 8, font: { size: 10 } },
            grid: { display: false },
            border: { display: false },
          },
          y: {
            ticks: { color: "#8896b3", font: { size: 10 }, padding: 8 },
            grid: { color: chartGridColor(), drawTicks: false },
            border: { display: false },
            beginAtZero: true,
            suggestedMax: 100,
          },
        },
        animation: { duration: 700, easing: "easeOutQuart" },
      },
    });
  } catch (e) {
    console.warn("Chart error:", e);
  }
}

document.getElementById("chart-range")?.addEventListener("change", drawChart);

// =================== OLT ===================
// =================== OLT MANAGEMENT (1a-1c) ===================
let _oltDriversCache = null;

async function _getOltDrivers() {
  if (_oltDriversCache) return _oltDriversCache;
  try {
    _oltDriversCache = await api("/olts/drivers");
    return _oltDriversCache;
  } catch (e) {
    toast("Gagal ambil daftar driver: " + e.message, "error");
    return [];
  }
}

function _oltStatusClass(status) {
  if (status === "online") return "online";
  if (status === "offline") return "offline";
  if (status === "degraded") return "degraded";
  return "unknown";
}

function _miniBarClass(val) {
  if (val == null) return "";
  if (val >= 90) return "critical";
  if (val >= 75) return "warn";
  return "";
}

function _vendorLabel(driverKey) {
  if (!driverKey) return "ZTE";
  if (driverKey.startsWith("zte")) return "ZTE";
  if (driverKey.startsWith("huawei")) return "HUAWEI";
  if (driverKey.startsWith("nokia")) return "NOKIA";
  return driverKey.split("_")[0].toUpperCase();
}

async function loadOLTs(opts = {}) {
  const el = document.getElementById("olt-list");
  if (!el) return;
  el.innerHTML = '<div class="olt-empty"><i class="fas fa-spinner fa-spin"></i><p>Memuat OLT...</p></div>';
  try {
    // Phase B3: cache daftar admin untuk badge owner
    if (CURRENT_USER.is_super_admin) {
      try {
        const users = await api("/users");
        window._oltOwnersCache = users;
      } catch(_) { window._oltOwnersCache = []; }
    }
    const olts = await api("/olts", opts);
    if (!olts.length) {
      el.innerHTML = '<div class="olt-empty">' +
        '<i class="fas fa-server"></i>' +
        '<h3>Belum ada OLT terdaftar</h3>' +
        '<p>Tambahkan OLT pertama untuk mulai monitoring.</p>' +
        '<button class="btn-primary" onclick="openOltModal()"><i class="fas fa-plus"></i> Add New OLT</button>' +
        '</div>';
      return;
    }
    el.innerHTML = '<div class="olt-grid">' + olts.map(_renderOltCard).join("") + '</div>';
  } catch (e) {
    if (e.name !== "AbortError") {
      el.innerHTML = '<div class="olt-empty"><i class="fas fa-exclamation-triangle"></i><p>Gagal memuat: ' + escapeHtml(e.message) + '</p></div>';
    }
  }
}

function _renderOltCard(o) {
  const cpuVal = o.cpu_usage != null ? o.cpu_usage : null;
  const memVal = o.memory_usage != null ? o.memory_usage : null;
  const cpuBar = _miniBarClass(cpuVal);
  const memBar = _miniBarClass(memVal);
  const isDisabled = o.enabled === 0;
  const isAdmin = CURRENT_USER.privilege >= 15;
  const isOp = CURRENT_USER.privilege >= 10;
  const vendor = _vendorLabel(o.driver);

  let actions = '';
  if (isAdmin) {
    actions += '<button class="btn-ghost" onclick="openOltEditModal(' + o.id + ')"><i class="fas fa-edit"></i> Edit</button>';
    actions += '<button class="btn-ghost" onclick="toggleOltEnabled(' + o.id + ', ' + (o.enabled != null ? o.enabled : 1) + ')"><i class="fas fa-power-off"></i> ' + (isDisabled ? 'Enable' : 'Disable') + '</button>';
    actions += '<button class="btn-danger" onclick="deleteOltConfirm(' + o.id + ', &quot;' + escapeHtml(o.hostname) + '&quot;)"><i class="fas fa-trash"></i> Remove</button>';
  } else if (isOp) {
    actions += '<button class="btn-ghost" onclick="openOltEditModal(' + o.id + ')"><i class="fas fa-eye"></i> View</button>';
  }

  // Phase B3: badge owner
  let ownerLabel = "JSN Pusat";
  let ownerClass = "multivers";
  if (o.owner_user_id) {
    const owner = (window._oltOwnersCache || []).find(x => x.id === o.owner_user_id);
    ownerLabel = owner ? (owner.full_name || owner.username) : ("User #" + o.owner_user_id);
    ownerClass = "";
  }

  return '<div class="olt-card ' + (isDisabled ? 'olt-disabled' : '') + '">' +
    '<div class="olt-card-head">' +
      '<div class="olt-card-icon"><i class="fas fa-server"></i></div>' +
      '<div class="olt-card-name">' + escapeHtml(o.hostname || "OLT") + '</div>' +
      '<span class="olt-card-vendor">' + escapeHtml(vendor) + '</span>' +
      '<span class="olt-card-status ' + _oltStatusClass(o.status) + '"><span class="dot"></span>' + escapeHtml(o.status || "unknown") + '</span>' +
    '</div>' +
    '<div style="margin-bottom:10px"><span class="olt-card-owner ' + ownerClass + '" title="Pemilik OLT"><i class="fas fa-user-shield"></i> ' + escapeHtml(ownerLabel) + '</span></div>' +
    '<div class="olt-card-body">' +
      '<div class="olt-row"><div class="olt-row-label">IP / Port</div><div class="olt-row-value"><code>' + escapeHtml(o.ip_address) + ':' + (o.port || 23) + '</code></div></div>' +
      '<div class="olt-row"><div class="olt-row-label">Model / FW</div><div class="olt-row-value">' + escapeHtml(o.model || "-") + ' / ' + escapeHtml(o.firmware || "-") + '</div></div>' +
      '<div class="olt-row"><div class="olt-row-label">CPU</div><div class="olt-row-value"><span class="olt-mini-bar ' + cpuBar + '"><span style="width:' + (cpuVal || 0) + '%"></span></span>' + (cpuVal != null ? cpuVal.toFixed(1) + '%' : '-') + '</div></div>' +
      '<div class="olt-row"><div class="olt-row-label">Memory</div><div class="olt-row-value"><span class="olt-mini-bar ' + memBar + '"><span style="width:' + (memVal || 0) + '%"></span></span>' + (memVal != null ? memVal.toFixed(1) + '%' : '-') + '</div></div>' +
      '<div class="olt-row"><div class="olt-row-label">Uptime</div><div class="olt-row-value">' + formatUptime(o.uptime_seconds) + '</div></div>' +
      '<div class="olt-row"><div class="olt-row-label">Location</div><div class="olt-row-value">' + escapeHtml(o.location || "-") + '</div></div>' +
    '</div>' +
    '<div class="olt-card-actions">' + actions + '</div>' +
  '</div>';
}

async function openOltModal() {
  if (CURRENT_USER.privilege < 15) { toast("Hanya admin yang bisa tambah OLT", "error"); return; }
  const drivers = await _getOltDrivers();
  if (!drivers.length) { toast("Tidak ada driver tersedia", "error"); return; }
  _renderOltModal({ mode: "add", drivers, olt: null });
}

async function openOltEditModal(id) {
  if (CURRENT_USER.privilege < 15) { toast("Hanya admin yang bisa edit OLT", "error"); return; }
  try {
    const olt = await api("/olts/" + id);
    const drivers = await _getOltDrivers();
    _renderOltModal({ mode: "edit", drivers, olt });
  } catch (e) {
    toast("Gagal load OLT: " + e.message, "error");
  }
}

function _renderOltModal(info) {
  const isEdit = info.mode === "edit";
  const v = info.olt || {};
  const drivers = info.drivers;

  const driverOpts = drivers.map(function(d) {
    const selected = (v.driver || "zte_zxan") === d.key ? " selected" : "";
    const exp = d.verified ? "" : " (Experimental)";
    return '<option value="' + d.key + '"' + selected + '>' + escapeHtml(d.label) + exp + '</option>';
  }).join("");

  const activeDriver = drivers.find(function(d){ return d.key === (v.driver || "zte_zxan"); }) || drivers[0];
  const hwOptions = (activeDriver ? activeDriver.hardware : []).map(function(h) {
    const selected = (v.hardware_type || "zte-c320") === h.key ? " selected" : "";
    return '<option value="' + h.key + '"' + selected + '>' + escapeHtml(h.label) + '</option>';
  }).join("");

  const pwPlaceholder = isEdit ? "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 (kosongkan jika tidak diubah)" : "Masukkan Password Telnet";

  // Phase B3: field Owner (khusus Super Admin)
  const isMulti = CURRENT_USER.is_super_admin === 1;
  let ownerField = "";
  if (isMulti) {
    const admins = (window._oltOwnersCache || []).filter(u => u.role === "admin" && !u.is_super_admin);
    const ownerOpts = '<option value=""' + (!v.owner_user_id ? ' selected' : '') + '>JSN Pusat (Super Admin)</option>' +
      admins.map(a => '<option value="' + a.id + '"' + (v.owner_user_id === a.id ? ' selected' : '') + '>' + escapeHtml(a.full_name || a.username) + ' (' + escapeHtml(a.username) + ')</option>').join("");
    ownerField = '<div class="form-group" style="background:var(--violet-soft);border:1px solid var(--violet-ring);padding:12px;border-radius:8px;margin-bottom:16px">' +
      '<label style="color:var(--violet)"><i class="fas fa-user-shield"></i> Pemilik OLT</label>' +
      '<select id="olt-owner">' + ownerOpts + '</select>' +
      '<div style="font-size:11px;color:var(--text-dim);margin-top:6px">Kosongkan atau pilih JSN Pusat jika OLT ini milik pusat. Pilih admin jika OLT ini dikelola admin wilayah.</div>' +
    '</div>';
  }

  const bodyHTML =
    ownerField +
    '<div class="form-group"><label>OLT Name</label><input id="olt-hostname" type="text" placeholder="Contoh: OLT-UTAMA-01" value="' + escapeHtml(v.hostname || "") + '"></div>' +
    '<div class="form-group"><label>IP Address</label><input id="olt-ip" type="text" placeholder="Contoh: 192.168.1.100" value="' + escapeHtml(v.ip_address || "") + '"></div>' +
    '<div class="form-group"><label>Type &amp; Hardware (Driver)</label><select id="olt-driver">' + driverOpts + '</select></div>' +
    '<div class="form-group"><label>Hardware Type</label><select id="olt-hardware">' + hwOptions + '</select></div>' +
    '<h4 style="margin:20px 0 10px;font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.06em">Telnet (wajib)</h4>' +
    '<div class="form-group"><label>Username</label><input id="olt-username" type="text" value="' + escapeHtml(v.username || "") + '"></div>' +
    '<div class="form-group"><label>Password</label><input id="olt-password" type="password" placeholder="' + pwPlaceholder + '" value=""></div>' +
    '<div class="form-group"><label>Enable Password (opsional)</label><input id="olt-enable" type="password" placeholder="' + (isEdit ? "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 (kosongkan jika tidak diubah)" : "zxr10") + '" value=""></div>' +
    '<div class="form-group"><label>Telnet Port</label><input id="olt-port" type="number" min="1" max="65535" value="' + (v.port || 23) + '"><small style="color:var(--text-dim);font-size:11px;display:block;margin-top:4px">Standar 23 (LAN). Pakai 779 jika lewat IP publik.</small></div>' +
    '<h4 style="margin:20px 0 10px;font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.06em">SNMP</h4>' +
    '<div class="form-group"><label>SNMP Community RO</label><input id="olt-snmp-ro" type="text" placeholder="public" value="' + escapeHtml(v.snmp_community_ro || "public") + '"></div>' +
    '<div class="form-group"><label>SNMP Port</label><input id="olt-snmp-port" type="number" min="1" max="65535" value="' + (v.snmp_port || 161) + '"><small style="color:var(--text-dim);font-size:11px;display:block;margin-top:4px">Standar 161. Biarkan default jika OLT tidak diakses via SNMP.</small></div>' +
    '<div class="form-group"><label>Location (opsional)</label><input id="olt-location" type="text" value="' + escapeHtml(v.location || "") + '"></div>' +
    '<div id="olt-test-result" class="olt-test-result"></div>';

  openModal(isEdit ? ("Edit OLT: " + (v.hostname || "")) : "OLT Connection Settings", bodyHTML, [
    { label: "Close", cls: "btn-secondary", action: closeModal },
    { label: "\u26A1 Test Connection", cls: "btn-ghost", action: function(){ _testOltConnection(isEdit ? v.id : null); } },
    { label: "Submit", cls: "btn-primary", action: function(){ _submitOltForm(info.mode, isEdit ? v.id : null); } },
  ]);

  const drvSel = document.getElementById("olt-driver");
  const hwSel = document.getElementById("olt-hardware");
  if (drvSel && hwSel) {
    drvSel.addEventListener("change", function() {
      const d = drivers.find(function(x){ return x.key === drvSel.value; });
      hwSel.innerHTML = (d ? d.hardware : []).map(function(h) {
        return '<option value="' + h.key + '">' + escapeHtml(h.label) + '</option>';
      }).join("");
    });
  }
}

// ============================================================
// OLT Connection Test Guard — state & helpers
// ============================================================
let _lastTest = null;

const _TELNET_FIELDS = ["ip", "driver", "username", "password", "enable", "port"];
const _SNMP_FIELDS   = ["ip", "driver", "snmpRo", "snmpPort"];

function _readConnectionFields() {
  return {
    ip:       (document.getElementById("olt-ip") || {}).value || "",
    driver:   (document.getElementById("olt-driver") || {}).value || "",
    username: (document.getElementById("olt-username") || {}).value || "",
    password: (document.getElementById("olt-password") || {}).value || "",
    enable:   (document.getElementById("olt-enable") || {}).value || "",
    port:     (document.getElementById("olt-port") || {}).value || "",
    snmpRo:   (document.getElementById("olt-snmp-ro") || {}).value || "",
    snmpPort: (document.getElementById("olt-snmp-port") || {}).value || "",
  };
}

function _checkTestState(oltId) {
  if (!_lastTest || _lastTest.oltId !== (oltId || null)) {
    return { action: "warn_untested", message: "ℹ️ Belum dites.\n\nYakin submit tanpa test?" };
  }
  const current = _readConnectionFields();
  const snap = _lastTest.snapshot;
  const telnetStale = _TELNET_FIELDS.some(f => current[f] !== snap[f]);
  const snmpStale   = _SNMP_FIELDS.some(f => current[f] !== snap[f]);
  if (telnetStale || snmpStale) {
    return { action: "warn_stale", message: "ℹ️ Setting berubah.\n\nYakin submit tanpa test ulang?" };
  }
  if (!_lastTest.telnet.ok) {
    return { action: "warn_telnet", message: "⚠️ Telnet gagal.\n\nProvisioning & sync OLT tidak akan jalan.\n\nYakin submit?" };
  }
  if (!_lastTest.snmp.ok) {
    return { action: "warn_snmp", message: "⚠️ SNMP gagal.\n\nMonitoring CPU/Mem/Port status tidak akan jalan.\n\nYakin submit?" };
  }
  return { action: "submit", message: null };
}

async function _testOltConnection(oltId) {
  _lastTest = null;
  const ip = (document.getElementById("olt-ip") || {}).value || "";
  const port = parseInt((document.getElementById("olt-port") || {}).value || "23", 10);
  const user = (document.getElementById("olt-username") || {}).value || "";
  const pass = (document.getElementById("olt-password") || {}).value || "";
  const enable = (document.getElementById("olt-enable") || {}).value || "";
  const snmpCommunity = (document.getElementById("olt-snmp-ro") || {}).value || "";
  const snmpPort = parseInt((document.getElementById("olt-snmp-port") || {}).value || "161", 10);
  const result = document.getElementById("olt-test-result");
  if (!result) return;

  if (!ip.trim() || !user.trim()) {
    result.className = "olt-test-result show err";
    result.innerHTML = "<b>Lengkapi IP dan Username dulu</b>";
    return;
  }
  if (!pass) {
    result.className = "olt-test-result show err";
    result.innerHTML = "<b>Password wajib diisi untuk test</b>";
    return;
  }

  result.className = "olt-test-result show";
  result.style.background = "var(--surface-2)";
  result.style.color = "var(--text-dim)";
  result.style.border = "1px solid var(--border)";
  result.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Menguji Telnet + SNMP...';

  try {
    const res = await api("/olts/test-connection-full", {
      method: "POST",
      body: JSON.stringify({
        ip_address: ip.trim(),
        port: port,
        username: user.trim(),
        password: pass,
        enable_password: enable || null,
        protocol: "telnet",
        snmp_community_ro: snmpCommunity.trim() || null,
        snmp_port: snmpPort,
      }),
    });

    // Reset container styling (per-baris punya class sendiri)
    result.className = "olt-test-result show";
    result.style.background = "transparent";
    result.style.color = "inherit";
    result.style.border = "none";

    const t = res.telnet || {};
    const s = res.snmp || {};

    _lastTest = {
      oltId: oltId || null,
      telnet: { ok: !!t.ok, error: t.error || null },
      snmp:   { ok: !!s.ok,   error: s.error || null },
      snapshot: _readConnectionFields(),
    };

    const telnetRow = t.ok
      ? '<div class="olt-test-row ok">' +
          '<b><i class="fas fa-bolt"></i> Telnet &nbsp;✅ Berhasil' +
            (t.latency_ms != null ? ' <span style="opacity:.7">(' + t.latency_ms + 'ms)</span>' : '') +
          '</b>' +
          'Hostname: <b>' + escapeHtml(t.hostname || "-") + '</b> · Model: ' + escapeHtml(t.model || "-") +
          ' · Firmware: ' + escapeHtml(t.firmware || "-") +
        '</div>'
      : '<div class="olt-test-row err">' +
          '<b><i class="fas fa-bolt"></i> Telnet &nbsp;❌ Gagal' +
            (t.latency_ms != null ? ' <span style="opacity:.7">(' + t.latency_ms + 'ms)</span>' : '') +
          '</b>' +
          '<code>' + escapeHtml(t.error || "Unknown error") + '</code>' +
        '</div>';

    const snmpRow = s.ok
      ? '<div class="olt-test-row ok">' +
          '<b><i class="fas fa-satellite-dish"></i> SNMP &nbsp;✅ Berhasil' +
            (s.latency_ms != null ? ' <span style="opacity:.7">(' + s.latency_ms + 'ms)</span>' : '') +
          '</b>' +
          'sysDescr: ' + escapeHtml(s.sys_descr || "-") +
          '<br>Uptime: ' + escapeHtml(String(s.uptime_seconds != null ? s.uptime_seconds : "-")) + ' detik' +
        '</div>'
      : '<div class="olt-test-row err">' +
          '<b><i class="fas fa-satellite-dish"></i> SNMP &nbsp;❌ Gagal' +
            (s.latency_ms != null ? ' <span style="opacity:.7">(' + s.latency_ms + 'ms)</span>' : '') +
          '</b>' +
          '<code>' + escapeHtml(s.error || "Unknown error") + '</code>' +
        '</div>';

    result.innerHTML = telnetRow + snmpRow;
  } catch (e) {
    result.className = "olt-test-result show";
    result.style.background = "transparent";
    result.style.color = "inherit";
    result.style.border = "none";
    const msg = escapeHtml(e.message || "Request gagal");
    result.innerHTML =
      '<div class="olt-test-row err"><b><i class="fas fa-bolt"></i> Telnet &nbsp;❌ Gagal</b><code>' + msg + '</code></div>' +
      '<div class="olt-test-row err"><b><i class="fas fa-satellite-dish"></i> SNMP &nbsp;❌ Gagal</b><code>' + msg + '</code></div>';
  }
}

async function _submitOltForm(mode, oltId) {
  const isEdit = mode === "edit";
  const payload = {
    hostname: ((document.getElementById("olt-hostname") || {}).value || "").trim(),
    ip_address: ((document.getElementById("olt-ip") || {}).value || "").trim(),
    port: parseInt((document.getElementById("olt-port") || {}).value || "23", 10),
    username: ((document.getElementById("olt-username") || {}).value || "").trim(),
    password: (document.getElementById("olt-password") || {}).value || "",
    enable_password: (document.getElementById("olt-enable") || {}).value || "",
    snmp_community_ro: ((document.getElementById("olt-snmp-ro") || {}).value || "").trim() || "public",
    snmp_port: parseInt((document.getElementById("olt-snmp-port") || {}).value || "161", 10),
    driver: (document.getElementById("olt-driver") || {}).value,
    hardware_type: (document.getElementById("olt-hardware") || {}).value,
    location: ((document.getElementById("olt-location") || {}).value || "").trim() || null,
  };

  // Phase B3: owner_user_id (khusus Super Admin)
  if (CURRENT_USER.is_super_admin) {
    const ownerEl = document.getElementById("olt-owner");
    if (ownerEl) {
      const ownerVal = ownerEl.value;
      payload.owner_user_id = ownerVal ? parseInt(ownerVal, 10) : null;
    }
  }

  if (!payload.hostname || !payload.ip_address || !payload.username) {
    toast("Hostname, IP, dan Username wajib diisi", "error");
    return;
  }
  if (!isEdit && !payload.password) {
    toast("Password wajib diisi untuk OLT baru", "error");
    return;
  }
  if (isEdit) {
    if (!payload.password) delete payload.password;
    if (!payload.enable_password) delete payload.enable_password;
  }

  const check = _checkTestState(isEdit ? oltId : null);
  if (check.action !== "submit" && !window.confirm(check.message)) {
    return;
  }

  await _doSubmitOltForm(mode, oltId, payload);
}

async function _doSubmitOltForm(mode, oltId, payload) {
  const isEdit = mode === "edit";
  try {
    if (isEdit) {
      await api("/olts/" + oltId, { method: "PATCH", body: JSON.stringify(payload) });
      toast("OLT berhasil diupdate", "success");
    } else {
      await api("/olts", { method: "POST", body: JSON.stringify(payload) });
      toast("OLT berhasil ditambahkan", "success");
    }
    closeModal();
    _oltDriversCache = null;
    _lastTest = null;
    await loadOLTs();
  } catch (e) {
    toast("Gagal simpan: " + e.message, "error");
  }
}

async function deleteOltConfirm(id, hostname) {
  if (CURRENT_USER.privilege < 15) { toast("Hanya admin", "error"); return; }
  const ok = confirm('Hapus OLT "' + hostname + '"?\n\nSemua data terkait (ONU, VLAN, event, alert) akan hilang dan tidak bisa dikembalikan.');
  if (!ok) return;
  try {
    await api("/olts/" + id, { method: "DELETE" });
    toast('OLT "' + hostname + '" dihapus', "success");
    await loadOLTs();
  } catch (e) {
    toast("Gagal hapus: " + e.message, "error");
  }
}

async function toggleOltEnabled(id, currentEnabled) {
  if (CURRENT_USER.privilege < 15) { toast("Hanya admin", "error"); return; }
  const newEnabled = currentEnabled === 0 ? 1 : 0;
  try {
    await api("/olts/" + id, { method: "PATCH", body: JSON.stringify({ enabled: newEnabled }) });
    toast(newEnabled ? "OLT diaktifkan" : "OLT dinonaktifkan (polling berhenti)", "success");
    await loadOLTs();
  } catch (e) {
    toast("Gagal: " + e.message, "error");
  }
}

document.getElementById("btn-add-olt")?.addEventListener("click", openOltModal);
document.getElementById("btn-refresh-olts")?.addEventListener("click", function(){ loadOLTs(); refreshFreshness(); });

// =================== PON ===================
function ifaceState(i) {
  if (i.admin_state === "shutdown") return "shutdown";
  return i.status === "up" ? "up" : "down";
}

function ponState(p) {
  const admin = (p.admin_state || "").toLowerCase();
  const status = (p.status || "").toLowerCase();
  if (admin === "shutdown" || status === "shutdown") return "shutdown";
  return status === "up" ? "up" : "down";
}

function portNoOrder(a, b) {
  const x = String(a.port_no).split("/").map(Number);
  const y = String(b.port_no).split("/").map(Number);
  for (let k = 0; k < Math.max(x.length, y.length); k++) {
    const d = (x[k] || 0) - (y[k] || 0);
    if (d) return d;
  }
  return 0;
}

// ===== Halaman Port (Tahap 2) =====
let portData = { pons: null, ifaces: null };
let portLoading = false;

function setPortTab(tab) {
  portTab = tab;
  renderPortPane();
}

function renderPortSummary(pons, ifaces) {
  const el = document.getElementById("port-summary");
  if (!el) return;
  const states = (pons && ifaces) ? [...pons.map(ponState), ...ifaces.map(ifaceState)] : null;
  const n = s => states ? states.filter(x => x === s).length : "–";
  const cards = [
    ["Total Port", states ? states.length : "–", "", null],
    ["Up", n("up"), "up", "up"],
    ["Down", n("down"), n("down") > 0 ? "down" : "", "down"],
    ["Shutdown", n("shutdown"), "shutdown", "shutdown"],
  ];
  el.innerHTML = cards.map(([label, val, cls, filter]) => {
    const active = filter !== null && filter === portFilter;
    const arg = filter ? `'${filter}'` : "null";
    return `<button type="button" class="port-stat ${cls}${active ? " active" : ""}" aria-pressed="${active}" onclick="setPortFilter(${arg})">
      <div class="port-stat-val">${val}</div><div class="port-stat-label">${label}</div></button>`;
  }).join("");
}

function renderPortAll(pons, ifaces) {
  const el = document.getElementById("port-table-all");
  if (!el || (!pons && !ifaces)) return;
  const btn = (kind, name, admin) => {
    const on = admin === "no shutdown";
    return `<button class="btn-icon" onclick="togglePort('${kind}', '${escapeHtml(name)}', '${on ? "disable" : "enable"}')" title="${on ? "Disable port" : "Enable port"}">
      <i class="fas fa-power-off" style="color:${on ? "var(--green)" : "var(--text-dim)"}"></i></button>`;
  };
  const rows = [
    ...(ifaces || []).map(i => ({ kind: "uplink", name: i.name, type: `Uplink · ${i.type}`, state: ifaceState(i), admin: i.admin_state, info: i.speed ? `${i.speed} Mbps` : "-" })),
    ...[...(pons || [])].sort(portNoOrder).map(p => ({ kind: "gpon", name: p.port_no, type: "PON", state: ponState(p), admin: p.admin_state, info: `${p.onu_count} ONU` })),
  ];
  el.innerHTML = `
    <div class="table-wrap"><table class="table-premium">
      <thead><tr><th>Port</th><th>Tipe</th><th>Status</th><th>Admin</th><th>Speed / ONU</th><th>Aksi</th></tr></thead>
      <tbody>
        ${rows.map(r => `
          <tr>
            <td><b>${escapeHtml(r.name)}</b></td>
            <td>${escapeHtml(r.type)}</td>
            <td><span class="status-badge ${r.state}">${r.state}</span></td>
            <td>${escapeHtml(r.admin)}</td>
            <td>${escapeHtml(r.info)}</td>
            <td>${btn(r.kind, r.name, r.admin)}</td>
          </tr>`).join("")}
      </tbody>
    </table></div>`;
}

function setPortFilter(state) {
  portFilter = (state === portFilter) ? null : (state || null); // klik kartu aktif → reset
  renderPortPane();
}

function renderPortPane() {
  document.querySelectorAll("#port-tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === portTab));
  document.querySelectorAll(".port-pane").forEach(p => p.classList.toggle("active", p.id === `port-table-${portTab}`));
  const { pons, ifaces } = portData;
  const filterData = (arr, stateFn) => portFilter ? arr.filter(item => stateFn(item) === portFilter) : arr;
  const filteredPons = pons ? filterData(pons, ponState) : null;
  const filteredIfaces = ifaces ? filterData(ifaces, ifaceState) : null;
  renderPortSummary(pons, ifaces); // data lengkap, supaya angka kartu tidak berubah
  if (portTab === "pon") { if (filteredPons) loadPONDetail({}, filteredPons); }
  else if (portTab === "uplink") { if (filteredIfaces) loadInterfaces({}, filteredIfaces); }
  else renderPortAll(filteredPons, filteredIfaces);
}

async function loadPortPage(opts = {}) {
  if (portLoading) return;
  portLoading = true;
  try {
    const [rp, ri] = await Promise.allSettled([
      api(`/olts/${CURRENT_OLT_ID}/pons`, opts),
      api(`/olts/${CURRENT_OLT_ID}/interfaces`, opts),
    ]);
    if ([rp, ri].some(r => r.status === "rejected" && r.reason?.name === "AbortError")) return;
    if (rp.status === "fulfilled") portData.pons = rp.value;
    if (ri.status === "fulfilled") portData.ifaces = ri.value;
    [rp, ri].forEach(r => { if (r.status === "rejected") toast(r.reason.message, "error"); });
    if (currentPage === "port") renderPortPane();
  } finally {
    portLoading = false;
  }
}

// ===== Tab halaman VLAN (Tahap 3a) =====
function renderVlanPane() {
  document.querySelectorAll("#vlan-tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === vlanTab));
  document.querySelectorAll(".vlan-pane").forEach(p => p.classList.toggle("active", p.id === `vlan-pane-${vlanTab}`));
}

function setVlanTab(tab) {
  vlanTab = tab;
  renderVlanPane();
  if (vlanTab === "port") loadPortVLANTable();
      else if (vlanTab === "vlanif") loadVlanifTable();
      else loadVLANs();
}

async function loadPortVLANTable(opts = {}) {
  const el = document.getElementById("port-vlan-table");
  if (!el) return;
  try {
    const ifaces = await api(`/olts/${CURRENT_OLT_ID}/interfaces`, opts);
    el.innerHTML = `
      <div class="table-wrap"><table class="table-premium">
        <thead><tr>
          <th>Interface</th><th>Mode</th><th>Native VLAN</th><th>Tag VLAN</th><th>Attribute</th><th>Aksi</th>
        </tr></thead>
        <tbody>
          ${ifaces.map(i => `
            <tr>
              <td><b>${escapeHtml(i.name)}</b></td>
              <td>${escapeHtml(i.switchport_mode || "-")}</td>
              <td title="Belum tersedia dari API">-</td>
              <td>${escapeHtml(i.vlans || "-")}</td>
              <td>${escapeHtml(i.hybrid_attribute || "-")}</td>
              <td style="white-space:nowrap">
                <button class="btn-icon" onclick="openPortEdit('${escapeHtml(i.name)}', '${escapeHtml(i.switchport_mode || "")}', ${i.vlans ? `'${escapeHtml(i.vlans)}'` : "null"}, ${i.hybrid_attribute ? `'${escapeHtml(i.hybrid_attribute)}'` : "null"})" title="Edit port VLAN">
                  <i class="fas fa-pen"></i>
                </button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table></div>
    `;
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}

async function loadPONDetail(opts = {}, pre = null) {
  const el = document.getElementById("pon-detail");
  try {
    const pons = pre || await api(`/olts/${CURRENT_OLT_ID}/pons`, opts);
    el.innerHTML = `
      <div class="widget">
        <div class="widget-header">
          <h3><i class="fas fa-broadcast-tower"></i> 16 PON Ports</h3>
        </div>
        <table>
          <thead><tr><th>Port</th><th>Status</th><th>Admin</th><th>TX (dBm)</th><th>RX (dBm)</th><th>ONU</th><th>Linktrap</th><th>Aksi</th></tr></thead>
          <tbody>
            ${[...pons].sort(portNoOrder).map(p => `
              <tr>
                <td><b>${escapeHtml(p.port_no)}</b></td>
                <td><span class="status-badge ${ponState(p)}">${ponState(p)}</span></td>
                <td>${escapeHtml(p.admin_state)}</td>
                <td>${p.optical_tx ?? "-"}</td>
                <td>${p.optical_rx ?? "-"}</td>
                <td>${p.onu_count}</td>
                <td>${p.linktrap ? "✓" : "✗"}</td>
                <td>
                  <button class="btn-icon" onclick="togglePort('gpon', '${escapeHtml(p.port_no)}', '${p.admin_state === "no shutdown" ? "disable" : "enable"}')" title="${p.admin_state === "no shutdown" ? "Disable port" : "Enable port"}">
                    <i class="fas fa-power-off" style="color:${p.admin_state === "no shutdown" ? "var(--green)" : "var(--text-dim)"}"></i>
                  </button>
                </td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    `;
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}

// =================== ONU ===================
// =================== VENDOR DETECTION (Frontend Mirror) ===================
const VENDOR_PREFIX_MAP = {
  "ZTEG": "ZTE", "YYKC": "ZTE", "ZTEC": "ZTE", "ZTEZ": "ZTE",
  "HWTC": "Huawei",
  "FHTT": "FiberHome",
  "ALCL": "Alcatel-Lucent",
};

function detectVendorFromSN(sn) {
  if (!sn) return "Unknown";
  const prefix = sn.trim().toUpperCase().slice(0, 4);
  return VENDOR_PREFIX_MAP[prefix] || "Unknown";
}

function isRoutedSupported(vendor) {
  return vendor === "ZTE";
}

function renderVendorBadge(o) {
  const label = o.vendor || "Unknown";
  // Semua vendor warna hijau (konsisten) — badge cuma info merek
  return `<span class="status-badge online" title="Merek: ${label}">${label}</span>`;
}

function renderInternetStatus(o) {
  const onuStatus = (o.status || "unknown").toLowerCase();
  const status = (o.pppoe_status || "unknown").toLowerCase();
  const mode = (o.provisioning_mode || "routed").toLowerCase();
  const dur = o.pppoe_online_duration || 0;
  let cls, label, icon, title;

  // ⭐ BRIDGE MODE — status tergantung konfirmasi user
  if (mode === "bridge" || status === "bridge") {
    if (onuStatus !== "online") {
      return `<span class="status-badge offline" title="ONU offline"><i class="fas fa-times-circle"></i> DISCONNECTED</span>`;
    }
    if (o.bridge_configured) {
      return `<span class="status-badge online" title="User sudah set PPPoE di GUI modem"><i class="fas fa-check-circle"></i> CONNECTED</span>`;
    }
    return `<span class="status-badge warning" title="Belum dikonfigurasi — set PPPoE di GUI modem lalu klik ✓ Aktif"><i class="fas fa-hourglass-half"></i> BELUM SETUP</span>`;
  }

  // ⭐ ATURAN LOGIS: kalau ONU LOS/OFFLINE/DYING_GASP → internet PASTI mati
  if (onuStatus === "los" || onuStatus === "offline" || onuStatus === "dying_gasp") {
    cls = "offline";
    label = "DISCONNECTED";
    icon = "fa-times-circle";
    title = onuStatus === "los"
      ? "Fiber putus (LOS) — internet pasti mati"
      : onuStatus === "dying_gasp"
      ? "ONU baru mati (dying-gasp) — kemungkinan cabut adaptor"
      : "Modem mati — internet pasti mati";
    return `<span class="status-badge ${cls}" title="${title}"><i class="fas ${icon}"></i> ${label}</span>`;
  }

  if (status === "connected") {
    // CONNECTED — hijau
    cls = "online";
    label = dur > 0 ? `CONNECTED · ${formatUptime(dur)}` : "CONNECTED";
    icon = "fa-check-circle";
    title = "PPPoE aktif — modem dapat internet";
  } else if (
    status === "disconnected" ||
    status === "dial_failed" ||
    status === "auth_failed" ||
    status === "no_signal"
  ) {
    // DISCONNECTED — merah
    cls = "offline";
    label = "DISCONNECTED";
    icon = "fa-times-circle";
    title = status === "auth_failed"
      ? "Kredensial PPPoE salah"
      : status === "dial_failed"
      ? "PPPoE gagal dial setelah timeout"
      : "PPPoE terputus — cek modem/kabel";
  } else {
    // CHECKING / UNKNOWN — kuning
    cls = "warning";
    label = "CHECKING...";
    icon = "fa-spinner fa-spin";
    title = "Sedang cek status PPPoE";
  }

  return `<span class="status-badge ${cls}" title="${title}"><i class="fas ${icon}"></i> ${label}</span>`;
}

// =================== TRAFFIC PAGE ===================
async function loadTrafficPage(opts = {}) {
  const el = document.getElementById("traffic-grid");
  if (!el) return;
  if (document.hidden && !opts.force) return;

  if (_trafficAbort) { try { _trafficAbort.abort(); } catch(_) {} }
  _trafficAbort = new AbortController();
  const mySignal = _trafficAbort.signal;

  if (opts.signal) {
    if (opts.signal.aborted) { try { _trafficAbort.abort(); } catch(_) {} }
    else opts.signal.addEventListener("abort", () => { try { _trafficAbort.abort(); } catch(_) {} }, { once: true });
  }

  const scope = document.getElementById("traffic-scope")?.value || "pon";
  const minutes = document.getElementById("traffic-period")?.value || "240";
  const url = scope === "onu"
    ? `/olts/${CURRENT_OLT_ID}/traffic/onu/recent?minutes=${minutes}`
    : `/olts/${CURRENT_OLT_ID}/traffic/recent?scope=${scope}&minutes=${minutes}`;

  if (Object.keys(_trafficCharts).length === 0) {
    el.innerHTML = '<div style="text-align:center;padding:40px"><i class="fas fa-spinner fa-spin"></i> Memuat traffic...</div>';
  }

  try {
    const r = await api(url, { ...opts, signal: mySignal });

    if (!r.entities || !r.entities.length) {
      _destroyAllTrafficCharts();
      _trafficStateKey = "";
      el.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-dim)">Tidak ada data traffic</div>';
      return;
    }

    const keys = r.entities.map(e => _trafficEntityKey(e));
    const stateKey = scope + "|" + minutes + "|" + keys.join(",");
    const structureChanged = stateKey !== _trafficStateKey;

    if (structureChanged) {
      _rebuildTrafficUI(el, r.entities, scope);
      _trafficStateKey = stateKey;
    } else {
      _updateTrafficUI(el, r.entities);
    }
  } catch (e) {
    if (e.name === "AbortError") return;
    if (Object.keys(_trafficCharts).length === 0) {
      el.innerHTML = '<div style="text-align:center;padding:40px;color:var(--red)">Gagal: ' + escapeHtml(e.message) + '</div>';
    }
  }
}

function _trafficEntityKey(e) {
  return e.onu_id ? "onu_" + e.onu_id : (e.ifindex || e.name.replace(/[^a-z0-9]/gi, "_"));
}

function _destroyAllTrafficCharts() {
  Object.values(_trafficCharts).forEach(c => { try { c.destroy(); } catch(_) {} });
  _trafficCharts = {};
}

/* ==================== TRAFFIC CHART HELPERS ==================== */
const TRAFFIC_MAX_POINTS = 300;

function _trafficCssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function _trafficColors() {
  return {
    rx:      _trafficCssVar("--traffic-rx-rgb", "251, 84, 104"),
    tx:      _trafficCssVar("--traffic-tx-rgb", "76, 125, 255"),
    text:    _trafficCssVar("--traffic-text", "#8896b3"),
    grid:    typeof chartGridColor === "function" ? chartGridColor() : _trafficCssVar("--traffic-grid", "rgba(136,150,179,.14)"),
    cross:   _trafficCssVar("--traffic-cross", "rgba(136,150,179,.55)"),
    tipBg:   _trafficCssVar("--traffic-tip-bg", "rgba(15,20,35,.94)"),
    tipText: _trafficCssVar("--traffic-tip-text", "#e8ecf6"),
    dotBg:   _trafficCssVar("--traffic-card-bg", "#141a2b"),
  };
}

function _trafficPickUnit(maxBps) {
  if (maxBps >= 1e9) return { div: 1e9, label: "Gbps" };
  if (maxBps >= 1e6) return { div: 1e6, label: "Mbps" };
  if (maxBps >= 1e3) return { div: 1e3, label: "kbps" };
  return { div: 1, label: "bps" };
}

function _trafficFmtTick(v) {
  if (v === 0) return "0";
  const d = v >= 100 ? 0 : v >= 10 ? 1 : 2;
  return String(+v.toFixed(d));
}

function _trafficFmtBpsTip(v) {
  return v > 0 ? _fmtBps(v) : "0 bps";
}

function _trafficFmtClock(ts, withSec) {
  const ms = typeof ts === "number" ? (ts < 1e12 ? ts * 1000 : ts) : Date.parse(ts);
  const d = new Date(ms);
  if (isNaN(d)) return String(ts);
  const p = n => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + (withSec ? ":" + p(d.getSeconds()) : "");
}

function _trafficDownsampleMax(series, maxPts = TRAFFIC_MAX_POINTS) {
  const n = series.length;
  if (n <= maxPts) return series;
  const size = Math.ceil(n / maxPts);
  const out = [];
  for (let i = 0; i < n; i += size) {
    const end = Math.min(i + size, n);
    let rx = 0, tx = 0;
    for (let j = i; j < end; j++) {
      const r = +series[j].rx_bps || 0, t = +series[j].tx_bps || 0;
      if (r > rx) rx = r;
      if (t > tx) tx = t;
    }
    out.push({ ts: series[i + ((end - i) >> 1)].ts, rx_bps: rx, tx_bps: tx });
  }
  return out;
}

function _trafficCalcStats(series) {
  let sRx = 0, sTx = 0, pRx = 0, pTx = 0, tRx = null, tTx = null;
  for (const p of series) {
    const rx = +p.rx_bps || 0, tx = +p.tx_bps || 0;
    sRx += rx; sTx += tx;
    if (rx > pRx) { pRx = rx; tRx = p.ts; }
    if (tx > pTx) { pTx = tx; tTx = p.ts; }
  }
  const n = series.length || 1;
  return { avgRx: sRx / n, avgTx: sTx / n, peakRx: pRx, peakTx: pTx, peakRxTs: tRx, peakTxTs: tTx };
}

function _trafficPeakIndex(arr) {
  let m = 0, idx = -1;
  for (let i = 0; i < arr.length; i++) if (arr[i] > m) { m = arr[i]; idx = i; }
  return idx;
}

function _trafficGradient(chart, k) {
  const a = chart.chartArea;
  if (!a) return undefined;
  const t = chart.tc;
  const sig = a.top + "|" + a.bottom + "|" + t.colors[k];
  if (!t.grad[k] || t.grad[k].sig !== sig) {
    const g = chart.ctx.createLinearGradient(0, a.top, 0, a.bottom);
    g.addColorStop(0, "rgba(" + t.colors[k] + ", 0.38)");
    g.addColorStop(1, "rgba(" + t.colors[k] + ", 0)");
    t.grad[k] = { sig, g };
  }
  return t.grad[k].g;
}

const _trafficPlugin = {
  id: "trafficPlugin",
  beforeInit(chart) {
    chart.tc = { colors: _trafficColors(), grad: {}, unit: _trafficPickUnit(0),
                 raw: { ts: [], rx: [], tx: [] }, peak: [-1, -1], empty: true };
  },
  beforeDatasetsDraw(chart) {
    const act = chart.tooltip && chart.tooltip.getActiveElements();
    if (!act || !act.length) return;
    const x = act[0].element.x;
    const { top, bottom } = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x, top); ctx.lineTo(x, bottom);
    ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
    ctx.strokeStyle = chart.tc.colors.cross;
    ctx.stroke();
    ctx.restore();
  },
  afterDatasetsDraw(chart) {
    const { peak, colors } = chart.tc;
    const ctx = chart.ctx;
    peak.forEach((idx, di) => {
      if (idx < 0) return;
      const meta = chart.getDatasetMeta(di);
      if (meta.hidden) return;
      const pt = meta.data[idx];
      if (!pt) return;
      ctx.save();
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, 4, 0, Math.PI * 2);
      ctx.fillStyle = colors.dotBg;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = "rgb(" + (di === 0 ? colors.rx : colors.tx) + ")";
      ctx.stroke();
      ctx.restore();
    });
  },
  afterDraw(chart) {
    if (!chart.tc.empty) return;
    const { left, right, top, bottom } = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.font = "500 12px system-ui, -apple-system, sans-serif";
    ctx.fillStyle = chart.tc.colors.text;
    ctx.fillText("Tidak ada traffic", (left + right) / 2, (top + bottom) / 2);
    ctx.restore();
  },
};

function _createTrafficChart(canvas) {
  const mkDataset = (label, k) => ({
    label,
    data: [],
    borderWidth: 2,
    fill: "origin",
    backgroundColor: ctx => _trafficGradient(ctx.chart, k),
    cubicInterpolationMode: "monotone",
    pointRadius: 0, pointHoverRadius: 4, pointHoverBorderWidth: 2,
    spanGaps: true,
  });
  const chart = new Chart(canvas, {
    type: "line",
    data: { labels: [], datasets: [mkDataset("Download", "rx"), mkDataset("Upload", "tx")] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false, axis: "x" },
      layout: { padding: { top: 6, right: 6 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          padding: 10, boxPadding: 4, cornerRadius: 8,
          usePointStyle: true, boxWidth: 8, boxHeight: 8,
          titleFont: { size: 11, weight: "600" },
          bodyFont: { size: 11 },
          footerFont: { size: 11, weight: "600" },
          callbacks: {
            title: items => _trafficFmtClock(items[0].chart.tc.raw.ts[items[0].dataIndex], true),
            label: ctx => {
              const raw = ctx.chart.tc.raw;
              const v = ctx.datasetIndex === 0 ? raw.rx[ctx.dataIndex] : raw.tx[ctx.dataIndex];
              return " " + ctx.dataset.label + ": " + _trafficFmtBpsTip(v);
            },
            labelColor: ctx => {
              const c = "rgb(" + ctx.chart.tc.colors[ctx.datasetIndex === 0 ? "rx" : "tx"] + ")";
              return { borderColor: c, backgroundColor: c, borderWidth: 0, borderRadius: 4 };
            },
            footer: items => {
              const raw = items[0].chart.tc.raw, i = items[0].dataIndex;
              return "Total: " + _trafficFmtBpsTip(raw.rx[i] + raw.tx[i]);
            },
          },
        },
      },
      scales: {
        x: { ticks: { maxTicksLimit: 6, maxRotation: 0, autoSkip: true, font: { size: 10 } },
             grid: { display: false }, border: { display: false } },
        y: { beginAtZero: true, grace: "12%",
             ticks: { maxTicksLimit: 4, padding: 6, font: { size: 10 },
               callback(value, index, ticks) {
                 const str = _trafficFmtTick(value);
                 return index === ticks.length - 1 ? str + " " + this.chart.tc.unit.label : str;
               } },
             grid: { drawTicks: false }, border: { display: false },
             afterFit(scale) { scale.width = 48; } },
      },
    },
    plugins: [_trafficPlugin],
  });
  _applyTrafficTheme(chart);
  return chart;
}

function _applyTrafficTheme(chart) {
  const c = (chart.tc.colors = _trafficColors());
  chart.tc.grad = {};
  const o = chart.options;
  o.scales.x.ticks.color = c.text;
  o.scales.y.ticks.color = c.text;
  o.scales.y.grid.color = c.grid;
  const tip = o.plugins.tooltip;
  tip.backgroundColor = c.tipBg;
  tip.titleColor = tip.bodyColor = tip.footerColor = c.tipText;
  chart.data.datasets.forEach((ds, i) => {
    const rgb = i === 0 ? c.rx : c.tx;
    ds.borderColor = "rgb(" + rgb + ")";
    ds.pointHoverBackgroundColor = c.dotBg;
    ds.pointHoverBorderColor = "rgb(" + rgb + ")";
  });
  chart.update("none");
}

function _refreshTrafficTheme() {
  Object.values(_trafficCharts).forEach(ch => _applyTrafficTheme(ch));
}

new MutationObserver(_refreshTrafficTheme)
  .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class"] });

function _updateTrafficChartData(chart, series) {
  const t = chart.tc;
  const full = series || [];
  const stats = _trafficCalcStats(full);
  const pts = _trafficDownsampleMax(full);
  const rx = pts.map(p => +p.rx_bps || 0);
  const tx = pts.map(p => +p.tx_bps || 0);
  const maxBps = Math.max(0, ...rx, ...tx);
  const unit = _trafficPickUnit(maxBps);
  t.unit = unit;
  t.raw = { ts: pts.map(p => p.ts), rx, tx };
  t.empty = maxBps === 0;
  t.peak = [_trafficPeakIndex(rx), _trafficPeakIndex(tx)];
  chart.data.labels = t.raw.ts.map(ts => _trafficFmtClock(ts));
  chart.data.datasets[0].data = rx.map(v => v / unit.div);
  chart.data.datasets[1].data = tx.map(v => v / unit.div);
  const y = chart.options.scales.y;
  y.suggestedMax = t.empty ? 1 : undefined;
  y.ticks.display = !t.empty;
  chart.update("none");
  return stats;
}

function _updateTrafficMeta(key, s) {
  const card = document.querySelector('.traffic-card[data-key="' + key + '"]');
  if (!card) return;
  const set = (role, text, title) => {
    const el = card.querySelector('[data-role="' + role + '"]');
    if (!el) return;
    if (el.textContent !== text) el.textContent = text;
    el.title = title || "";
  };
  set("peak-rx", _fmtBps(s.peakRx), s.peakRxTs ? "Puncak pukul " + _trafficFmtClock(s.peakRxTs) : "");
  set("peak-tx", _fmtBps(s.peakTx), s.peakTxTs ? "Puncak pukul " + _trafficFmtClock(s.peakTxTs) : "");
  set("avg-rx", _fmtBps(s.avgRx));
  set("avg-tx", _fmtBps(s.avgTx));
}

function _renderTrafficChart(key, series) {
  let chart = _trafficCharts[key];
  if (chart && !chart.canvas.isConnected) {
    try { chart.destroy(); } catch(_) {}
    delete _trafficCharts[key];
    chart = null;
  }
  if (!chart) {
    const canvas = document.getElementById("chart-traffic-" + key);
    if (!canvas) return null;
    chart = _createTrafficChart(canvas);
    _trafficCharts[key] = chart;
  }
  const stats = _updateTrafficChartData(chart, series);
  _updateTrafficMeta(key, stats);
  return chart;
}

function _trafficSortKey(e) {
  // Sort natural: parse angka dari name (mis. "xgei_1/3/2" → [1,3,2])
  // onu_id (kalau ada) jadi prioritas utama
  const onuId = e.onu_id != null ? e.onu_id : 999999;
  const name = String(e.name || "");
  // Ambil semua grup angka dari name
  const nums = (name.match(/\d+/g) || []).map(Number);
  // Pad jadi array panjang 4 supaya bisa dibandingkan konsisten
  while (nums.length < 4) nums.push(0);
  return [onuId, ...nums];
}

function _trafficCompare(a, b) {
  const ka = _trafficSortKey(a);
  const kb = _trafficSortKey(b);
  for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
    const va = ka[i] || 0;
    const vb = kb[i] || 0;
    if (va !== vb) return va - vb;
  }
  // Kalau semua angka sama, sort by name string
  return String(a.name || "").localeCompare(String(b.name || ""));
}

function _buildTrafficCardHTML(e, scope) {
  const key = _trafficEntityKey(e);
  const cid = "chart-traffic-" + key;
  const active = (e.latest_rx_bps || 0) > 0 || (e.latest_tx_bps || 0) > 0;
  const rx = e.latest_rx_bps ? _fmtBps(e.latest_rx_bps) : "-";
  const tx = e.latest_tx_bps ? _fmtBps(e.latest_tx_bps) : "-";
  const isOnu = scope === "onu";
  const title = isOnu && e.label ? e.label : e.name;
  const subtitle = isOnu ? '<div class="traffic-card-sub">' + escapeHtml(e.name) + (e.status ? " \u00b7 " + escapeHtml(e.status) : "") + '</div>' : "";
  return ''
    + '<div class="traffic-card" data-key="' + key + '">'
    +   '<div class="traffic-card-head">'
    +     '<div style="flex:1;min-width:0">'
    +       '<span class="traffic-card-title">' + escapeHtml(title) + '</span>'
    +       subtitle
    +     '</div>'
    +     '<span class="traffic-card-badge ' + (active ? "active" : "idle") + '">' + (active ? "active" : "idle") + '</span>'
    +   '</div>'
    +   '<div class="traffic-stats">'
    +     '<div class="traffic-stat rx"><span class="traffic-stat-label">Download</span>'
    +       '<span class="traffic-stat-value rx">' + rx + '</span></div>'
    +     '<div class="traffic-stat tx"><span class="traffic-stat-label">Upload</span>'
    +       '<span class="traffic-stat-value tx">' + tx + '</span></div>'
    +   '</div>'
    +   '<div class="traffic-chart-wrap"><canvas id="' + cid + '"></canvas></div>'
    +   '<div class="traffic-meta">'
    +     '<div class="traffic-meta-item"><span>Peak \u2193</span><span data-role="peak-rx">-</span></div>'
    +     '<div class="traffic-meta-item"><span>Peak \u2191</span><span data-role="peak-tx">-</span></div>'
    +     '<div class="traffic-meta-item"><span>Avg \u2193</span><span data-role="avg-rx">-</span></div>'
    +     '<div class="traffic-meta-item"><span>Avg \u2191</span><span data-role="avg-tx">-</span></div>'
    +   '</div>'
    + '</div>';
}

function _rebuildTrafficUI(el, entities, scope) {
  _destroyAllTrafficCharts();
  // Sort entities dulu sebelum render
  const sorted = entities.slice().sort(_trafficCompare);
  el.innerHTML = '<div class="traffic-grid">' + sorted.map(e => _buildTrafficCardHTML(e, scope)).join("") + '</div>';
  requestAnimationFrame(() => {
    sorted.forEach(e => {
      const key = _trafficEntityKey(e);
      const canvas = document.getElementById("chart-traffic-" + key);
      if (!canvas || canvas.offsetWidth === 0) return;
      _renderTrafficChart(key, e.series);
    });
  });
}

function _updateTrafficUI(el, entities) {
  entities.forEach(e => {
    const key = _trafficEntityKey(e);
    const card = el.querySelector('.traffic-card[data-key="' + key + '"]');
    if (!card) return;
    const rx = e.latest_rx_bps ? _fmtBps(e.latest_rx_bps) : "-";
    const tx = e.latest_tx_bps ? _fmtBps(e.latest_tx_bps) : "-";
    const rxEl = card.querySelector(".traffic-stat-value.rx");
    const txEl = card.querySelector(".traffic-stat-value.tx");
    if (rxEl) rxEl.textContent = rx;
    if (txEl) txEl.textContent = tx;
    const active = (e.latest_rx_bps || 0) > 0 || (e.latest_tx_bps || 0) > 0;
    const badge = card.querySelector(".traffic-card-badge");
    if (badge) {
      badge.textContent = active ? "active" : "idle";
      badge.className = "traffic-card-badge " + (active ? "active" : "idle");
    }
    _renderTrafficChart(key, e.series);
  });
}


function _fmtBps(bps) {
  if (bps == null) return "-";
  if (bps >= 1e9) return (bps/1e9).toFixed(2) + " Gbps";
  if (bps >= 1e6) return (bps/1e6).toFixed(2) + " Mbps";
  if (bps >= 1e3) return (bps/1e3).toFixed(1) + " Kbps";
  return bps.toFixed(0) + " bps";
}

async function loadONUs(opts = {}) {
  const el = document.getElementById("onu-table");
  if (!el) return;
  try {
    const search = document.getElementById("onu-search").value.toLowerCase().trim();
    const status = document.getElementById("onu-status-filter").value;
    let url = `/olts/${CURRENT_OLT_ID}/onus`;
    if (status) url += `?status=${status}`;
    const onus = await api(url, opts);

    const filtered = onus.filter(o => {
      if (!search) return true;
      return (o.serial_number || "").toLowerCase().includes(search)
        || (o.name || "").toLowerCase().includes(search)
        || (o.pon_port || "").includes(search)
        || (o.interface_name || "").toLowerCase().includes(search);
    });

    if (!filtered.length) {
      el.innerHTML = `
        <div class="widget" style="text-align:center;padding:40px">
          <i class="fas fa-wifi" style="font-size:36px;color:var(--text-dim);margin-bottom:12px;display:block"></i>
          <p style="color:var(--text-dim)">Tidak ada ONU ditemukan</p>
        </div>`;
      return;
    }

    el.innerHTML = `
      <div class="table-wrap"><table class="table-premium">
        <thead><tr>
          <th>OLT</th><th>Interface</th><th>Serial Number</th><th>Nama</th><th>Merek</th><th>ONU</th>
          <th>Internet</th><th>RX (dBm)</th><th>Distance</th><th>VLAN</th><th>PPPoE</th><th>Aksi</th>
        </tr></thead>
        <tbody>
          ${filtered.map(o => `
            <tr>
              <td><b>${escapeHtml(o.olt_hostname || _currentOltName || "-")}</b></td>
              <td><b>${escapeHtml(o.interface_name || `${o.pon_port}:${o.onu_id}`)}</b></td>
              <td><code>${escapeHtml(o.serial_number || "-")}</code></td>
              <td>${escapeHtml(o.name || "-")}</td>
              <td>${renderVendorBadge(o)}</td>
              <td><span class="status-badge ${escapeHtml(o.status)}">${escapeHtml(o.status)}</span></td>
              <td>${renderInternetStatus(o)}</td>
              <td>${o.optical_rx != null ? o.optical_rx + " dBm" : "-"}</td>
              <td>${o.distance ? o.distance + " m" : "-"}</td>
              <td>${o.user_vlan || "-"} → ${o.vlan || "-"}</td>
              <td>${escapeHtml(o.pppoe_user || "-")}</td>
              <td>
                <button class="btn-icon" onclick="showONUDetail(${o.onu_id}, ${o.olt_id || "null"})" title="Detail"><i class="fas fa-eye"></i></button>
                ${(o.provisioning_mode === "bridge") ? (
                  o.bridge_configured
                    ? `<button class="btn-icon" onclick="markDisconnected(${o.onu_id}, ${o.olt_id || "null"})" title="Tandai Internet Mati" style="color:var(--yellow)"><i class="fas fa-times-circle"></i></button>`
                    : `<button class="btn-icon" onclick="markConnected(${o.onu_id}, ${o.olt_id || "null"})" title="Tandai Internet Aktif" style="color:var(--green)"><i class="fas fa-check-circle"></i></button>`
                ) : ""}
                <button class="btn-icon" onclick="rebootONU(${o.onu_id}, ${o.olt_id || "null"})" title="Reboot"><i class="fas fa-power-off"></i></button>
                <button class="btn-icon" onclick="deleteONU(${o.onu_id}, ${o.olt_id || "null"})" title="Hapus dari OLT" style="color:var(--red)"><i class="fas fa-trash"></i></button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table></div>
    `;
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}

document.getElementById("onu-search")?.addEventListener("input", debounce(loadONUs, 300));
document.getElementById("onu-status-filter")?.addEventListener("change", loadONUs);
document.getElementById("traffic-scope")?.addEventListener("change", loadTrafficPage);
document.getElementById("traffic-period")?.addEventListener("change", loadTrafficPage);
document.getElementById("btn-traffic-refresh")?.addEventListener("click", loadTrafficPage);

let CURRENT_ONU_ID = null;
let _trafficCharts = {};       // Chart.js instances by canvas id (reuse)
let _trafficStateKey = "";
let _trafficAbort = null;

// Debug helper — panggil dari Console: window.waTrafficDebug()
window.waTrafficDebug = function() {
  return {
    charts: Object.keys(_trafficCharts).length,
    chartsIds: Object.keys(_trafficCharts),
    stateKey: _trafficStateKey,
    hasAbort: !!_trafficAbort,
    currentPage: typeof currentPage !== "undefined" ? currentPage : null,
  };
};
// Expose function untuk manual refresh dari Console
window.waTrafficRefresh = function() { return loadTrafficPage({ force: true }); };
let CURRENT_ONU_OLT_ID = null;   // OLT tempat ONU yang sedang dibuka

async function showONUDetail(onuId, oltId = null) {
  CURRENT_ONU_ID = onuId;
  CURRENT_ONU_OLT_ID = oltId || CURRENT_OLT_ID;
  // Pindah ke halaman detail (bukan modal)
  loadPage("onu-detail");
}

async function loadONUDetailPage(opts = {}) {
  const el = document.getElementById("onu-detail-content");
  if (!el) return;
  if (!CURRENT_ONU_ID) {
    el.innerHTML = `<div class="widget" style="padding:40px;text-align:center;color:var(--text-dim)">
      <i class="fas fa-wifi" style="font-size:36px;opacity:0.5;margin-bottom:12px;display:block"></i>
      <p>Tidak ada ONU dipilih</p>
      <button class="btn-primary" onclick="loadPage('onus')" style="margin-top:14px">
        <i class="fas fa-arrow-left"></i> Kembali ke daftar ONU
      </button>
    </div>`;
    return;
  }

  try {
    const o = await api(`/olts/${CURRENT_ONU_OLT_ID || CURRENT_OLT_ID}/onus/${CURRENT_ONU_ID}`, opts);
    const rx = o.optical_rx;
    let rxCls = "good";
    if (rx != null) {
      if (rx < -28) rxCls = "bad";
      else if (rx < -25) rxCls = "warn";
    }

    const html = `
      <button class="onu-detail-back" onclick="loadPage('onus')">
        <i class="fas fa-arrow-left"></i> Kembali ke ONU
      </button>

      <div class="onu-detail-header">
        <div class="onu-detail-title-row">
          <h1>${escapeHtml(o.name || "Tanpa Nama")}</h1>
          <span class="status-badge ${escapeHtml(o.status)}">${escapeHtml(o.status)}</span>
          ${renderInternetStatus(o)}
        </div>
        <div style="font-size:12px;color:var(--text-dim);font-family:'Monaco',monospace">
          <code>${escapeHtml(o.serial_number || "-")}</code>
          · ${escapeHtml(o.interface_name || "")}
          · ${escapeHtml(o.pon_port || "")}:${o.onu_id}
        </div>
        <div class="onu-detail-actions">
          <button onclick="rebootONU(${o.onu_id}, ${CURRENT_ONU_OLT_ID || CURRENT_OLT_ID})"><i class="fas fa-power-off"></i> Reboot</button>
          ${o.provisioning_mode === "bridge" ? (
            o.bridge_configured
              ? `<button onclick="markDisconnected(${o.onu_id}, ${CURRENT_ONU_OLT_ID || CURRENT_OLT_ID})"><i class="fas fa-times-circle"></i> Tandai Disconnected</button>`
              : `<button onclick="markConnected(${o.onu_id}, ${CURRENT_ONU_OLT_ID || CURRENT_OLT_ID})"><i class="fas fa-check-circle"></i> Tandai Connected</button>`
          ) : ""}
          <button onclick="loadONUDetailPage()"><i class="fas fa-sync"></i> Refresh</button>
          <button class="danger" onclick="deleteONU(${o.onu_id}, ${CURRENT_ONU_OLT_ID || CURRENT_OLT_ID}).then(() => loadPage('onus'))"><i class="fas fa-trash"></i> Hapus</button>
        </div>
      </div>

      <div class="health-strip">
        <div class="health-item">
          <div class="lbl">Optical RX</div>
          <div class="val ${rxCls}">${rx != null ? rx.toFixed(2) + " dBm" : "-"}</div>
        </div>
        <div class="health-item">
          <div class="lbl">Optical TX</div>
          <div class="val">${o.optical_tx != null ? o.optical_tx.toFixed(2) + " dBm" : "-"}</div>
        </div>
        <div class="health-item">
          <div class="lbl">Distance</div>
          <div class="val">${o.distance != null ? o.distance + " m" : "-"}</div>
        </div>
        <div class="health-item">
          <div class="lbl">Internet Uptime</div>
          <div class="val">${o.pppoe_online_duration ? formatUptime(o.pppoe_online_duration) : "-"}</div>
        </div>
      </div>

      <div class="detail-grid">
        <div class="detail-section">
          <div class="detail-section-header">
            <span><i class="fas fa-info-circle"></i> Identitas ONU</span>
          </div>
          <div class="detail-section-body">
            <div class="detail-kv">
              <div class="k">Nama</div><div class="v">${escapeHtml(o.name || "-")}</div>
              <div class="k">Serial Number</div><div class="v"><code>${escapeHtml(o.serial_number || "-")}</code></div>
              <div class="k">Merek</div><div class="v">${escapeHtml(o.vendor || "ZTE")}</div>
              <div class="k">Tipe ONU</div><div class="v">${escapeHtml(o.type || "-")}</div>
              <div class="k">Interface</div><div class="v"><code>${escapeHtml(o.interface_name || "-")}</code></div>
              <div class="k">Lokasi (ODP)</div><div class="v">${escapeHtml(o.description || "-")}</div>
            </div>
          </div>
        </div>

        <div class="detail-section">
          <div class="detail-section-header">
            <span><i class="fas fa-network-wired"></i> Service & VLAN</span>
          </div>
          <div class="detail-section-body">
            <div class="detail-kv">
              <div class="k">TCONT</div><div class="v">${escapeHtml(o.tcont || "-")}</div>
              <div class="k">GEMPORT</div><div class="v">${o.gemport || "-"}</div>
              <div class="k">Service Port</div><div class="v">${o.service_port || "-"}</div>
              <div class="k">User VLAN</div><div class="v">${o.user_vlan || "-"}</div>
              <div class="k">VLAN</div><div class="v">${o.vlan || "-"}</div>
              <div class="k">Mode</div><div class="v">${escapeHtml(o.provisioning_mode || "routed")}</div>
            </div>
          </div>
        </div>

        <div class="detail-section">
          <div class="detail-section-header">
            <span><i class="fas fa-key"></i> PPPoE & Internet</span>
          </div>
          <div class="detail-section-body">
            <div class="detail-kv">
              <div class="k">PPPoE User</div><div class="v">${escapeHtml(o.pppoe_user || "-")}</div>
              <div class="k">NAT</div><div class="v">${o.pppoe_nat ? "Enabled" : "Disabled"}</div>
              <div class="k">Status</div><div class="v">${escapeHtml(o.pppoe_status || "unknown")}</div>
              <div class="k">Online Duration</div><div class="v">${o.pppoe_online_duration ? formatUptime(o.pppoe_online_duration) : "-"}</div>
              <div class="k">Terakhir Dicek</div><div class="v">${o.internet_checked_at ? new Date(o.internet_checked_at).toLocaleString("id-ID") : "-"}</div>
            </div>
          </div>
        </div>

        <div class="detail-section">
          <div class="detail-section-header">
            <span><i class="fas fa-heartbeat"></i> Kesehatan</span>
          </div>
          <div class="detail-section-body">
            <div class="detail-kv">
              <div class="k">Status</div><div class="v"><span class="status-badge ${escapeHtml(o.status)}">${escapeHtml(o.status)}</span></div>
              <div class="k">Optical RX</div><div class="v ${rxCls}" style="color:${rxCls === "good" ? "var(--green)" : rxCls === "warn" ? "var(--yellow)" : rxCls === "bad" ? "var(--red)" : "inherit"}">${rx != null ? rx.toFixed(2) + " dBm" : "-"}</div>
              <div class="k">Optical TX</div><div class="v">${o.optical_tx != null ? o.optical_tx.toFixed(2) + " dBm" : "-"}</div>
              <div class="k">Distance</div><div class="v">${o.distance != null ? o.distance + " m" : "-"}</div>
            </div>
          </div>
        </div>
      </div>

      <div class="detail-section" style="margin-top:16px">
        <div class="detail-section-header">
          <span><i class="fas fa-history"></i> Timeline Event</span>
          <button class="btn-link" onclick="loadONUEvents()" style="font-size:11px">
            <i class="fas fa-sync"></i> Refresh
          </button>
        </div>
        <div class="detail-section-body" id="onu-events-body">
          <div style="text-align:center;color:var(--text-dim);padding:14px;font-size:12px">
            <i class="fas fa-spinner fa-spin"></i> Memuat event...
          </div>
        </div>
      </div>
    `;

    el.innerHTML = html;

    // Attach Edit VLAN (fitur baru)
    if (typeof onuVlanAttach === "function") onuVlanAttach(o);

    // Load events async
    loadONUEvents();
  } catch (e) {
    if (e.name !== "AbortError") {
      el.innerHTML = `<div class="widget" style="padding:30px;text-align:center;color:var(--red)">
        <i class="fas fa-exclamation-triangle" style="font-size:32px;margin-bottom:12px;display:block"></i>
        <p>Gagal memuat detail ONU</p>
        <p style="font-size:12px;color:var(--text-dim);margin-top:6px">${escapeHtml(e.message)}</p>
        <button class="btn-primary" onclick="loadPage('onus')" style="margin-top:14px">
          <i class="fas fa-arrow-left"></i> Kembali ke daftar
        </button>
      </div>`;
    }
  }
}

async function markConnected(onuId, oltId = null) {
  if (!confirm("Tandai ONU ini sebagai CONNECTED? (user sudah set PPPoE di GUI modem)")) return;
  try {
    const targetOlt = oltId || CURRENT_OLT_ID;
    await api(`/onu/${targetOlt}/${onuId}/mark-connected`, { method: "POST" });
    toast("✅ Status: CONNECTED", "success");
    loadONUs();
  } catch (e) { toast(e.message, "error"); }
}

async function markDisconnected(onuId, oltId = null) {
  if (!confirm("Tandai ONU ini sebagai BELUM SETUP?")) return;
  try {
    const targetOlt = oltId || CURRENT_OLT_ID;
    await api(`/onu/${targetOlt}/${onuId}/mark-disconnected`, { method: "POST" });
    toast("Status: BELUM SETUP", "warning");
    loadONUs();
  } catch (e) { toast(e.message, "error"); }
}

async function rebootONU(onuId, oltId = null) {
  let label = `ONU ID ${onuId}`;
  try {
    let onu = _searchCache && _searchCache.find(o => o.onu_id === onuId);
    if (!onu) {
      const list = await api(`/olts/${CURRENT_OLT_ID}/onus`);
      _searchCache = list;
      _searchCacheTime = Date.now();
      onu = list.find(o => o.onu_id === onuId);
    }
    if (onu) label = `${onu.name || "-"} (${onu.serial_number || "-"}) · ${onu.pon_port}:${onu.onu_id}`;
  } catch (_) {}

  confirmDangerous({
    title: "Reboot ONU",
    target: label,
    command: `pon-onu-mng gpon-onu_X:Y\nreboot\nexit`,
    warning: "ONU akan restart. Internet pelanggan terputus 1-3 menit sampai modem boot ulang.",
    confirmText: "Reboot",
    onConfirm: async () => {
      try {
        const targetOlt = oltId || CURRENT_OLT_ID;
        await api(`/onu/${targetOlt}/${onuId}/reboot`, { method: "POST" });
        toast("Perintah reboot terkirim", "success");
        loadONUs();
      } catch (e) { toast(e.message, "error"); }
    },
  });
}

async function deleteONU(onuId, oltId = null) {
  let label = `ONU ID ${onuId}`;
  try {
    let onu = _searchCache && _searchCache.find(o => o.onu_id === onuId);
    if (!onu) {
      const list = await api(`/olts/${CURRENT_OLT_ID}/onus`);
      _searchCache = list;
      _searchCacheTime = Date.now();
      onu = list.find(o => o.onu_id === onuId);
    }
    if (onu) label = `${onu.name || "-"} (${onu.serial_number || "-"}) · ${onu.pon_port}:${onu.onu_id}`;
  } catch (_) {}

  confirmDangerous({
    title: "Hapus ONU dari OLT",
    target: label,
    command: `configure terminal\ninterface gpon-onu_X:Y\nno service-port N\nexit\ninterface gpon-olt_X\nno onu Y\nexit\nend\nwrite`,
    warning: "Config ONU akan dihapus permanen dari OLT dan database aplikasi. Tidak bisa di-undo.",
    confirmText: "Hapus ONU",
    onConfirm: async () => {
      const targetOlt = oltId || CURRENT_OLT_ID;
      await submitJob(`/onu/${targetOlt}/${onuId}`, {
        method: "DELETE",
        label: "Hapus ONU",
        subject: label,
        refresh: () => { loadONUs(); uncfgCache = null; },
      });
    },
  });
}

// =================== PROVISION WIZARD ===================
let provisionData = {};
let provisionStep = 0;

// ===== Fase 4b-2: Service Package di wizard =====
let _packagesCache = [];

function wizardCanCustom() {
  const u = (typeof CURRENT_USER !== "undefined" && CURRENT_USER) || window.currentUser || null;
  return u && typeof u.privilege === "number" ? u.privilege >= 15 : true;
}

function _wizardPkgSelection() {
  const list = _packagesCache || [];
  if (!list.length) return { pkg: null };
  if (provisionData.package_custom && wizardCanCustom()) return { pkg: null };
  const hit = provisionData.package_id ? list.find(p => p.id === provisionData.package_id) : null;
  return { pkg: hit || list[0] };
}

async function loadWizardPackages() {
  const oltId = CURRENT_OLT_ID;
  _packagesCache = [];
  try {
    if (typeof Deployment === "undefined" || !oltId) return;
    const d = await Deployment.fetchPackages(oltId);
    if (oltId !== CURRENT_OLT_ID) return;
    _packagesCache = (d && d.items) || [];
  } catch (e) {
    console.warn("Service package fetch failed:", e);
    _packagesCache = [];
  }
}

function onPackageChange() {
  collectWizardData();
  renderWizard();
}
let _skipWizardReset = false;   // flag: jangan reset kalau dipanggil dari provisionDetected

document.getElementById("btn-provision-onu")?.addEventListener("click", async () => {
  if (!_skipWizardReset) {
    provisionData = {};
    provisionStep = 0;
  }
  _skipWizardReset = false;
  openModal("Provision ONU Baru", `<div id="wizard-content"></div>`);
  renderWizard();
  await loadWizardInventory();
  renderWizard();
});

function renderWizard() {
  const c = document.getElementById("wizard-content");
  if (!c) return;
  const steps = ["Pilih PON", "Data ONU", "Service", "Preview"];
  let html = `<div class="wizard-steps">` +
    steps.map((s, i) => `<div class="wizard-step ${i === provisionStep ? "active" : i < provisionStep ? "done" : ""}">${i + 1}. ${s}</div>`).join("") +
    `</div>`;

  const inv = _inventoryCache || {};

  if (provisionStep === 0) {
    html += `
      <div class="form-group"><label>PON Port</label>
        <select id="w-pon">${Array.from({ length: 16 }, (_, i) => `<option value="1/1/${i + 1}" ${provisionData.pon_port === `1/1/${i+1}` ? "selected" : ""}>1/1/${i + 1}</option>`).join("")}</select>
      </div>
      <div class="form-group"><label>ONU ID</label><input type="number" id="w-onu-id" value="${provisionData.onu_id || 1}" min="1" max="128"></div>
      <div style="background:var(--bg);padding:10px 14px;border-radius:8px;font-size:12px;color:var(--text-dim)">
        <i class="fas fa-info-circle"></i> Pilih SN di step berikut. Sistem akan cek bentrok otomatis.
      </div>
    `;
  } else if (provisionStep === 1) {
    const detectedVendor = detectVendorFromSN(provisionData.serial_number);
    const vendorDisplay = detectedVendor === "Unknown" ? "Tidak dikenali" : `Terdeteksi: ${detectedVendor}`;
    const vendorColor = detectedVendor === "ZTE" ? "var(--green)"
                       : detectedVendor === "Huawei" ? "var(--yellow)"
                       : detectedVendor === "FiberHome" ? "var(--primary)"
                       : "var(--text-dim)";
    const onuTypes = (inv.onu_types && inv.onu_types.length) ? inv.onu_types : ["F609","F601","F660","F670L"];
    const typeOptions = onuTypes.map(t => `<option ${provisionData.onu_type === t ? "selected" : ""}>${t}</option>`).join("");

    // Auto-default service mode dari SN prefix
    if (!provisionData.service_mode) {
      if (detectedVendor === "ZTE") {
        provisionData.service_mode = "pppoe";
      } else if (detectedVendor !== "Unknown") {
        provisionData.service_mode = "bridge";
      } else {
        provisionData.service_mode = "pppoe";
      }
    }
    const sm = provisionData.service_mode;
    const smOptions = [
      { v: "pppoe", l: "PPPoE (dial via ONU)" },
      { v: "bridge", l: "Bridge (dial di router pelanggan)" },
      { v: "static", l: "Static IP (belum didukung)" },
      { v: "dhcp", l: "DHCP Client (belum didukung)" },
    ].map(o => `<option value="${o.v}" ${sm === o.v ? "selected" : ""} ${(o.v === "static" || o.v === "dhcp") ? "disabled" : ""}>${o.l}</option>`).join("");

    html += `
      <div class="form-group">
        <label>Serial Number</label>
        <div style="display:flex;gap:8px">
          <input id="w-sn" placeholder="YYKC37D4BADA" value="${escapeHtml(provisionData.serial_number || "")}" style="flex:1">
          <button type="button" class="btn-secondary" onclick="openSNPicker()" title="Pilih SN dari OLT">
            <i class="fas fa-list"></i> Pilih dari OLT
          </button>
        </div>
      </div>
      <div style="background:var(--bg);padding:10px 14px;border-radius:8px;border-left:3px solid ${vendorColor};font-size:12px;margin-bottom:14px">
        <i class="fas fa-info-circle" style="color:${vendorColor}"></i>
        <b>Vendor terdeteksi:</b> ${escapeHtml(vendorDisplay)}
      </div>
      <div class="form-group"><label>Service Mode</label>
        <select id="w-service-mode" onchange="onServiceModeChange()">${smOptions}</select>
        <div style="font-size:11px;color:var(--text-dim);margin-top:4px">
          Default dari SN prefix. Bisa diubah manual.
        </div>
      </div>
      <div class="form-group"><label>Nama ONU (ID Pelanggan)</label><input id="w-name" placeholder="CLIENT-001" value="${escapeHtml(provisionData.name || "")}"></div>
      <div class="form-group"><label>Description / Site Location (ODP)</label><input id="w-desc" placeholder="Banduajo - ODP-4:1" value="${escapeHtml(provisionData.description || "")}"></div>
      <div class="form-group"><label>Tipe ONU</label>
        <select id="w-type">${typeOptions}</select>
      </div>
    `;
  } else if (provisionStep === 2) {
    const detectedVendor = detectVendorFromSN(provisionData.serial_number);
    const serviceMode = provisionData.service_mode || "pppoe";
    const routedOk = serviceMode === "pppoe";
    const pppoeDisabled = routedOk ? "" : "disabled";
    const pppoeStyle = routedOk ? "" : "opacity:0.5;cursor:not-allowed";

    let pppoeInfo = "";
    if (serviceMode === "bridge") {
      pppoeInfo = `<div style="background:rgba(79,140,255,0.08);padding:10px 14px;border-radius:8px;border-left:3px solid var(--primary);font-size:12px;margin-bottom:14px">
        <i class="fas fa-info-circle" style="color:var(--primary)"></i>
        <b>Bridge Mode:</b> ONU cuma jadi jembatan L2. PPPoE di-set manual di router pelanggan.
      </div>`;
    } else if (serviceMode === "static") {
      pppoeInfo = `<div style="background:rgba(239,68,68,0.08);padding:10px 14px;border-radius:8px;border-left:3px solid var(--red);font-size:12px;margin-bottom:14px">
        <i class="fas fa-exclamation-triangle" style="color:var(--red)"></i>
        <b>Static IP belum didukung.</b> Aplikasi akan fallback ke Bridge Mode. Fitur ini akan ditambah setelah command diverifikasi.
      </div>`;
    } else if (serviceMode === "dhcp") {
      pppoeInfo = `<div style="background:rgba(239,68,68,0.08);padding:10px 14px;border-radius:8px;border-left:3px solid var(--red);font-size:12px;margin-bottom:14px">
        <i class="fas fa-exclamation-triangle" style="color:var(--red)"></i>
        <b>DHCP Client belum didukung.</b> Aplikasi akan fallback ke Bridge Mode. Fitur ini akan ditambah setelah command diverifikasi.
      </div>`;
    } else if (serviceMode === "pppoe" && detectedVendor && detectedVendor !== "ZTE" && detectedVendor !== "Unknown") {
      pppoeInfo = `<div style="background:rgba(245,158,11,0.1);padding:10px 14px;border-radius:8px;border-left:3px solid var(--yellow);font-size:12px;margin-bottom:14px">
        <i class="fas fa-exclamation-triangle" style="color:var(--yellow)"></i>
        <b>Peringatan:</b> Vendor ${escapeHtml(detectedVendor)} tidak support PPPoE via OLT (OMCI proprietary ZTE). Coba ganti ke Bridge Mode.
      </div>`;
    }

    // Fase 4: pakai GPON profiles dari OLT (bukan inventory lama)
    const tcontNames = _gponProfilesCache.tcont.length
      ? _gponProfilesCache.tcont.map(p => p.name)
      : (inv.tcont_profiles || ["1G", "100M", "50M", "20M"]);
    const trafficNames = _gponProfilesCache.traffic.length
      ? _gponProfilesCache.traffic.map(p => p.name)
      : ["1G", "100M", "50M", "20M"];
    const wpPkg = _wizardPkgSelection().pkg;
    const wpLocked = !!wpPkg;
    const wpLockSel = wpLocked ? "disabled" : "";
    const wpLockInp = wpLocked ? "readonly" : "";
    const wpVal = {
      tcont: wpPkg ? wpPkg.tcont_profile : provisionData.tcont_profile,
      traffic: wpPkg ? wpPkg.traffic_profile : provisionData.traffic_limit,
      gem: wpPkg ? wpPkg.gemport_id : (provisionData.gemport_id || 1),
      sp: wpPkg ? wpPkg.service_port : (provisionData.service_port || 1),
      vport: wpPkg ? wpPkg.vport : (provisionData.vport || 1),
      uvlan: wpPkg ? wpPkg.user_vlan : provisionData.user_vlan,
      vlan: wpPkg ? wpPkg.vlan : provisionData.vlan,
    };

    const wpNameOpts = (names, cur) => {
      let o = names.map(t => `<option ${cur === t ? "selected" : ""}>${escapeHtml(t)}</option>`).join("");
      if (wpPkg && cur && !names.includes(cur)) {
        o += `<option value="${escapeHtml(cur)}" selected>${escapeHtml(cur)} (tidak ada di cache)</option>`;
      }
      return o;
    };
    const tcontOpts = wpNameOpts(tcontNames, wpVal.tcont);
    const trafficOpts = wpNameOpts(trafficNames, wpVal.traffic);

    const vlans = inv.vlans || [];
    const wpVlanOpts = (cur) => {
      if (!vlans.length) {
        return wpPkg && cur != null
          ? `<option value="${cur}" selected>${cur}</option>`
          : `<option value="15">15 (default)</option>`;
      }
      let o = vlans.map(v => `<option value="${v.id}" ${cur === v.id ? "selected" : ""}>${v.id} - ${v.name}</option>`).join("");
      if (wpPkg && cur != null && !vlans.some(v => v.id === cur)) {
        o += `<option value="${cur}" selected>${cur} (tidak ada di daftar VLAN)</option>`;
      }
      return o;
    };
    const uvlanOpts = wpVlanOpts(wpVal.uvlan);
    const vlanOpts = wpVlanOpts(wpVal.vlan);

    const wpList = _packagesCache || [];
    let pkgBlock;
    if (wpList.length) {
      const wpOpts = wpList.map(p => `<option value="${p.id}" ${wpPkg && wpPkg.id === p.id ? "selected" : ""}>${escapeHtml(p.name)}</option>`).join("")
        + (wizardCanCustom() ? `<option value="custom" ${wpPkg ? "" : "selected"}>Custom (Admin)</option>` : "");
      const wpNotes = wpPkg
        ? [
            wpPkg.description ? `<small class="pkg-desc">${escapeHtml(wpPkg.description)}</small>` : "",
            (wpPkg.issues && wpPkg.issues.length) ? `<small class="pkg-warn">⚠ ${escapeHtml(wpPkg.issues.join("; "))}</small>` : "",
            ((wpPkg.pppoe_user_pattern || "") + (wpPkg.pppoe_password_pattern || "")).includes("{")
              ? `<small class="pkg-note">PPPoE belum diisi otomatis dari paket (menunggu Fase 4b-3). Isi manual atau kosongkan.</small>` : "",
          ].join("")
        : `<small class="pkg-desc">Mode Custom: semua field diisi manual.</small>`;
      pkgBlock = `<div class="form-group pkg-select"><label>Paket Layanan</label><select id="w-package" onchange="onPackageChange()">${wpOpts}</select>${wpNotes}</div>`;
    } else {
      pkgBlock = `<div class="form-group pkg-select"><label>Paket Layanan</label><select id="w-package" disabled><option value="custom">Custom (belum ada paket)</option></select><small class="pkg-desc">Belum ada Service Package untuk OLT ini. Buat di Deployment → Service Package.</small></div>`;
    }
    const wpPppoeHint = wpPkg ? `Pola paket: ${wpPkg.pppoe_user_pattern}` : "";
    // Fase 4b-2.1: auto-isi PPPoE kalau paket isi literal (tanpa pattern {..})
    const wpPppoeUser = wpPkg ? (wpPkg.pppoe_user_pattern || "") : "";
    const wpPppoePass = wpPkg ? (wpPkg.pppoe_password_pattern || "") : "";
    const wpPppoeUserVal = (wpPkg && wpPppoeUser && !wpPppoeUser.includes("{"))
      ? wpPppoeUser
      : (provisionData.pppoe_user || "");
    const wpPppoePassVal = (wpPkg && wpPppoePass && !wpPppoePass.includes("{"))
      ? wpPppoePass
      : "";

    html += `
      ${pkgBlock}
      ${pppoeInfo}
      <div class="form-group"><label>T-CONT Profile</label>
        <select id="w-tcont" ${wpLockSel}>${tcontOpts}</select>
      </div>
      <div class="form-group"><label>T-CONT Name</label><input id="w-tcont-name" value="${escapeHtml(provisionData.tcont_name || "PON1")}"></div>
      <div class="form-group"><label>GEMPORT ID</label><input type="number" id="w-gem" value="${wpVal.gem}" min="1" max="32" ${wpLockInp}></div>
      <div class="form-group"><label>Traffic Limit</label>
        <select id="w-traffic" ${wpLockSel}>${trafficOpts}</select>
      </div>
      <div class="form-group"><label>Service Port</label><input type="number" id="w-sp" value="${wpVal.sp}" min="1" ${wpLockInp}></div>
      <div class="form-group"><label>VPort</label><input type="number" id="w-vport" value="${wpVal.vport}" ${wpLockInp}></div>
      <div class="form-group"><label>User VLAN</label>
        <select id="w-uvlan" ${wpLockSel}>${uvlanOpts}</select>
      </div>
      <div class="form-group"><label>VLAN</label>
        <select id="w-vlan" ${wpLockSel}>${vlanOpts}</select>
      </div>
      <div class="form-group" style="${pppoeStyle}"><label>PPPoE User ${routedOk ? "(opsional)" : "(tidak tersedia)"}</label><input id="w-pppoe" ${pppoeDisabled} value="${escapeHtml(wpPppoeUserVal)}" placeholder="${escapeHtml(wpPppoeHint)}"></div>
      <div class="form-group" style="${pppoeStyle}"><label>PPPoE Password</label><input type="password" id="w-pppoe-pass" ${pppoeDisabled} value="${escapeHtml(wpPppoePassVal)}"></div>
    `;
  } else if (provisionStep === 3) {
    html += `<h4 style="margin-bottom:10px;font-size:13px;color:var(--text-dim);text-transform:uppercase;letter-spacing:1px">Preview CLI ZXAN</h4>
      <div class="cli-preview">${escapeHtml(generatePreview())}</div>
      <p style="margin-top:12px;color:var(--text-dim);font-size:12px">
        Sistem jalankan preflight check dulu. Kalau ada bentrok, provisioning diblokir.
      </p>`;
  }

  c.innerHTML = html;

  const footer = document.getElementById("modal-footer");
  footer.innerHTML = "";

  if (provisionStep > 0) {
    const back = document.createElement("button");
    back.className = "btn-secondary";
    back.textContent = "Kembali";
    back.onclick = () => { if (collectWizardData()) { provisionStep--; renderWizard(); } };
    footer.appendChild(back);
  }
  const cancel = document.createElement("button");
  cancel.className = "btn-secondary";
  cancel.textContent = "Tutup";
  cancel.onclick = closeModal;
  footer.appendChild(cancel);

  const next = document.createElement("button");
  next.className = "btn-primary";
  if (provisionStep === 3) {
    next.innerHTML = '<i class="fas fa-rocket"></i> Provision';
    next.onclick = doProvision;
  } else {
    next.textContent = "Lanjut";
    next.onclick = () => { if (collectWizardData()) { provisionStep++; renderWizard(); } };
  }
  footer.appendChild(next);
}

function onServiceModeChange() {
  const val = document.getElementById("w-service-mode")?.value;
  if (val) provisionData.service_mode = val;
}

// ==================== GPON PROFILES (Fase 4) ====================
async function loadGponProfiles(kind) {
  if (!CURRENT_OLT_ID) return [];
  try {
    const r = await api(`/olts/${CURRENT_OLT_ID}/profiles/${kind}`);
    const items = Array.isArray(r.items) ? r.items : [];
    _gponProfilesCache[kind] = items;
    return items;
  } catch (e) {
    console.warn("[gpon-profiles] gagal load", kind, e);
    return [];
  }
}

async function loadAllGponProfiles() {
  if (!CURRENT_OLT_ID) return;
  await Promise.all([
    loadGponProfiles("tcont"),
    loadGponProfiles("traffic"),
  ]);
}

async function loadWizardInventory() {
  try {
    const [inv] = await Promise.all([
      api(`/onu/inventory?olt_id=${CURRENT_OLT_ID}`),
      loadAllGponProfiles(),   // Fase 4: fetch GPON profiles paralel
      loadWizardPackages(),    // Fase 4b-2: fetch Service Package
    ]);
    _inventoryCache = inv;
  } catch (e) {
    console.warn("Inventory fetch failed:", e);
    _inventoryCache = {};
  }
}

async function openSNPicker() {
  let data;
  try {
    data = await api(`/onu/uncfg?olt_id=${CURRENT_OLT_ID}`);
  } catch (e) {
    toast(`Gagal ambil uncfg: ${e.message}`, "error");
    return;
  }
  if (!data.items || !data.items.length) {
    toast("Tidak ada ONU baru di OLT", "info");
    return;
  }
  const html = `
    <p style="font-size:12px;color:var(--text-dim);margin-bottom:12px">
      ONU yang terdeteksi di OLT tapi belum terdaftar:
    </p>
    <table>
      <thead><tr><th>Index</th><th>Serial</th><th>Vendor</th><th>State</th><th></th></tr></thead>
      <tbody>
        ${data.items.map(it => `
          <tr>
            <td><b>${escapeHtml(it.onu_index)}</b></td>
            <td><code>${escapeHtml(it.serial_number)}</code></td>
            <td>${escapeHtml(it.vendor)}</td>
            <td>${escapeHtml(it.state)}</td>
            <td><button class="btn-primary" style="padding:4px 12px;font-size:11px" onclick="pickSN('${escapeHtml(it.serial_number)}', '${escapeHtml(it.pon_port || "")}', ${it.onu_id || "null"})">Pilih</button></td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
  openModal("Pilih SN dari OLT", html, [
    { label: "Tutup", cls: "btn-secondary", action: () => {
      document.getElementById("modal-title").textContent = "Provision ONU Baru";
      document.getElementById("modal-body").innerHTML = '<div id="wizard-content"></div>';
      renderWizard();
    }},
  ]);
}

function pickSN(sn, ponPort, onuId) {
  provisionData.serial_number = sn;
  if (ponPort) provisionData.pon_port = ponPort;
  if (onuId) provisionData.onu_id = onuId;
  document.getElementById("modal-title").textContent = "Provision ONU Baru";
  document.getElementById("modal-body").innerHTML = '<div id="wizard-content"></div>';
  renderWizard();
  toast(`SN ${sn} dipilih`, "success");
}

function collectWizardData() {
  if (provisionStep === 0) {
    provisionData.pon_port = document.getElementById("w-pon").value;
    provisionData.onu_id = parseInt(document.getElementById("w-onu-id").value);
  } else if (provisionStep === 1) {
    const sn = document.getElementById("w-sn").value.trim();
    const name = document.getElementById("w-name").value.trim();
    if (!sn || !name) { toast("SN dan nama wajib diisi", "error"); return false; }
    provisionData.serial_number = sn;
    provisionData.name = name;
    provisionData.description = (document.getElementById("w-desc")?.value || "").trim() || null;
    provisionData.onu_type = document.getElementById("w-type").value;
    provisionData.service_mode = document.getElementById("w-service-mode")?.value || "pppoe";
    // Map service_mode → provisioning_mode untuk backend
    provisionData.provisioning_mode = provisionData.service_mode === "pppoe" ? "routed" : "bridge";
  } else if (provisionStep === 2) {
    provisionData.tcont_profile = document.getElementById("w-tcont").value;
    provisionData.tcont_name = document.getElementById("w-tcont-name").value;
    provisionData.gemport_id = parseInt(document.getElementById("w-gem").value);
    provisionData.traffic_limit = document.getElementById("w-traffic").value;
    provisionData.service_port = parseInt(document.getElementById("w-sp").value);
    provisionData.vport = parseInt(document.getElementById("w-vport").value);
    provisionData.user_vlan = parseInt(document.getElementById("w-uvlan").value);
    provisionData.vlan = parseInt(document.getElementById("w-vlan").value);
  const pkEl = document.getElementById("w-package");
  const pkVal = pkEl && !pkEl.disabled ? pkEl.value : "custom";
  provisionData.package_custom = pkVal === "custom";
  provisionData.package_id = /^\d+$/.test(pkVal) ? parseInt(pkVal, 10) : null;
  const pkSel = provisionData.package_id ? (_packagesCache || []).find(p => p.id === provisionData.package_id) : null;
  if (pkSel) {
    provisionData.enable_nat = !!pkSel.pppoe_nat;
    provisionData.pppoe_nat = !!pkSel.pppoe_nat;
  }
    // Handle PPPoE field — cuma dibaca kalau service_mode = pppoe
    const sm = provisionData.service_mode || "pppoe";
    if (sm === "pppoe") {
      const pppoeEl = document.getElementById("w-pppoe");
      const pppoePassEl = document.getElementById("w-pppoe-pass");
      if (pppoeEl && !pppoeEl.disabled) {
        provisionData.pppoe_user = pppoeEl.value || null;
        provisionData.pppoe_password = pppoePassEl ? (pppoePassEl.value || null) : null;
      }
    } else {
      provisionData.pppoe_user = null;
      provisionData.pppoe_password = null;
    }
  }
  return true;
}

function generatePreview() {
  const d = provisionData;
  const iface = `gpon-onu_${d.pon_port}:${d.onu_id}`;
  const vendor = detectVendorFromSN(d.serial_number);
  const routedOk = isRoutedSupported(vendor);

  let cmds = `configure terminal
interface gpon-olt_${d.pon_port}
onu ${d.onu_id} type ${d.onu_type} sn ${d.serial_number}
exit
interface ${iface}
  name ${d.name}
  sn-bind enable sn
  tcont 1 name ${d.tcont_name} profile ${d.tcont_profile}
  gemport ${d.gemport_id} tcont 1
  gemport ${d.gemport_id} traffic-limit downstream ${d.traffic_limit}
  service-port ${d.service_port} vport ${d.vport} user-vlan ${d.user_vlan} vlan ${d.vlan}
exit
pon-onu-mng ${iface}
  service ${d.tcont_name} gemport ${d.gemport_id} iphost 1 vlan ${d.vlan}
  vlan port eth_0/1 mode tag vlan ${d.vlan}
  vlan port eth_0/2 mode tag vlan ${d.vlan}`;

  // Cuma ZTE yang support routed (proprietary)
  if (routedOk && d.pppoe_user) {
    cmds += `
  no pppoe 1
  [delay 3 detik]
  pppoe 1 nat enable user ${d.pppoe_user} password ${d.pppoe_password || "zte"}
  firewall enable level low anti-hack disable
  security-mgmt 1 state enable mode forward protocol web
  wan 1 service internet host 1`;
  }

  cmds += `
exit`;
  return cmds;
}

async function doProvision() {
  const footer = document.getElementById("modal-footer");
  const buttons = footer.querySelectorAll("button");
  const provisionBtn = buttons[buttons.length - 1];
  const originalHTML = provisionBtn.innerHTML;

  // Preflight
  provisionBtn.disabled = true;
  provisionBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Cek validasi...';

  let preflight;
  try {
    preflight = await api("/onu/preflight", {
      method: "POST",
      body: JSON.stringify({ olt_id: CURRENT_OLT_ID, ...provisionData }),
    });
  } catch (e) {
    provisionBtn.disabled = false;
    provisionBtn.innerHTML = originalHTML;
    toast(`Gagal cek preflight: ${e.message}`, "error");
    return;
  }

  // Kalau ada error
  if (!preflight.valid) {
    provisionBtn.disabled = false;
    provisionBtn.innerHTML = originalHTML;

    const errHTML = preflight.errors.map(e => `
      <div style="display:flex;gap:10px;padding:10px 12px;background:rgba(239,68,68,0.08);border-left:3px solid var(--red);border-radius:8px;margin-bottom:8px">
        <i class="fas fa-times-circle" style="color:var(--red);margin-top:2px;font-size:14px"></i>
        <div>
          <div style="font-weight:600;color:var(--red);font-size:12px;text-transform:uppercase;letter-spacing:0.5px">${escapeHtml(e.field)}</div>
          <div style="font-size:13px;color:var(--text-2);margin-top:2px">${escapeHtml(e.message)}</div>
        </div>
      </div>
    `).join("");

    const warnHTML = (preflight.warnings || []).map(w => `
      <div style="display:flex;gap:10px;padding:8px 12px;background:rgba(245,158,11,0.08);border-left:3px solid var(--yellow);border-radius:8px;margin-bottom:6px">
        <i class="fas fa-exclamation-triangle" style="color:var(--yellow);margin-top:2px;font-size:12px"></i>
        <div style="font-size:12px;color:var(--text-2)"><b>${escapeHtml(w.field)}:</b> ${escapeHtml(w.message)}</div>
      </div>
    `).join("");

    const suggHTML = (preflight.suggestions && Object.keys(preflight.suggestions).length > 0)
      ? `<div style="margin-top:14px;padding:12px;background:rgba(79,140,255,0.08);border-left:3px solid var(--primary);border-radius:8px">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--primary);font-weight:700;margin-bottom:6px">
            Saran ID kosong
          </div>
          <div style="font-size:12px;color:var(--text-2)">
            ${preflight.suggestions.service_port ? `<div>Service Port: <b>${preflight.suggestions.service_port}</b></div>` : ""}
            ${preflight.suggestions.gemport_id ? `<div>GEMPORT: <b>${preflight.suggestions.gemport_id}</b></div>` : ""}
            ${preflight.suggestions.onu_id ? `<div>ONU ID: <b>${preflight.suggestions.onu_id}</b></div>` : ""}
          </div>
        </div>`
      : "";

    const html = `
      <div style="padding:14px;background:rgba(239,68,68,0.1);border-left:3px solid var(--red);border-radius:8px;margin-bottom:16px">
        <div style="font-size:15px;font-weight:700;color:var(--red);margin-bottom:4px">
          <i class="fas fa-shield-alt"></i> Validasi Gagal
        </div>
        <div style="font-size:12px;color:var(--text-2)">
          Ada ${preflight.errors.length} masalah. Perbaiki dulu di step sebelumnya.
        </div>
      </div>
      ${errHTML}
      ${warnHTML ? `<div style="margin-top:14px;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--yellow);font-weight:700;margin-bottom:6px">Peringatan</div>${warnHTML}` : ""}
      ${suggHTML}
    `;

    document.getElementById("modal-body").innerHTML = html;
    const f2 = document.getElementById("modal-footer");
    f2.innerHTML = "";
    const b = document.createElement("button");
    b.className = "btn-secondary";
    b.textContent = "Kembali Edit";
    b.onclick = () => {
      document.getElementById("modal-title").textContent = "Provision ONU Baru";
      document.getElementById("modal-body").innerHTML = '<div id="wizard-content"></div>';
      renderWizard();
    };
    f2.appendChild(b);
    const cl = document.createElement("button");
    cl.className = "btn-primary";
    cl.textContent = "Tutup";
    cl.onclick = closeModal;
    f2.appendChild(cl);
    document.getElementById("modal-title").textContent = "Validasi Gagal";
    return;
  }

  // Valid → kirim
  provisionBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Mengirim...';

  try {
    const r = await api("/onu/provision", {
      method: "POST",
      body: JSON.stringify({ olt_id: CURRENT_OLT_ID, ...provisionData }),
    });
    closeModal();
    const sn = provisionData.serial_number || "ONU";
    if (r.duplicate) {
      toast(`ONU ${sn} sedang diproses`, "info");
    } else {
      toast(`Job dimulai: Provision ONU ${sn}`, "info");
    }
    showJobCard(r.job_id, "Provision ONU", sn);
  } catch (e) {
    provisionBtn.disabled = false;
    provisionBtn.innerHTML = originalHTML;
    toast(`Gagal mulai job: ${e.message}`, "error");
  }
}

// =================== GENERIC JOB SUBMIT ===================
let _submitInProgress = false;   // ⭐ Guard: cegah double submit

async function submitJob(endpoint, opts = {}) {
  // ⭐ Guard: cegah double submit
  if (_submitInProgress) {
    console.log("[SUBMIT] Skip — submit lain sedang berjalan:", endpoint);
    return null;
  }
  _submitInProgress = true;
  const guardRelease = setTimeout(() => { _submitInProgress = false; }, 15000);

  const method = opts.method || "POST";
  const label = opts.label || "Operasi";
  const subject = opts.subject || "";
  const refresh = opts.refresh;

  try {
    const body = opts.body ? JSON.stringify(opts.body) : undefined;
    const r = await api(endpoint, { method, body });

    if (r && r.job_id) {
      if (r.duplicate) {
        toast(`Aksi sedang diproses (job berjalan)`, "info");
      } else {
        toast(`Job dimulai: ${label}`, "info");
      }
      showJobCard(r.job_id, label, subject || endpoint, refresh);
      return r;
    }

    if (r && r.ok) {
      toast(`${label} sukses`, "success");
      if (refresh) refresh();
    } else {
      toast(`${label} selesai`, "info");
      if (refresh) refresh();
    }
    return r;
  } catch (e) {
    let msg = e.message || "Unknown error";
    try {
      const p = JSON.parse(msg);
      if (p.message) msg = p.message;
      else if (p.detail && typeof p.detail === "string") msg = p.detail;
      else if (p.detail && p.detail.message) msg = p.detail.message;
    } catch (_) {}
    toast(`Gagal ${label.toLowerCase()}: ${msg}`, "error");
    throw e;
  } finally {
    clearTimeout(guardRelease);
    _submitInProgress = false;
  }
}


// =================== FLOATING JOB CARD ===================
const _jobPollers = {};   // {job_id: intervalId}
const _jobRefreshCallbacks = {};   // {job_id: callback}

// =================== ONU UNCFG (BELUM TERDAFTAR) ===================
async function loadUncfg(force = false) {
  const widget = document.getElementById("widget-uncfg");
  const el = document.getElementById("uncfg-list");
  if (!widget || !el) return;

  try {
    // Pakai cache kalau sudah ada (kecuali force=true)
    if (uncfgCache === null || force) {
      const r = await api(`/olts/${CURRENT_OLT_ID}/sync-pons`, { method: "POST" });
      uncfgCache = r.unconfigured || [];
    }
    const list = uncfgCache;

    if (!list.length) {
      widget.style.display = "none";
      return;
    }

    widget.style.display = "block";
    el.innerHTML = list.map(u => `
      <div class="onu-optical-item" style="border-left:3px solid var(--yellow)">
        <div class="alert-icon" style="background:rgba(245,158,11,0.15);color:var(--yellow);width:32px;height:32px;display:flex;align-items:center;justify-content:center;border-radius:8px">
          <i class="fas fa-plug"></i>
        </div>
        <div class="onu-optical-info">
          <div class="onu-optical-name">${escapeHtml(u.vendor || "Unknown")} · ${escapeHtml(u.serial_number || "")}</div>
          <div class="onu-optical-sn">${escapeHtml(u.onu_index || "")} · state: ${escapeHtml(u.state || "")}</div>
        </div>
        <div class="onu-optical-value">
          <button class="btn-primary" style="padding:6px 12px;font-size:11px"
                  onclick="provisionDetected('${escapeHtml(u.serial_number || "")}', '${escapeHtml(u.onu_index || "")}')">
            <i class="fas fa-plus"></i> Daftarkan
          </button>
        </div>
      </div>
    `).join("");
  } catch (e) {
    widget.style.display = "none";
    console.warn("loadUncfg error:", e);
  }
}


function provisionDetected(sn, onuIndex) {
  // Parse PON & ONU ID dari onuIndex (gpon-onu_1/1/1:1)
  const m = onuIndex.match(/gpon-onu_([\d\/]+):(\d+)/);
  if (!m) { toast("Format ONU index tidak dikenali", "error"); return; }
  const ponFull = m[1];              // 1/1/1
  const onuId = parseInt(m[2]);

  // ⭐ Set provisionData DULU sebelum buka wizard
  // Wizard akan auto-populate dari state ini (template sudah pakai value="${provisionData.X}")
  provisionData = {
    pon_port: ponFull,
    onu_id: onuId,
    serial_number: sn,
    name: "",
    onu_type: "F609",
  };
  provisionStep = 0;   // mulai dari step 1 (Pilih PON)

  toast(`Buka form Provision untuk SN ${sn}`, "info");

  // ⭐ Set flag supaya button handler tidak reset provisionData
  _skipWizardReset = true;

  // Pindah ke menu ONU + buka wizard
  document.querySelector('[data-page="onus"]').click();
  setTimeout(() => {
    const btn = document.getElementById("btn-provision-onu");
    if (btn) btn.click();
  }, 200);
}


function showJobCard(jobId, jobType, subject, refreshCallback = null) {
  const id = `job-${jobId}`;
  let card = document.getElementById(id);
  if (!card) {
    card = document.createElement("div");
    card.id = id;
    card.className = "job-card";
    card.style.cssText = `
      position: fixed; bottom: 20px; right: 20px; z-index: 9999;
      width: 320px; background: var(--bg-2); border: 1px solid var(--border);
      border-radius: 12px; padding: 14px 16px; box-shadow: 0 8px 24px rgba(0,0,0,0.4);
      font-size: 13px; animation: slideIn 0.3s ease-out;
    `;
    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <strong style="font-size:12px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-dim)">${escapeHtml(jobType)}</strong>
        <span class="job-close" style="cursor:pointer;color:var(--text-dim)">×</span>
      </div>
      <div class="job-subject" style="font-weight:600;margin-bottom:8px">${escapeHtml(subject)}</div>
      <div style="background:var(--bg);border-radius:6px;height:6px;overflow:hidden;margin-bottom:8px">
        <div class="job-progress" style="height:100%;width:0%;background:linear-gradient(90deg,#4f8cff,#3b74e8);transition:width 0.5s ease;border-radius:6px"></div>
      </div>
      <div class="job-message" style="color:var(--text-dim);font-size:12px">Menunggu antrean...</div>
      <div class="job-percent" style="position:absolute;top:14px;right:36px;font-size:11px;color:var(--text-dim)">0%</div>
    `;
    document.body.appendChild(card);

    // Tombol close manual
    card.querySelector(".job-close").onclick = () => {
      if (_jobPollers[jobId]) { clearInterval(_jobPollers[jobId]); delete _jobPollers[jobId]; }
      card.remove();
    };
  }

  // Simpan refresh callback
  if (refreshCallback) {
    _jobRefreshCallbacks[jobId] = refreshCallback;
  }

  // Mulai polling
  if (!_jobPollers[jobId]) {
    _jobPollers[jobId] = setInterval(() => pollJobStatus(jobId, card), 3000);
    pollJobStatus(jobId, card);  // poll instan pertama
  }
}


async function pollJobStatus(jobId, card) {
  if (!document.getElementById(card.id)) {
    if (_jobPollers[jobId]) { clearInterval(_jobPollers[jobId]); delete _jobPollers[jobId]; }
    return;
  }

  try {
    const job = await api(`/jobs/${jobId}`);
    const progBar = card.querySelector(".job-progress");
    const msgEl = card.querySelector(".job-message");
    const pctEl = card.querySelector(".job-percent");

    if (progBar) progBar.style.width = (job.progress || 0) + "%";
    if (pctEl) pctEl.textContent = (job.progress || 0) + "%";
    if (msgEl) msgEl.textContent = job.message || job.status;

    // Done?
    if (job.status === "success") {
      if (_jobPollers[jobId]) { clearInterval(_jobPollers[jobId]); delete _jobPollers[jobId]; }
      // ⭐ Panggil refresh callback kalau ada
      const _cb = _jobRefreshCallbacks[jobId];
      if (_cb) { try { _cb(); } catch (_) {} delete _jobRefreshCallbacks[jobId]; }

      const hasWarning = job.result && job.result.warning;
      const subject = escapeHtml(card.querySelector(".job-subject")?.textContent || "");

      if (hasWarning) {
        // ⚠️ Sukses tapi ada warning (mis. PPPoE belum dial)
        card.style.borderColor = "var(--yellow)";
        if (progBar) progBar.style.background = "linear-gradient(90deg,#f59e0b,#d97706)";
        if (msgEl) {
          msgEl.innerHTML = `<span style="color:var(--yellow);font-weight:600">⚠️ ONU Terdaftar</span><br><span style="font-size:11px;color:var(--text-2)">${escapeHtml(job.result.warning)}</span>`;
        }
        toast(`⚠️ ONU ${subject} terdaftar, tapi ${escapeHtml((job.result.warning || "").slice(0, 60))}`, "warning");
        setTimeout(() => { if (card.parentNode) card.remove(); }, 15000);
      } else {
        // ✅ Sukses penuh — tidak ada warning
        card.style.borderColor = "var(--green)";
        if (progBar) progBar.style.background = "linear-gradient(90deg,#10b981,#059669)";
        if (msgEl) {
          msgEl.innerHTML = `<span style="color:var(--green);font-weight:600">✅ Berhasil</span>`;
        }
        // Info internet (kalau ada)
        if (job.result && job.result.internet_status) {
          const dur = job.result.internet_online_duration || 0;
          msgEl.innerHTML += `<br><span style="font-size:11px">Internet: ${escapeHtml(job.result.internet_status)}${dur > 0 ? ` · ${formatUptime(dur)}` : ""}</span>`;
        }
        toast(`✅ Provision ONU ${subject} berhasil`, "success");
        setTimeout(() => { if (card.parentNode) card.remove(); }, 10000);
      }

      loadONUs();  // refresh tabel
      return;
    }

    if (job.status === "failed") {
      if (_jobPollers[jobId]) { clearInterval(_jobPollers[jobId]); delete _jobPollers[jobId]; }
      delete _jobRefreshCallbacks[jobId];
      card.style.borderColor = "var(--red)";
      if (progBar) progBar.style.background = "linear-gradient(90deg,#ef4444,#b91c1c)";
      if (msgEl) {
        msgEl.innerHTML = `<span style="color:var(--red);font-weight:600">❌ Gagal</span><br><span style="font-size:11px">${escapeHtml(job.error || "Unknown error")}</span>`;
      }
      toast(`❌ Provision gagal: ${escapeHtml((job.error || "").slice(0, 80))}`, "error");
      setTimeout(() => { if (card.parentNode) card.remove(); }, 15000);
      return;
    }
    // queued/running — lanjut poll
  } catch (e) {
    // Job 404 / expired → stop polling
    if (e.message && e.message.includes("404")) {
      if (_jobPollers[jobId]) { clearInterval(_jobPollers[jobId]); delete _jobPollers[jobId]; }
    }
  }
}


// =================== RESUME JOB SAAT REFRESH ===================
function resumeActiveJobs() {
  // Cek job yang masih running di server → tampilkan card-nya
  api("/jobs").then(jobs => {
    if (!Array.isArray(jobs)) return;
    jobs.forEach(j => {
      const subject = (j.result && j.result.onu_index) || j.type || "Job";
      showJobCard(j.id, j.type || "Job", subject);
    });
  }).catch(() => {});
}


function showProvisionResult(r, data) {
  const statusMap = {
    connected:    ['online',   'fa-check-circle',      'Connected'],
    disconnected: ['offline',  'fa-times-circle',      'Disconnected'],
    connecting:   ['warning',  'fa-spinner',           'Connecting...'],
    unknown:      ['shutdown', 'fa-question-circle',   'Unknown'],
  };
  const [cls, icon, label] = statusMap[r.internet_status] || statusMap.unknown;
  const internetBadge = `<span class="status-badge ${cls}"><i class="fas ${icon}"></i> ${label}</span>`;
  const dur = r.internet_online_duration
    ? (r.internet_online_duration > 60 ? formatUptime(r.internet_online_duration) : r.internet_online_duration + "s")
    : "-";

  const html = `
    <div style="text-align:center;margin-bottom:20px">
      <div style="width:64px;height:64px;margin:0 auto 12px;border-radius:50%;background:rgba(16,185,129,0.15);display:flex;align-items:center;justify-content:center">
        <i class="fas fa-check-circle" style="font-size:32px;color:var(--green)"></i>
      </div>
      <h3 style="margin-bottom:6px;font-size:16px">ONU Berhasil di-provision</h3>
      <p style="color:var(--text-dim);font-size:12px"><code>${escapeHtml(r.onu_index || "")}</code></p>
    </div>
    <table>
      <tr><td>Serial Number</td><td><code>${escapeHtml(data.serial_number || "-")}</code></td></tr>
      <tr><td>Nama</td><td>${escapeHtml(data.name || "-")}</td></tr>
      <tr><td>ONU ID</td><td>${data.onu_id}</td></tr>
      <tr><td>Service Port</td><td>${data.service_port}</td></tr>
      <tr><td>VLAN</td><td>${data.user_vlan} → ${data.vlan}</td></tr>
      <tr><td>PPPoE User</td><td>${escapeHtml(data.pppoe_user || "-")}</td></tr>
      <tr><td><b>Status Internet</b></td><td>${internetBadge}</td></tr>
      <tr><td>Online Duration</td><td>${dur}</td></tr>
    </table>
    <p style="margin-top:16px;color:var(--text-dim);font-size:12px;line-height:1.5">
      <i class="fas fa-info-circle"></i> Data lengkap (RX, Distance) akan muncul setelah sync berikutnya, atau klik ☁️ Sync di topbar.
    </p>
  `;

  openModal("✅ Hasil Provisioning", html, [
    { label: "Tutup", cls: "btn-secondary", action: () => { closeModal(); } },
    { label: "Lihat di ONU", cls: "btn-primary", action: () => {
      closeModal();
      document.querySelector('[data-page="onus"]').click();
    }},
  ]);
}

function showProvisionError(e, data) {
  const msg = (e && e.message) ? e.message : "Terjadi kesalahan tidak dikenal";

  const html = `
    <div style="text-align:center;margin-bottom:20px">
      <div style="width:64px;height:64px;margin:0 auto 12px;border-radius:50%;background:rgba(239,68,68,0.15);display:flex;align-items:center;justify-content:center">
        <i class="fas fa-times-circle" style="font-size:32px;color:var(--red)"></i>
      </div>
      <h3 style="margin-bottom:6px;font-size:16px">Provisioning Gagal</h3>
      <p style="color:var(--text-dim);font-size:12px">SN <code>${escapeHtml((data && data.serial_number) || "-")}</code></p>
    </div>
    <div style="background:var(--bg);padding:12px 14px;border-radius:8px;border:1px solid var(--border);font-size:12px;color:var(--red);word-break:break-word;line-height:1.5;max-height:220px;overflow:auto">
      ${escapeHtml(msg)}
    </div>
    <div style="margin-top:16px;padding:12px 14px;background:rgba(79,140,255,0.08);border-radius:8px;border-left:3px solid var(--primary);font-size:12px;color:var(--text-2);line-height:1.5">
      <b><i class="fas fa-lightbulb"></i> Yang bisa dilakukan:</b>
      <ul style="margin:8px 0 0 20px;padding:0">
        <li>Klik ☁️ Sync untuk cek apakah ONU setengah terdaftar di OLT</li>
        <li>Kalau muncul di panel "ONU Belum Terdaftar" — coba daftarkan ulang</li>
        <li>Cek log server (terminal uvicorn) untuk detail error</li>
      </ul>
    </div>
  `;

  openModal("❌ Gagal Provisioning", html, [
    { label: "Tutup", cls: "btn-secondary", action: () => { closeModal(); } },
    { label: "Refresh Halaman", cls: "btn-primary", action: () => {
      closeModal();
      location.reload();
    }},
  ]);
}

// =================== INTERFACES ===================
async function loadInterfaces(opts = {}, pre = null) {
  const el = document.getElementById("iface-table");
  try {
    const ifaces = pre || await api(`/olts/${CURRENT_OLT_ID}/interfaces`, opts);
    el.innerHTML = `
      <div class="table-wrap"><table class="table-premium">
        <thead><tr>
          <th>Interface</th><th>Tipe</th><th>Status</th><th>Admin</th>
          <th>Speed</th><th>Duplex</th><th>Mode</th><th>VLANs</th><th>Attribute</th><th>Aksi</th>
        </tr></thead>
        <tbody>
          ${ifaces.map(i => `
            <tr>
              <td><b>${escapeHtml(i.name)}</b></td>
              <td>${escapeHtml(i.type)}</td>
              <td><span class="status-badge ${escapeHtml(i.status)}">${escapeHtml(i.status)}</span></td>
              <td>${escapeHtml(i.admin_state)}</td>
              <td>${i.speed ? i.speed + " Mbps" : "-"}</td>
              <td>${escapeHtml(i.duplex || "-")}</td>
              <td>${escapeHtml(i.switchport_mode || "-")}</td>
              <td>${escapeHtml(i.vlans || "-")}</td>
              <td>${escapeHtml(i.hybrid_attribute || "-")}</td>
              <td style="white-space:nowrap">
                <button class="btn-icon" onclick="togglePort('uplink', '${escapeHtml(i.name)}', '${i.admin_state === "no shutdown" ? "disable" : "enable"}')" title="${i.admin_state === "no shutdown" ? "Disable port" : "Enable port"}">
                  <i class="fas fa-power-off" style="color:${i.admin_state === "no shutdown" ? "var(--green)" : "var(--text-dim)"}"></i>
                </button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table></div>
    `;
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}

// =================== VLANS ===================
async function loadVLANs(opts = {}) {
  const el = document.getElementById("vlan-table");
  try {
    const vlans = await api(`/olts/${CURRENT_OLT_ID}/vlans`, opts);
    el.innerHTML = `
      <div class="table-wrap"><table class="table-premium">
        <thead><tr><th>VLAN ID</th><th>Nama</th><th>Deskripsi</th><th>Aksi</th></tr></thead>
        <tbody>
          ${vlans.map(v => `
            <tr>
              <td><b>${v.vlan_id}</b></td>
              <td>${escapeHtml(v.name || "-")}</td>
              <td>${escapeHtml(v.description || "-")}</td>
              <td>${CURRENT_USER.privilege >= 10 ? `<button class="btn-icon" onclick="deleteVLAN(${v.vlan_id})"><i class="fas fa-trash"></i></button>` : ""}</td>
            </tr>
          `).join("")}
        </tbody>
      </table></div>
    `;
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}

document.getElementById("btn-add-vlan")?.addEventListener("click", () => {
  if (CURRENT_USER.privilege < 10) { toast("Privilege tidak cukup", "error"); return; }
  openModal("Tambah VLAN", `
    <div class="form-group"><label>VLAN ID</label><input type="number" id="v-id" min="1" max="4094"></div>
    <div class="form-group"><label>Nama</label><input id="v-name"></div>
    <div class="form-group"><label>Deskripsi</label><input id="v-desc"></div>
  `, [
    { label: "Kembali", cls: "btn-secondary", action: closeModal },
    { label: "Simpan", cls: "btn-primary", action: async () => {
      // Loading state
      const footer = document.getElementById("modal-footer");
      const btns = footer.querySelectorAll("button");
      btns.forEach(b => b.disabled = false);
      const saveBtn = btns[btns.length - 1];
      saveBtn.disabled = true;
      const origHTML = saveBtn.innerHTML;
      saveBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Menyimpan...';

      // Saat proses: disable tombol Kembali + ubah label jadi "Tunggu..."
      btns.forEach(b => {
        const txt = b.textContent.trim();
        if (txt === "Kembali" || txt === "Tutup") {
          b.disabled = true;
          b.innerHTML = '<i class="fas fa-hourglass-half"></i> Tunggu...';
        }
      });

      const vlanId = parseInt(document.getElementById("v-id").value);
      const vlanName = document.getElementById("v-name")?.value || "";
      const vlanDesc = document.getElementById("v-desc")?.value || "";

      try {
        closeModal();
        await submitJob(`/olts/${CURRENT_OLT_ID}/vlans`, {
          method: "POST",
          label: `Buat VLAN ${vlanId}`,
          subject: `VLAN ${vlanId}`,
          body: { vlan_id: vlanId, name: vlanName, description: vlanDesc },
          refresh: loadVLANs,
        });
      } catch (e) {
        // Restore tombol biar bisa coba lagi
        saveBtn.innerHTML = origHTML;
        btns.forEach(b => b.disabled = false);
      }
    }},
  ]);
});

async function deleteVLAN(id) {
  // 1. Cek dependency dulu
  let deps;
  try {
    deps = await api(`/olts/${CURRENT_OLT_ID}/vlans/${id}/dependencies`);
  } catch (e) {
    toast(`❌ Gagal cek dependency: ${e.message}`, "error");
    return;
  }

  // 2. Kalau VLAN manajemen — blok total
  if (deps.is_management) {
    openModal("⛔ VLAN Manajemen — Tidak Bisa Dihapus", `
      <div style="padding:16px;background:rgba(239,68,68,0.08);border-left:3px solid var(--red);border-radius:8px;margin-bottom:16px">
        <div style="font-size:15px;font-weight:700;color:var(--red);margin-bottom:8px">
          <i class="fas fa-shield-alt"></i> VLAN ${id} dipakai untuk manajemen OLT
        </div>
        <div style="font-size:12px;color:var(--text-2)">
          Menghapus VLAN ini bisa memutus akses ke OLT. Fitur ini diblokir untuk mencegah outage.
        </div>
      </div>
      <p style="font-size:12px;color:var(--text-dim)">
        Kalau memang perlu hapus, edit di CLI OLT langsung dengan hati-hati.
      </p>
    `, [
      { label: "Tutup", cls: "btn-secondary", action: closeModal },
    ]);
    return;
  }

  // 3. Kalau ada dependency — tampilkan detail + force option
  if (deps.onu_count > 0 || deps.interface_count > 0) {
    let onuListHTML = "";
    if (deps.onus && deps.onus.length > 0) {
      onuListHTML = `
        <div style="margin-top:14px">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--text-dim);font-weight:700;margin-bottom:8px">ONU yang pakai VLAN ${id} (${deps.onu_count} total)</div>
          <div style="max-height:200px;overflow-y:auto;border:1px solid var(--border);border-radius:8px">
            <table style="font-size:12px;margin:0">
              <thead><tr><th>PON</th><th>ONU</th><th>SN</th><th>Nama</th><th>Status</th></tr></thead>
              <tbody>
                ${deps.onus.map(o => `
                  <tr>
                    <td>${escapeHtml(o.pon_port)}</td>
                    <td>${o.onu_id}</td>
                    <td><code>${escapeHtml(o.serial_number || "-")}</code></td>
                    <td>${escapeHtml(o.name || "-")}</td>
                    <td><span class="status-badge ${escapeHtml(o.status)}">${escapeHtml(o.status)}</span></td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          </div>
          ${deps.onu_count > deps.onus.length ? `<div style="font-size:11px;color:var(--text-dim);margin-top:6px">... dan ${deps.onu_count - deps.onus.length} ONU lainnya</div>` : ""}
        </div>
      `;
    }

    let ifaceListHTML = "";
    if (deps.interfaces && deps.interfaces.length > 0) {
      ifaceListHTML = `
        <div style="margin-top:14px">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--text-dim);font-weight:700;margin-bottom:8px">Uplink interface yang pakai VLAN ${id}</div>
          <div style="border:1px solid var(--border);border-radius:8px;padding:10px">
            ${deps.interfaces.map(i => `<div style="font-size:12px;padding:4px 0"><code>${escapeHtml(i.name)}</code> · ${escapeHtml(i.type)} · <span class="status-badge ${escapeHtml(i.status)}">${escapeHtml(i.status)}</span></div>`).join("")}
          </div>
        </div>
      `;
    }

    const html = `
      <div style="padding:16px;background:rgba(239,68,68,0.08);border-left:3px solid var(--red);border-radius:8px;margin-bottom:16px">
        <div style="font-size:15px;font-weight:700;color:var(--red);margin-bottom:6px">
          <i class="fas fa-exclamation-triangle"></i> VLAN ${id} masih dipakai
        </div>
        <div style="font-size:13px;color:var(--text-2)">
          <b>${deps.onu_count} ONU</b> dan <b>${deps.interface_count} interface uplink</b> memakai VLAN ini.
        </div>
        <div style="font-size:11px;color:var(--text-dim);margin-top:8px">
          Menghapus VLAN ini akan memutus trafik pelanggan tanpa alert — ONU tetap "online" tapi internet mati.
        </div>
      </div>
      ${ifaceListHTML}
      ${onuListHTML}
      <p style="font-size:12px;color:var(--text-dim);margin-top:16px">
        <i class="fas fa-info-circle"></i> Rekomendasi: <b>jangan hapus</b>. Kalau memang perlu, pindahkan dulu ONU/interface ke VLAN lain.
      </p>
    `;

    openModal(`⚠️ Hapus VLAN ${id} — Banyak Dependency`, html, [
      { label: "Batal (Rekomendasi)", cls: "btn-secondary", action: closeModal },
      { label: "Hapus Paksa", cls: "btn-primary", action: async () => {
        closeModal();
        try {
          await submitJob(`/olts/${CURRENT_OLT_ID}/vlans/${id}?force=true`, {
            method: "DELETE",
            label: `Hapus VLAN ${id} (paksa)`,
            subject: `VLAN ${id}`,
            refresh: loadVLANs,
          });
        } catch (e) {}
      }},
    ]);
    return;
  }

  // 4. Nggak ada dependency — hapus langsung
  if (!confirm(`Hapus VLAN ${id}?\n\nVLAN ini tidak dipakai ONU/interface manapun.`)) return;
  try {
    await submitJob(`/olts/${CURRENT_OLT_ID}/vlans/${id}`, {
      method: "DELETE",
      label: `Hapus VLAN ${id}`,
      subject: `VLAN ${id}`,
      refresh: loadVLANs,
    });
  } catch (e) {}
}

// =================== ALERTS ===================
async function loadAlerts(opts = {}) {
  const el = document.getElementById("alert-list");
  if (!el) return;
  try {
    const sev = document.getElementById("alert-severity-filter").value;
    const unresolved = document.getElementById("alert-unresolved").checked;
    let url = "/alerts?limit=100";
    if (sev) url += `&severity=${sev}`;
    if (unresolved) url += `&resolved=false`;
    const alerts = await api(url, opts);
    if (!alerts.length) {
      el.innerHTML = `
        <div class="widget" style="text-align:center;padding:40px">
          <i class="fas fa-check-circle" style="font-size:36px;color:var(--green);margin-bottom:12px;display:block"></i>
          <p style="color:var(--text-dim)">Tidak ada alert</p>
        </div>`;
      return;
    }
    el.innerHTML = `
      <div class="toolbar" style="margin-bottom:12px;gap:8px">
        <label class="checkbox" style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="alert-select-all" style="accent-color:var(--primary);width:16px;height:16px">
          <span>Pilih semua</span>
        </label>
        <button class="btn-secondary" id="alert-bulk-delete-btn" disabled
                onclick="bulkDeleteAlerts()" style="margin-left:auto">
          <i class="fas fa-trash"></i> Hapus Terpilih <span id="alert-selected-count"></span>
        </button>
      </div>
      <div class="alert-list" id="alert-items">
        ${alerts.map(a => alertItemHTML(a, true)).join("")}
      </div>
    `;
    _bindAlertCheckboxes();
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}


// ---- WA-2: tab di halaman Alerts ----
var _alertTab = "system";

function switchAlertTab(tabId) {
  _alertTab = tabId || "system";
  ["system", "wa-log"].forEach(function (t) {
    var el = document.getElementById("alert-tab-" + t);
    if (el) el.hidden = (t !== _alertTab);
  });
  var btns = document.querySelectorAll("#alert-tabs .alert-tab-btn");
  btns.forEach(function (b) {
    b.classList.toggle("active", b.getAttribute("data-alert-tab") === _alertTab);
  });
  if (_alertTab === "system") {
    loadAlerts();
  } else if (_alertTab === "wa-log") {
    if (window.WaBot && WaBot.mountAlertLog) {
      WaBot.mountAlertLog(document.getElementById("alert-tab-wa-log"), "global");
    }
  }
}

function _bindAlertTabs() {
  var tabs = document.getElementById("alert-tabs");
  if (!tabs) return;
  tabs.querySelectorAll(".alert-tab-btn").forEach(function (b) {
    b.addEventListener("click", function () {
      switchAlertTab(b.getAttribute("data-alert-tab"));
    });
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", _bindAlertTabs);
} else {
  _bindAlertTabs();
}


function _bindAlertCheckboxes() {
  const allBox = document.getElementById("alert-select-all");
  const items = document.querySelectorAll(".alert-checkbox");
  const delBtn = document.getElementById("alert-bulk-delete-btn");
  const counter = document.getElementById("alert-selected-count");

  function updateState() {
    const checked = document.querySelectorAll(".alert-checkbox:checked").length;
    if (delBtn) delBtn.disabled = checked === 0;
    if (counter) counter.textContent = checked > 0 ? `(${checked})` : "";
    if (allBox) allBox.checked = checked === items.length && items.length > 0;
  }

  items.forEach(cb => cb.addEventListener("change", updateState));
  if (allBox) {
    allBox.addEventListener("change", () => {
      items.forEach(cb => cb.checked = allBox.checked);
      updateState();
    });
  }
}


async function bulkDeleteAlerts() {
  const ids = Array.from(document.querySelectorAll(".alert-checkbox:checked"))
    .map(cb => parseInt(cb.dataset.id));

  if (!ids.length) { toast("Tidak ada alert dipilih", "warning"); return; }
  if (!confirm(`Hapus ${ids.length} alert?`)) return;

  try {
    const r = await api("/alerts/bulk-delete", {
      method: "POST",
      body: JSON.stringify(ids),
    });
    toast(`✅ ${r.deleted} alert dihapus`, "success");
    loadAlerts();
    loadAlertBadge();
    loadRecentAlerts();
  } catch (e) { toast(e.message, "error"); }
}
document.getElementById("alert-severity-filter")?.addEventListener("change", () => loadAlerts());
document.getElementById("alert-unresolved")?.addEventListener("change", () => loadAlerts());

// =================== AUDIT ===================
async function loadAudit(opts = {}) {
  const el = document.getElementById("audit-table");
  try {
    const logs = await api("/audit-logs?limit=200", opts);
    el.innerHTML = `
      <div class="toolbar" style="margin-bottom:12px;gap:8px">
        <button class="btn-secondary" id="audit-bulk-delete-btn" disabled
                onclick="bulkDeleteAudit()">
          <i class="fas fa-trash"></i> Hapus Terpilih <span id="audit-selected-count"></span>
        </button>
      </div>
      <div class="table-wrap"><table class="table-premium">
        <thead><tr>
          <th style="width:36px"><input type="checkbox" id="audit-select-all" style="accent-color:var(--primary)"></th>
          <th>Time</th><th>User</th><th>Action</th><th>Target</th><th>Detail</th><th>Result</th><th>IP</th>
        </tr></thead>
        <tbody>
          ${logs.map(l => `
            <tr>
              <td><input type="checkbox" class="audit-checkbox" data-id="${l.id}" style="accent-color:var(--primary)"></td>
              <td>${l.created_at ? new Date(l.created_at).toLocaleString("id-ID") : "-"}</td>
              <td><b>${escapeHtml(l.username)}</b></td>
              <td>${escapeHtml(l.action)}</td>
              <td>${escapeHtml(l.target)}</td>
              <td>${escapeHtml(l.detail || "-")}</td>
              <td><span class="status-badge ${l.result === "success" ? "online" : "critical"}">${escapeHtml(l.result)}</span></td>
              <td>${escapeHtml(l.ip_address || "-")}</td>
            </tr>
          `).join("")}
        </tbody>
      </table></div>
    `;
    _bindAuditCheckboxes();
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}


function _bindAuditCheckboxes() {
  const allBox = document.getElementById("audit-select-all");
  const items = document.querySelectorAll(".audit-checkbox");
  const delBtn = document.getElementById("audit-bulk-delete-btn");
  const counter = document.getElementById("audit-selected-count");

  function updateState() {
    const checked = document.querySelectorAll(".audit-checkbox:checked").length;
    if (delBtn) delBtn.disabled = checked === 0;
    if (counter) counter.textContent = checked > 0 ? `(${checked})` : "";
    if (allBox) allBox.checked = checked === items.length && items.length > 0;
  }

  items.forEach(cb => cb.addEventListener("change", updateState));
  if (allBox) {
    allBox.addEventListener("change", () => {
      items.forEach(cb => cb.checked = allBox.checked);
      updateState();
    });
  }
}


async function bulkDeleteAudit() {
  const ids = Array.from(document.querySelectorAll(".audit-checkbox:checked"))
    .map(cb => parseInt(cb.dataset.id));

  if (!ids.length) { toast("Tidak ada log dipilih", "warning"); return; }
  if (!confirm(`Hapus ${ids.length} audit log?`)) return;

  try {
    const r = await api("/audit-logs/bulk-delete", {
      method: "POST",
      body: JSON.stringify(ids),
    });
    toast(`✅ ${r.deleted} audit log dihapus`, "success");
    loadAudit();
  } catch (e) { toast(e.message, "error"); }
}

// =================== USERS ===================
// Phase B3: collapse/expand user tree
function toggleUserTree(uid) {
  const row = document.querySelector('[data-uid="' + uid + '"]');
  if (!row) return;
  const btn = row.querySelector('.user-tree-toggle i');
  const isExpanded = btn.classList.contains('fa-chevron-down');

  const allRows = document.querySelectorAll('.user-tree-row');
  let targetDepth = parseInt(row.style.paddingLeft || '0', 10);
  let i = Array.from(allRows).indexOf(row);
  for (let j = i + 1; j < allRows.length; j++) {
    const next = allRows[j];
    const nextDepth = parseInt(next.style.paddingLeft || '0', 10);
    if (nextDepth <= targetDepth) break;
    next.classList.toggle('hidden', isExpanded);
  }

  btn.classList.toggle('fa-chevron-down', !isExpanded);
  btn.classList.toggle('fa-chevron-right', isExpanded);
}

async function loadUsers(opts = {}) {
  const el = document.getElementById("user-table");
  try {
    const users = await api("/users", opts);
    // Banner edukasi
    const banner = `
      <div style="background:rgba(79,140,255,0.08);border-left:3px solid var(--primary);border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:12px;color:var(--text-2);line-height:1.5">
        <b style="color:var(--primary)"><i class="fas fa-info-circle"></i> Perhatian:</b>
        Akun di sini <b>HANYA untuk login ke Dashboard Aplikasi</b>.
        Untuk login CLI/Telnet ke OLT, gunakan akun OLT yang dibuat langsung via CLI (bukan dari sini).
      </div>
    `;
    el.innerHTML = banner + `
      <div class="table-wrap"><table class="table-premium">
        <thead><tr>
          <th style="width:32px"></th>
          <th>Username</th><th>Nama Lengkap</th><th>Role</th><th>Owner</th>
          <th>Status</th><th>Last Login</th><th style="text-align:right">Aksi</th>
        </tr></thead>
        <tbody>
          ${(() => {
            const rows = [];
            const subordinates = {};
            const topLevel = [];

            users.forEach(u => {
              if (u.owner_user_id) {
                (subordinates[u.owner_user_id] = subordinates[u.owner_user_id] || []).push(u);
              } else {
                topLevel.push(u);
              }
            });

            const renderRow = (u, depth, hasChildren) => {
              const isSelf = u.username === CURRENT_USER.username;
              const statusBadge = u.is_active
                ? '<span class="status-badge online">Aktif</span>'
                : '<span class="status-badge offline">Nonaktif</span>';
              let roleLabel = escapeHtml(u.role);
              if (u.is_super_admin) roleLabel = '👑 Super Admin';
              const indent = depth > 0 ? 'padding-left:' + (depth * 24) + 'px;' : '';
              const arrow = hasChildren
                ? '<button class="user-tree-toggle" onclick="toggleUserTree(' + u.id + ')"><i class="fas fa-chevron-down"></i></button>'
                : '<span style="display:inline-block;width:20px"></span>';
              const childCount = hasChildren ? '<span style="font-size:10px;color:var(--text-dim);margin-left:6px">(' + subordinates[u.id].length + ')</span>' : '';
              let ownerLabel = "-";
              if (u.owner_user_id) {
                const ow = users.find(x => x.id === u.owner_user_id);
                ownerLabel = ow ? escapeHtml(ow.username) : ("id=" + u.owner_user_id);
              } else if (u.is_super_admin) {
                ownerLabel = '<span style="color:var(--violet)">Semua OLT</span>';
              }
              return '<tr class="user-tree-row" data-uid="' + u.id + '" style="' + indent + '">' +
                '<td style="text-align:center">' + arrow + '</td>' +
                '<td><b>' + escapeHtml(u.username) + '</b>' + (isSelf ? ' <span style="font-size:10px;color:var(--text-dim)">(Anda)</span>' : '') + childCount + '</td>' +
                '<td>' + escapeHtml(u.full_name || "-") + '</td>' +
                '<td><span class="status-badge info" style="text-transform:capitalize">' + roleLabel + '</span></td>' +
                '<td style="font-size:12px">' + ownerLabel + '</td>' +
                '<td>' + statusBadge + '</td>' +
                '<td style="font-size:12px;color:var(--text-dim)">' + (u.last_login ? new Date(u.last_login).toLocaleString("id-ID", {day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"}) : "Belum pernah") + '</td>' +
                '<td style="text-align:right;white-space:nowrap">' +
                  '<button class="btn-icon" onclick="openUserEdit(' + u.id + ')" title="Edit user"><i class="fas fa-pen"></i></button>' +
                  '<button class="btn-icon" onclick="openUserPasswordReset(' + u.id + ')" title="Reset password"><i class="fas fa-key"></i></button>' +
                  (!isSelf ? '<button class="btn-icon" onclick="deleteUser(' + u.id + ')" title="Hapus user" style="color:var(--red)"><i class="fas fa-trash"></i></button>' : '') +
                '</td>' +
              '</tr>';
            };

            const renderTree = (u, depth) => {
              const subs = subordinates[u.id] || [];
              const hasChildren = subs.length > 0;
              rows.push(renderRow(u, depth, hasChildren));
              subs.forEach(c => renderTree(c, depth + 1));
            };

            topLevel.sort((a, b) => {
              if (a.is_super_admin !== b.is_super_admin) return b.is_super_admin - a.is_super_admin;
              return a.id - b.id;
            });
            topLevel.forEach(u => renderTree(u, 0));

            return rows.join("");
          })()}
        </tbody>
      </table></div>
    `;
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}


// =================== MODAL TAMBAH USER ===================
async function openUserCreate() {
  const isMulti = CURRENT_USER.is_super_admin === 1;

  let owners = [];
  if (isMulti) {
    try {
      const all = await api("/users");
      owners = all.filter(u => u.role === "admin" && !u.is_super_admin);
    } catch(_) {}
  }

  const ownerOptions = '<option value="">— Tidak ada (langsung di bawah saya) —</option>' +
    owners.map(o => '<option value="' + o.id + '">' + escapeHtml(o.username) + ' (' + escapeHtml(o.full_name || "") + ')</option>').join("");

  const roleOptions = isMulti
    ? '<option value="multivers">Super Admin (akses semua OLT)</option>' +
      '<option value="admin">Admin (privilege 15) - kelola OLT sendiri</option>' +
      '<option value="operator">Operator (privilege 10) - provisioning, config</option>' +
      '<option value="field_tech">Field Tech (privilege 8) - reboot, lihat</option>' +
      '<option value="viewer" selected>Viewer (privilege 5) - hanya lihat</option>'
    : '<option value="operator">Operator (privilege 10) - provisioning, config</option>' +
      '<option value="field_tech">Field Tech (privilege 8) - reboot, lihat</option>' +
      '<option value="viewer" selected>Viewer (privilege 5) - hanya lihat</option>';

  const ownerField = isMulti
    ? '<div class="form-group"><label>Atasan / Owner (opsional)</label>' +
      '<select id="u-owner">' + ownerOptions + '</select>' +
      '<div style="font-size:11px;color:var(--text-dim);margin-top:4px">Pilih admin sebagai atasan. Operator/viewer akan share akses OLT dengan admin ini.</div></div>'
    : '<div style="background:var(--surface-2);padding:10px 14px;border-radius:8px;font-size:11px;color:var(--text-dim);margin-top:10px">' +
      '<i class="fas fa-info-circle"></i> User baru otomatis jadi bawahan Anda.</div>';

  openModal("Tambah User Baru",
    '<div class="form-group"><label>Username</label><input id="u-username" placeholder="budi"></div>' +
    '<div class="form-group"><label>Nama Lengkap</label><input id="u-fullname" placeholder="Budi Santoso"></div>' +
    '<div class="form-group"><label>Password</label><input type="password" id="u-password" placeholder="Minimal 6 karakter"></div>' +
    '<div class="form-group"><label>Role</label><select id="u-role">' + roleOptions + '</select>' +
    '<div style="font-size:11px;color:var(--text-dim);margin-top:4px">Role menentukan hak akses. Privilege di-set otomatis.</div></div>' +
    ownerField
  , [
    { label: "Kembali", cls: "btn-secondary", action: closeModal },
    { label: "Simpan", cls: "btn-primary", action: async () => {
      const username = document.getElementById("u-username").value.trim();
      const full_name = document.getElementById("u-fullname").value.trim() || null;
      const password = document.getElementById("u-password").value;
      let role = document.getElementById("u-role").value;
      const ownerRaw = document.getElementById("u-owner") ? document.getElementById("u-owner").value : "";

      let payload = { username, password, full_name, role };
      if (role === "multivers") {
        payload.role = "admin";
        payload.is_super_admin = 1;
      }
      if (isMulti && ownerRaw) {
        payload.owner_user_id = parseInt(ownerRaw, 10);
      }

      if (!username || !password) { toast("Username & password wajib diisi", "error"); return; }
      if (password.length < 6) { toast("Password minimal 6 karakter", "error"); return; }
      try {
        await api("/users", { method: "POST", body: JSON.stringify(payload) });
        toast("User berhasil ditambahkan", "success");
        closeModal(); loadUsers();
      } catch (e) { toast(e.message, "error"); }
    }},
  ]);
}


// =================== MODAL EDIT USER ===================
async function openUserEdit(userId) {
  let u, allUsers;
  try {
    allUsers = await api("/users");
    u = allUsers.find(x => x.id === userId);
    if (!u) { toast("User tidak ditemukan", "error"); return; }
  } catch (e) { toast(e.message, "error"); return; }

  const isSelf = u.username === CURRENT_USER.username;
  const isMulti = CURRENT_USER.is_super_admin === 1;
  const targetIsMulti = u.is_super_admin === 1;

  // Dropdown role — opsi "multivers" cuma untuk requester Super Admin
  const roleOpts = [];
  if (isMulti) {
    roleOpts.push('<option value="multivers" ' + (targetIsMulti ? 'selected' : '') + '>👑 Super Admin (lihat semua OLT)</option>');
  }
  roleOpts.push('<option value="admin" ' + (!targetIsMulti && u.role === "admin" ? 'selected' : '') + '>Admin (15)</option>');
  roleOpts.push('<option value="operator" ' + (!targetIsMulti && u.role === "operator" ? 'selected' : '') + '>Operator (10)</option>');
  roleOpts.push('<option value="field_tech" ' + (!targetIsMulti && u.role === "field_tech" ? 'selected' : '') + '>Field Tech (8)</option>');
  roleOpts.push('<option value="viewer" ' + (!targetIsMulti && u.role === "viewer" ? 'selected' : '') + '>Viewer (5)</option>');

  // Owner dropdown (hanya untuk requester Super Admin + target bukan Super Admin)
  let ownerField = '';
  if (isMulti && !targetIsMulti) {
    const owners = allUsers.filter(x => x.role === "admin" && !x.is_super_admin && x.id !== u.id);
    const ownerOpts = '<option value="">— Tidak ada —</option>' +
      owners.map(o => '<option value="' + o.id + '" ' + (u.owner_user_id === o.id ? 'selected' : '') + '>' + escapeHtml(o.username) + ' (' + escapeHtml(o.full_name || "") + ')</option>').join("");
    ownerField = '<div class="form-group"><label>Atasan / Owner</label>' +
      '<select id="ue-owner">' + ownerOpts + '</select></div>';
  }

  openModal("Edit User — " + u.username,
    '<div class="form-group"><label>Username</label><input value="' + escapeHtml(u.username) + '" disabled style="opacity:0.6"></div>' +
    '<div class="form-group"><label>Nama Lengkap</label><input id="ue-fullname" value="' + escapeHtml(u.full_name || "") + '"></div>' +
    '<div class="form-group"><label>Role</label><select id="ue-role">' + roleOpts.join("") + '</select></div>' +
    ownerField +
    '<div class="form-group"><label>Status Akun</label>' +
      '<select id="ue-active" ' + (isSelf ? 'disabled style="opacity:0.6"' : '') + '>' +
        '<option value="true" ' + (u.is_active ? 'selected' : '') + '>Aktif</option>' +
        '<option value="false" ' + (!u.is_active ? 'selected' : '') + '>Nonaktif (tidak bisa login)</option>' +
      '</select>' +
      (isSelf ? '<div style="font-size:11px;color:var(--yellow);margin-top:4px">Tidak bisa menonaktifkan akun sendiri</div>' : '') +
    '</div>' +
    '<div style="background:var(--bg);padding:10px 14px;border-radius:8px;font-size:11px;color:var(--text-dim);line-height:1.5">' +
      '<i class="fas fa-info-circle"></i> Perubahan role atau status akan <b>menginvalidasi token</b> user tersebut. Mereka harus login ulang.' +
    '</div>'
  , [
    { label: "Batal", cls: "btn-secondary", action: closeModal },
    { label: "Simpan", cls: "btn-primary", action: async () => {
      const full_name = document.getElementById("ue-fullname").value.trim() || null;
      let role = document.getElementById("ue-role").value;
      const is_active = document.getElementById("ue-active").value === "true";
      const ownerEl = document.getElementById("ue-owner");

      let payload = { full_name, role, is_active };
      if (role === "multivers") {
        payload.role = "admin";
        payload.is_super_admin = 1;
      } else if (targetIsMulti) {
        // Downgrade dari Super Admin: kirim is_super_admin=0
        payload.is_super_admin = 0;
      }
      if (ownerEl) {
        const ownerRaw = ownerEl.value;
        payload.owner_user_id = ownerRaw ? parseInt(ownerRaw, 10) : null;
      }

      try {
        await api("/users/" + userId, { method: "PUT", body: JSON.stringify(payload) });
        toast("User berhasil diupdate", "success");
        closeModal(); loadUsers();
      } catch (e) {
        let msg = e.message;
        try { const p = JSON.parse(msg); msg = p.detail || p.message || msg; } catch (_) {}
        toast("Gagal: " + msg, "error");
      }
    }},
  ]);
}


// =================== MODAL RESET PASSWORD ===================
async function openUserPasswordReset(userId) {
  let users;
  try { users = await api("/users"); } catch (e) { toast(e.message, "error"); return; }
  const u = users.find(x => x.id === userId);
  if (!u) { toast("User tidak ditemukan", "error"); return; }

  openModal(`Reset Password — ${u.username}`, `
    <div style="background:rgba(245,158,11,0.08);border-left:3px solid var(--yellow);border-radius:8px;padding:12px 14px;margin-bottom:16px;font-size:12px;color:var(--text-2)">
      <b style="color:var(--yellow)"><i class="fas fa-exclamation-triangle"></i> Peringatan:</b>
      Setelah reset, user <b>${escapeHtml(u.username)}</b> harus login ulang dengan password baru.
    </div>
    <div class="form-group"><label>Password Baru</label>
      <input type="password" id="rp-password" placeholder="Minimal 6 karakter">
    </div>
    <div class="form-group"><label>Konfirmasi Password</label>
      <input type="password" id="rp-password2" placeholder="Ulangi password">
    </div>
  `, [
    { label: "Batal", cls: "btn-secondary", action: closeModal },
    { label: "Reset Password", cls: "btn-primary", action: async () => {
      const p1 = document.getElementById("rp-password").value;
      const p2 = document.getElementById("rp-password2").value;
      if (p1.length < 6) { toast("Password minimal 6 karakter", "error"); return; }
      if (p1 !== p2) { toast("Password tidak sama", "error"); return; }
      try {
        await api(`/users/${userId}/password`, { method: "PUT",
          body: JSON.stringify({ new_password: p1 }) });
        toast(`Password ${u.username} berhasil direset`, "success");
        closeModal();
      } catch (e) { toast(e.message, "error"); }
    }},
  ]);
}


// =================== GANTI PASSWORD SENDIRI ===================
function openSelfPasswordChange() {
  openModal("Ganti Password Saya", `
    <div class="form-group"><label>Password Lama</label>
      <input type="password" id="sp-old" placeholder="Password saat ini">
    </div>
    <div class="form-group"><label>Password Baru</label>
      <input type="password" id="sp-new" placeholder="Minimal 6 karakter">
    </div>
    <div class="form-group"><label>Konfirmasi Password Baru</label>
      <input type="password" id="sp-new2" placeholder="Ulangi password baru">
    </div>
    <div style="background:var(--bg);padding:10px 14px;border-radius:8px;font-size:11px;color:var(--text-dim);line-height:1.5">
      <i class="fas fa-info-circle"></i> Setelah ganti password, Anda akan <b>logout otomatis</b> dan harus login ulang.
    </div>
  `, [
    { label: "Batal", cls: "btn-secondary", action: closeModal },
    { label: "Ganti Password", cls: "btn-primary", action: async () => {
      const oldP = document.getElementById("sp-old").value;
      const newP = document.getElementById("sp-new").value;
      const newP2 = document.getElementById("sp-new2").value;
      if (!oldP) { toast("Password lama wajib diisi", "error"); return; }
      if (newP.length < 6) { toast("Password baru minimal 6 karakter", "error"); return; }
      if (newP !== newP2) { toast("Password baru tidak sama", "error"); return; }
      if (oldP === newP) { toast("Password baru harus beda dari yang lama", "error"); return; }
      try {
        await api("/users/me/password", { method: "PUT",
          body: JSON.stringify({ old_password: oldP, new_password: newP }) });
        toast("Password berhasil diubah. Silakan login ulang...", "success");
        closeModal();
        setTimeout(() => { logout(); }, 1500);
      } catch (e) {
        let msg = e.message;
        try { const p = JSON.parse(msg); msg = p.detail || p.message || msg; } catch (_) {}
        toast(`Gagal: ${msg}`, "error");
      }
    }},
  ]);
}


document.getElementById("btn-add-user")?.addEventListener("click", openUserCreate);

async function deleteUser(id) {
  if (!confirm("Hapus user ini?")) return;
  try {
    await api(`/users/${id}`, { method: "DELETE" });
    toast("User dihapus", "success");
    loadUsers();
  } catch (e) { toast(e.message, "error"); }
}

// =================== MODAL ===================
function openModal(title, bodyHTML, buttons = []) {
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").innerHTML = bodyHTML;
  const footer = document.getElementById("modal-footer");
  footer.innerHTML = "";
  buttons.forEach(b => {
    const btn = document.createElement("button");
    btn.className = b.cls;
    btn.textContent = b.label;
    btn.onclick = b.action;
    footer.appendChild(btn);
  });
  document.getElementById("modal").classList.remove("hidden");
}
function closeModal() {
  document.getElementById("modal").classList.add("hidden");
}
document.getElementById("modal-close").addEventListener("click", closeModal);
document.getElementById("modal").addEventListener("click", (e) => {
  if (e.target.id === "modal") closeModal();
});

// =================== SYNC DARI OLT ===================
// =================== SYNC REMOVED (auto-refresh via scheduler) ===================

// =================== REFRESH ===================
document.getElementById("refresh-btn").addEventListener("click", () => {
  const active = document.querySelector(".sidebar nav a.active")?.dataset.page || "dashboard";
  const btn = document.getElementById("refresh-btn");
  const icon = btn.querySelector("i");
  icon.style.transition = "transform 0.6s";
  icon.style.transform = "rotate(360deg)";
  setTimeout(() => { icon.style.transform = ""; }, 600);
  // ⭐ Invalidate cache uncfg biar dipanggil ulang dari OLT
  uncfgCache = null;
  loadPage(active);
  toast("Data di-refresh", "info");
});

// =================== INIT ===================
async function init() {
  if (TOKEN && CURRENT_USER) {
    try {
      const me = await api("/auth/me");
      CURRENT_USER = { ...CURRENT_USER, ...me };
      localStorage.setItem("user", JSON.stringify(CURRENT_USER));
      await showApp();
    } catch {
      logout();
    }
  }
}
init();

// Resume jobs aktif kalau user refresh
setTimeout(() => resumeActiveJobs(), 1000);


// =================== GLOBAL SEARCH ===================
let _searchCache = null;
let _searchCacheTime = 0;
const SEARCH_CACHE_TTL = 5000;   // 5 detik

async function _getSearchData() {
  const now = Date.now();
  if (_searchCache && (now - _searchCacheTime) < SEARCH_CACHE_TTL) {
    return _searchCache;
  }
  try {
    // Ambil 4 sumber paralel
    const [onus, vlans, pons, ifaces] = await Promise.all([
      api(`/olts/${CURRENT_OLT_ID}/onus`).catch(() => []),
      api(`/olts/${CURRENT_OLT_ID}/vlans`).catch(() => []),
      api(`/olts/${CURRENT_OLT_ID}/pons`).catch(() => []),
      api(`/olts/${CURRENT_OLT_ID}/interfaces`).catch(() => []),
    ]);
    // Tandai tipe untuk rendering
    onus.forEach(o => o._type = "onu");
    vlans.forEach(v => v._type = "vlan");
    pons.forEach(p => p._type = "pon");
    ifaces.forEach(i => i._type = "interface");
    _searchCache = [...onus, ...vlans, ...pons, ...ifaces];
    _searchCacheTime = now;
    return _searchCache;
  } catch (e) {
    return _searchCache || [];
  }
}

function _matches(item, q) {
  const t = item._type || "onu";
  let fields = [];

  if (t === "onu") {
    fields = [
      item.serial_number, item.name, item.pon_port,
      item.interface_name, String(item.vlan || ""),
      String(item.user_vlan || ""), String(item.onu_id || ""),
      item.pppoe_user,
    ];
  } else if (t === "vlan") {
    fields = [
      String(item.vlan_id || ""),
      item.name,
      item.description,
    ];
  } else if (t === "pon") {
    fields = [
      item.port_no,
      item.status,
      item.admin_state,
      String(item.onu_count || ""),
    ];
  } else if (t === "interface") {
    fields = [
      item.name, item.type, item.status,
      item.admin_state, item.switchport_mode,
      item.vlans, item.hybrid_attribute,
    ];
  }
  return fields.some(f => f && String(f).toLowerCase().includes(q));
}

function _renderSearchResults(results, query) {
  const el = document.getElementById("global-search-results");
  if (!el) return;

  if (!query.trim()) {
    el.classList.add("hidden");
    return;
  }

  if (!results.length) {
    el.innerHTML = `<div class="search-result-empty">
      <i class="fas fa-search" style="font-size:20px;opacity:0.5;display:block;margin-bottom:6px"></i>
      Tidak ada hasil untuk "<b>${escapeHtml(query)}</b>"
    </div>`;
    el.classList.remove("hidden");
    return;
  }

  const icons = { online: "fa-check-circle", offline: "fa-times-circle", los: "fa-unlink", unknown: "fa-question-circle" };

  el.innerHTML = `<div class="search-result-section">${results.length} hasil</div>` +
    results.slice(0, 15).map(item => {
      const t = item._type || "onu";

      if (t === "onu") {
        const st = ["online","offline","los"].includes(item.status) ? item.status : "unknown";
        const label = item.name || item.serial_number || `ONU ${item.onu_id}`;
        const sub = `ONU · ${item.serial_number || "-"} · ${item.pon_port}:${item.onu_id}`;
        return `<div class="search-result-item" data-action="onu" data-onu-id="${item.onu_id}">
          <div class="search-result-icon ${st}"><i class="fas ${icons[st]}"></i></div>
          <div class="search-result-body">
            <div class="search-result-title">${escapeHtml(label)}</div>
            <div class="search-result-sub">${escapeHtml(sub)}</div>
          </div>
          <span class="search-result-badge ${st}">${escapeHtml((item.status || "").toUpperCase())}</span>
        </div>`;
      }

      if (t === "vlan") {
        const label = `VLAN ${item.vlan_id}${item.name ? " · " + item.name : ""}`;
        const sub = item.description || "VLAN database";
        return `<div class="search-result-item" data-action="vlan">
          <div class="search-result-icon unknown" style="background:rgba(79,140,255,0.15);color:var(--primary)"><i class="fas fa-layer-group"></i></div>
          <div class="search-result-body">
            <div class="search-result-title">${escapeHtml(label)}</div>
            <div class="search-result-sub">${escapeHtml(sub)}</div>
          </div>
          <span class="search-result-badge info">VLAN</span>
        </div>`;
      }

      if (t === "pon") {
        const st = item.status === "up" ? "online" : "offline";
        return `<div class="search-result-item" data-action="pon">
          <div class="search-result-icon ${st}" style="background:rgba(168,85,247,0.15);color:var(--purple)"><i class="fas fa-broadcast-tower"></i></div>
          <div class="search-result-body">
            <div class="search-result-title">PON ${escapeHtml(item.port_no)}</div>
            <div class="search-result-sub">${item.onu_count || 0} ONU · admin: ${escapeHtml(item.admin_state || "-")}</div>
          </div>
          <span class="search-result-badge ${st}">${escapeHtml((item.status || "").toUpperCase())}</span>
        </div>`;
      }

      if (t === "interface") {
        const st = item.status === "up" ? "online" : "offline";
        return `<div class="search-result-item" data-action="interface">
          <div class="search-result-icon ${st}" style="background:rgba(6,182,212,0.15);color:var(--cyan)"><i class="fas fa-ethernet"></i></div>
          <div class="search-result-body">
            <div class="search-result-title">${escapeHtml(item.name)}</div>
            <div class="search-result-sub">${escapeHtml(item.type || "-")} · mode: ${escapeHtml(item.switchport_mode || "-")} · VLANs: ${escapeHtml(item.vlans || "-")}</div>
          </div>
          <span class="search-result-badge ${st}">${escapeHtml((item.status || "").toUpperCase())}</span>
        </div>`;
      }
      return "";
    }).join("");

  // Handler klik per tipe
  el.querySelectorAll(".search-result-item").forEach(item => {
    item.addEventListener("click", () => {
      const action = item.dataset.action;
      if (action === "onu") {
        _goToONU(parseInt(item.dataset.onuId));
      } else if (action === "vlan") {
        document.querySelector('[data-page="vlans"]')?.click();
        document.getElementById("global-search-results")?.classList.add("hidden");
        document.getElementById("global-search").value = "";
      } else if (action === "pon") {
        document.querySelector('[data-page="pons"]')?.click();
        document.getElementById("global-search-results")?.classList.add("hidden");
        document.getElementById("global-search").value = "";
      } else if (action === "interface") {
        document.querySelector('[data-page="interfaces"]')?.click();
        document.getElementById("global-search-results")?.classList.add("hidden");
        document.getElementById("global-search").value = "";
      }
    });
  });

  el.classList.remove("hidden");
}

function _goToONU(onuId) {
  // Tutup search
  const input = document.getElementById("global-search");
  const results = document.getElementById("global-search-results");
  if (input) input.value = "";
  if (results) results.classList.add("hidden");

  // Pindah ke halaman ONU
  const link = document.querySelector('[data-page="onus"]');
  if (link) link.click();

  // Buka detail ONU setelah halaman loaded
  setTimeout(() => {
    if (typeof showONUDetail === "function") showONUDetail(onuId);
  }, 250);
}

async function _doGlobalSearch(q) {
  const query = (q || "").trim().toLowerCase();
  if (!query) {
    _renderSearchResults([], "");
    return;
  }
  const onus = await _getSearchData();
  const results = onus.filter(o => _matches(o, query));
  _renderSearchResults(results, q);
}

function _initGlobalSearch() {
  const input = document.getElementById("global-search");
  const results = document.getElementById("global-search-results");
  if (!input) return;

  const debouncedSearch = debounce(() => _doGlobalSearch(input.value), 180);
  input.addEventListener("input", debouncedSearch);
  input.addEventListener("focus", () => {
    if (input.value.trim()) _doGlobalSearch(input.value);
  });

  // Tutup dropdown kalau klik di luar
  document.addEventListener("click", (e) => {
    if (!results) return;
    if (!e.target.closest("#topbar-search")) {
      results.classList.add("hidden");
    }
  });

  // Escape untuk tutup
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      input.value = "";
      results.classList.add("hidden");
      input.blur();
    }
  });

  // Shortcut: tekan "/" untuk fokus search (kalau tidak sedang mengetik di input lain)
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
      e.preventDefault();
      input.focus();
    }
  });
}

// Init saat DOM siap
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", _initGlobalSearch);
} else {
  _initGlobalSearch();
}


// =================== FRESHNESS INDICATOR ===================
function updateFreshness(lastPolled) {
  const pill = document.getElementById("freshness-pill");
  const text = document.getElementById("freshness-text");
  if (!pill || !text) return;

  if (!lastPolled) {
    pill.className = "freshness-pill critical";
    text.textContent = "Belum pernah sync";
    return;
  }

  // Parse timestamp — backend kirim tanpa suffix Z kadang
  let ts;
  try {
    ts = new Date(lastPolled.endsWith("Z") ? lastPolled : lastPolled + "Z").getTime();
  } catch (_) {
    ts = new Date(lastPolled).getTime();
  }
  const ageSec = Math.max(0, Math.floor((Date.now() - ts) / 1000));

  let label;
  if (ageSec < 60) label = `${ageSec}s lalu`;
  else if (ageSec < 3600) label = `${Math.floor(ageSec / 60)}m lalu`;
  else if (ageSec < 86400) label = `${Math.floor(ageSec / 3600)}j lalu`;
  else label = `${Math.floor(ageSec / 86400)}d lalu`;

  text.textContent = `Sync ${label}`;

  // Warna berdasarkan umur data (interval polling LAN ~15s, margin 3x)
  const interval = 15;   // asumsi LAN
  if (ageSec <= interval * 3) {
    pill.className = "freshness-pill fresh";
  } else if (ageSec <= interval * 8) {
    pill.className = "freshness-pill stale";
  } else {
    pill.className = "freshness-pill critical";
  }

  // Tandai widget yang datanya stale
  const staleThreshold = interval * 8;
  document.querySelectorAll(".kpi, #hero-card").forEach(el => {
    el.classList.toggle("data-stale", ageSec > staleThreshold);
  });
}

// Auto-update freshness display tiap 3 detik (hitung umur tanpa hit API)
let _lastPolledCache = null;
setInterval(() => {
  if (_lastPolledCache) updateFreshness(_lastPolledCache);
}, 3000);

// Auto-poll freshness dari server tiap 45s (lintas halaman).
// Jalan hanya saat tab visible — hemat request untuk multi-tab.
let _freshnessPollTimer = null;
function _startFreshnessPoll() {
  if (_freshnessPollTimer) return;
  _freshnessPollTimer = setInterval(refreshFreshness, 45000);
}
function _stopFreshnessPoll() {
  if (_freshnessPollTimer) { clearInterval(_freshnessPollTimer); _freshnessPollTimer = null; }
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) _stopFreshnessPoll();
  else { refreshFreshness(); _startFreshnessPoll(); }
});
_startFreshnessPoll();


// =================== SAFETY GUARD — Dialog Konfirmasi Aksi Berbahaya ===================
function confirmDangerous({ title, target, command, warning, confirmText = "Lanjutkan", onConfirm }) {
  const html = `
    <div style="margin-bottom:16px">
      <div style="display:flex;align-items:center;gap:12px;padding:14px;background:rgba(239,68,68,0.08);border-left:3px solid var(--red);border-radius:8px;margin-bottom:16px">
        <i class="fas fa-exclamation-triangle" style="font-size:20px;color:var(--red)"></i>
        <div style="font-size:13px;line-height:1.5">
          <b style="color:var(--red)">Aksi ini mengirim perintah ke OLT</b><br>
          <span style="color:var(--text-2);font-size:12px">Periksa target & command sebelum lanjut.</span>
        </div>
      </div>

      <table style="width:100%;font-size:12px">
        <tr><td style="color:var(--text-dim);width:100px;padding:6px 0">Target</td><td><b>${escapeHtml(target)}</b></td></tr>
        <tr><td style="color:var(--text-dim);padding:6px 0;vertical-align:top">Command</td><td><code style="display:block;padding:8px;background:#0a0f1a;border-radius:6px;color:#4ade80;font-size:11px;white-space:pre-wrap">${escapeHtml(command)}</code></td></tr>
      </table>

      ${warning ? `<div style="margin-top:14px;padding:12px;background:rgba(245,158,11,0.08);border-left:3px solid var(--yellow);border-radius:8px;font-size:12px;color:var(--text-2)">
        <b style="color:var(--yellow)"><i class="fas fa-info-circle"></i> Perhatian:</b> ${escapeHtml(warning)}
      </div>` : ""}
    </div>
  `;

  openModal(title, html, [
    { label: "Batal", cls: "btn-secondary", action: closeModal },
    { label: confirmText, cls: "btn-primary", action: async () => {
      closeModal();
      await onConfirm();
    }},
  ]);
}


// =================== TOGGLE PORT (PON & UPLINK) ===================
async function togglePort(portType, portName, action) {
  const isEnable = action === "enable";
  const ifaceName = portType === "gpon" ? `gpon-olt_${portName}` : portName;

  const cmdPreview = [
    "configure terminal",
    `interface ${ifaceName}`,
    isEnable ? "no shutdown" : "shutdown",
    "exit",
    "end",
    "write",
  ].join("\n");

  const warning = !isEnable
    ? "Port akan dimatikan. SEMUA ONU di port ini akan kehilangan koneksi internet. Pastikan tidak ada pelanggan aktif."
    : null;

  confirmDangerous({
    title: `${isEnable ? "Enable" : "Disable"} Port ${portName}`,
    target: `${portType.toUpperCase()} — ${ifaceName}`,
    command: cmdPreview,
    warning,
    confirmText: isEnable ? "Enable Port" : "Disable Port",
    onConfirm: async () => {
      try {
        await submitJob(`/olts/${CURRENT_OLT_ID}/ports/toggle`, {
          method: "POST",
          label: `${isEnable ? "Enable" : "Disable"} Port ${portName}`,
          subject: ifaceName,
          body: { port_type: portType, port_name: portName, action },
          refresh: () => {
            if (portType === "gpon") loadPONDetail();
            else loadInterfaces();
            loadPONGrid();
            loadUplinkGrid();
          },
        });
      } catch (e) {}
    },
  });
}


// =================== CIRCUIT BREAKER INDICATOR ===================
function updateCircuitPill(circuit) {
  const pill = document.getElementById("circuit-pill");
  const text = document.getElementById("circuit-text");
  if (!pill || !text) return;

  if (!circuit || !circuit.is_open) {
    pill.classList.add("hidden");
    return;
  }

  pill.classList.remove("hidden");
  const remain = circuit.remain_sec || 0;
  const min = Math.ceil(remain / 60);
  text.textContent = `OLT DEGRADED · ${min}m`;
  pill.title = `Circuit breaker OPEN: ${circuit.failures}x gagal berturut-turut. Retry dalam ${remain}s. Error: ${circuit.last_error || "-"}`;
}


// =================== EDIT PORT VLAN (Uplink) ===================
let _editPortData = null;

function openPortEdit(portName, mode, vlans, attribute) {
  _editPortData = {
    port: portName,
    mode: mode || "trunk",
    vlans: vlans || "",
    native_vlan: 1,
    attribute: attribute || "-",
    tag_vlan: vlans || "",
  };

  renderPortEditModal();
}

function renderPortEditModal() {
  const d = _editPortData;
  const html = `
    <div style="background:rgba(79,140,255,0.08);padding:10px 14px;border-radius:8px;border-left:3px solid var(--primary);font-size:12px;color:var(--text-2);margin-bottom:16px">
      <i class="fas fa-info-circle" style="color:var(--primary)"></i>
      Perubahan port VLAN dapat mempengaruhi layanan ONU di port ini.
    </div>

    <div class="form-group">
      <label>Port</label>
      <input id="e-port" value="${escapeHtml(d.port)}" readonly style="opacity:0.7;cursor:not-allowed">
    </div>

    <div class="form-group">
      <label>Mode</label>
      <select id="e-mode">
        <option value="access" ${d.mode === "access" ? "selected" : ""}>Access</option>
        <option value="trunk" ${d.mode === "trunk" ? "selected" : ""}>Trunk</option>
        <option value="hybrid" ${d.mode === "hybrid" ? "selected" : ""}>Hybrid</option>
      </select>
    </div>

    <div class="form-group">
      <label>Native VLAN (untag)</label>
      <input type="number" id="e-native" value="${d.native_vlan || 1}" min="1" max="4094">
      <div style="font-size:11px;color:var(--text-dim);margin-top:4px">VLAN yang dilewatkan tanpa tag (biasanya VLAN manajemen uplink)</div>
    </div>

    <div class="form-group">
      <label>Tag VLAN</label>
      <input id="e-tag" placeholder="Contoh: 1-2,15,101" value="${escapeHtml(d.tag_vlan || "")}">
      <div style="font-size:11px;color:var(--text-dim);margin-top:4px">VLAN yang di-tag (diizinkan lewat port ini). Format: <code>1-10,15,101</code></div>
    </div>

    <div class="form-group">
      <label>Attribute</label>
      <input id="e-attr" value="${escapeHtml(d.attribute)}" readonly style="opacity:0.7;cursor:not-allowed">
    </div>

    <div style="margin-top:16px;padding:12px;background:rgba(245,158,11,0.08);border-left:3px solid var(--yellow);border-radius:8px;font-size:12px;color:var(--text-2)">
      <b style="color:var(--yellow)"><i class="fas fa-exclamation-triangle"></i> Perhatian:</b>
      Port uplink ini dipakai aplikasi untuk akses OLT. Kalau VLAN manajemen dihapus dari allowed list, aplikasi akan kehilangan koneksi.
    </div>
  `;

  openModal(`Edit Port — ${d.port}`, html, [
    { label: "Kembali", cls: "btn-secondary", action: closeModal },
    { label: "Simpan", cls: "btn-primary", action: savePortEdit },
  ]);
}

async function savePortEdit() {
  const port = document.getElementById("e-port").value;
  const mode = document.getElementById("e-mode").value;
  const native = parseInt(document.getElementById("e-native").value);
  const tagVlan = document.getElementById("e-tag").value.trim();

  // Validasi
  if (!mode) { toast("Mode wajib dipilih", "error"); return; }
  if (isNaN(native) || native < 1 || native > 4094) { toast("Native VLAN harus 1-4094", "error"); return; }
  if (!tagVlan) { toast("Tag VLAN wajib diisi", "error"); return; }

  // Disable tombol
  const footer = document.getElementById("modal-footer");
  const btns = footer.querySelectorAll("button");
  const saveBtn = btns[btns.length - 1];
  saveBtn.disabled = true;
  const origHTML = saveBtn.innerHTML;
  saveBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Mengirim ke OLT...';
  btns[0].disabled = true;

  try {
    closeModal();
    await submitJob(`/olts/${CURRENT_OLT_ID}/ports/vlan`, {
      method: "POST",
      label: `Edit Port ${port}`,
      subject: `${port} (mode=${mode})`,
      body: { port_name: port, mode, native_vlan: native, tag_vlans: tagVlan },
      refresh: loadPortVLANTable,
    });
  } catch (e) {
    saveBtn.disabled = false;
    saveBtn.innerHTML = origHTML;
    btns[0].disabled = false;
  }
}


// =================== CONFIG PENDING CHIP ===================
let _lastPendingState = null;

async function updateConfigPill() {
  const pill = document.getElementById("config-pill");
  const text = document.getElementById("config-pill-text");
  if (!pill || !text) return;

  try {
    const state = await api(`/olts/${CURRENT_OLT_ID}/config/pending`);
    _lastPendingState = state;

    if (state.pending) {
      pill.classList.remove("hidden", "saved");
      const age = state.age_sec || 0;
      const label = age < 60 ? `${age}s lalu` : `${Math.floor(age / 60)}m lalu`;
      text.textContent = `Config belum disimpan · ${label}`;
      pill.title = `${state.ops_count} perubahan belum di-commit. Klik untuk commit ke flash.`;
    } else {
      pill.classList.add("hidden");
    }
  } catch (_) {
    pill.classList.add("hidden");
  }
}

function openConfigCommitPanel() {
  if (!_lastPendingState || !_lastPendingState.pending) return;

  const age = _lastPendingState.age_sec || 0;
  const ageLabel = age < 60 ? `${age} detik` : `${Math.floor(age / 60)} menit`;
  const ops = _lastPendingState.ops_count || 0;
  const lastOp = _lastPendingState.last_op || "operasi";

  const html = `
    <div style="padding:14px;background:rgba(245,158,11,0.1);border-left:3px solid var(--yellow);border-radius:8px;margin-bottom:16px">
      <div style="font-size:15px;font-weight:700;color:var(--yellow);margin-bottom:4px">
        <i class="fas fa-exclamation-triangle"></i> Config Belum Permanen
      </div>
      <div style="font-size:12px;color:var(--text-2)">
        Ada <b>${ops} perubahan</b> di running-config yang <b>belum disimpan ke flash</b>.
        Terakhir: <b>${escapeHtml(lastOp)}</b>, ${ageLabel} lalu.
      </div>
    </div>

    <div style="background:var(--bg);padding:12px;border-radius:8px;font-size:12px;color:var(--text-2);line-height:1.6">
      <div><b>Artinya:</b> Perubahan aktif sekarang, tapi kalau OLT reboot → <b>hilang</b>.</div>
      <div style="margin-top:6px"><b>Aman?</b> Sebelum commit, pastikan:</div>
      <ul style="margin:6px 0 0 20px;padding:0">
        <li>Semua perubahan memang disengaja</li>
        <li>Tidak ada teknisi lain yang sedang edit via CLI</li>
        <li>OLT masih bisa diakses normal</li>
      </ul>
    </div>

    <div style="margin-top:14px;font-size:11px;color:var(--text-dim)">
      Kalau ada masalah setelah commit, recovery butuh console fisik ke OLT.
    </div>
  `;

  openModal("💾 Commit Config ke Flash", html, [
    { label: "Nanti Saja", cls: "btn-secondary", action: closeModal },
    { label: "Commit Sekarang", cls: "btn-primary", action: async () => {
      closeModal();
      toast("⏳ Mengirim write ke OLT...", "info");
      try {
        const r = await api(`/olts/${CURRENT_OLT_ID}/config/commit`, { method: "POST" });
        toast("✅ Config tersimpan permanen", "success");
        updateConfigPill();
      } catch (e) {
        let msg = e.message;
        try { const p = JSON.parse(msg); msg = p.message || msg; } catch (_) {}
        toast(`❌ Gagal commit: ${msg}`, "error");
      }
    }},
  ]);
}

// Poll pending state tiap 10 detik
setInterval(() => {
  if (TOKEN) updateConfigPill();
}, 10000);

// Panggil pertama kali
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => setTimeout(updateConfigPill, 500));
} else {
  setTimeout(updateConfigPill, 500);
}


// =================== ONU EVENTS TIMELINE ===================
async function loadONUEvents() {
  const el = document.getElementById("onu-events-body");
  if (!el || !CURRENT_ONU_ID) return;
  try {
    const events = await api(`/olts/${CURRENT_OLT_ID}/onus/${CURRENT_ONU_ID}/events?limit=30`);
    if (!events.length) {
      el.innerHTML = `<div style="text-align:center;color:var(--text-dim);padding:14px;font-size:12px">
        Belum ada event tercatat. Event muncul saat status ONU berubah.
      </div>`;
      return;
    }

    const iconMap = {
      online: { cls: "online", icon: "fa-check-circle" },
      offline: { cls: "offline", icon: "fa-times-circle" },
      los: { cls: "los", icon: "fa-unlink" },
      dying_gasp: { cls: "dying_gasp", icon: "fa-bolt" },
      configuring: { cls: "configuring", icon: "fa-spinner" },
      unknown: { cls: "unknown", icon: "fa-question-circle" },
    };

    el.innerHTML = events.map(ev => {
      const nv = (ev.new_value || "").toLowerCase();
      const meta = iconMap[nv] || iconMap.unknown;
      const time = ev.created_at ? new Date(ev.created_at).toLocaleString("id-ID", {
        day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit"
      }) : "-";
      const age = ev.created_at ? _humanAgo(new Date(ev.created_at).getTime()) : "";
      const transition = `${escapeHtml(ev.old_value || "?")} → ${escapeHtml(ev.new_value || "?")}`;
      return `
        <div style="display:flex;gap:12px;padding:10px 0;border-bottom:1px solid var(--border)">
          <div style="width:28px;height:28px;border-radius:8px;flex-shrink:0;display:flex;align-items:center;justify-content:center;background:var(--bg-3);color:var(--text-dim)">
            <i class="fas ${meta.icon}" style="font-size:12px"></i>
          </div>
          <div style="flex:1;min-width:0">
            <div style="font-size:13px;font-weight:600">${escapeHtml(ev.event_type || "-")}</div>
            <div style="font-size:11px;color:var(--text-dim);font-family:'Monaco',monospace;margin-top:2px">
              ${transition}
              ${ev.detail ? ` · ${escapeHtml(ev.detail)}` : ""}
            </div>
          </div>
          <div style="text-align:right;flex-shrink:0">
            <div style="font-size:11px;color:var(--text-dim)">${time}</div>
            <div style="font-size:10px;color:var(--text-dim);opacity:0.7">${age}</div>
          </div>
        </div>
      `;
    }).join("");
  } catch (e) {
    el.innerHTML = `<div style="text-align:center;color:var(--red);padding:14px;font-size:12px">
      Gagal memuat events: ${escapeHtml(e.message)}
    </div>`;
  }
}

function _humanAgo(ts) {
  const sec = Math.floor((Date.now() - ts) / 1000);
  if (sec < 60) return `${sec}s lalu`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m lalu`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}j lalu`;
  return `${Math.floor(sec / 86400)}d lalu`;
}


// =================== DASHBOARD CARD NAVIGATION ===================
function gotoFiltered(page, filterValue) {
  const link = document.querySelector(`[data-page="${page}"]`);
  if (!link) return;
  link.click();
  // Setelah pindah halaman, set filter
  setTimeout(() => {
    if (page === "onus") {
      const sel = document.getElementById("onu-status-filter");
      if (sel) {
        sel.value = filterValue;
        loadONUs();
      }
    }
  }, 150);
}

function gotoPage(page) {
  const alias = PAGE_ALIAS[page];
  if (alias) { portTab = alias.tab; page = alias.page; }
  const link = document.querySelector(`[data-page="${page}"]`);
  if (link) link.click();
}


function gotoOpticalFilter(filterValue) {
  const sel = document.getElementById("optical-filter");
  if (sel) {
    sel.value = filterValue;
    loadOpticalPage();
    document.getElementById("optical-table")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

// Card "RX Terburuk" diklik: tampilkan SEMUA ONU, urut terburuk dulu
function gotoOpticalWorstRX() {
  const sel = document.getElementById("optical-filter");
  if (sel) sel.value = "";  // reset filter supaya semua tampil
  loadOpticalPage();
  document.getElementById("optical-table")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

// =================== SIDEBAR: COMPACT + DRAWER MOBILE ===================
// Desktop: tombol « = toggle mini/expand
// Mobile (<768px): sidebar jadi drawer off-canvas, tombol « = close drawer
var SIDEBAR_MQ = window.matchMedia("(max-width: 768px)");

function isMobileNav() { return SIDEBAR_MQ.matches; }

function openSidebar() {
  var sb = document.getElementById("sidebar");
  var bd = document.getElementById("sidebar-backdrop");
  var hb = document.getElementById("hamburger");
  if (sb) sb.classList.add("open");
  if (bd) bd.classList.add("show");
  document.body.classList.add("nav-open");
  if (hb) hb.setAttribute("aria-expanded", "true");
}

function closeSidebar() {
  var sb = document.getElementById("sidebar");
  var bd = document.getElementById("sidebar-backdrop");
  var hb = document.getElementById("hamburger");
  if (sb) sb.classList.remove("open");
  if (bd) bd.classList.remove("show");
  document.body.classList.remove("nav-open");
  if (hb) hb.setAttribute("aria-expanded", "false");
}

function toggleSidebarCompact() {
  // Di mobile, tombol « berfungsi sebagai close drawer
  if (isMobileNav()) { closeSidebar(); return; }
  var sb = document.getElementById("sidebar");
  if (!sb) return;
  var isMini = sb.classList.toggle("mini");
  try { localStorage.setItem("sidebar_mini", isMini ? "1" : "0"); } catch (e) {}
  applyDesktopContentMargin();
}

// Desktop: content margin mengikuti state mini
function applyDesktopContentMargin() {
  var sb = document.getElementById("sidebar");
  var content = document.querySelector(".content");
  if (!content) return;
  if (isMobileNav()) {
    content.style.marginLeft = "";
    content.style.maxWidth = "";
    return;
  }
  if (sb && sb.classList.contains("mini")) {
    content.style.marginLeft = "72px";
    content.style.maxWidth = "calc(100vw - 72px)";
  } else {
    content.style.marginLeft = "";
    content.style.maxWidth = "";
  }
}

// Sinkron mode saat load, resize, rotate HP, atau habis login
function applySidebarMode() {
  var sb = document.getElementById("sidebar");
  if (!sb) return;
  if (isMobileNav()) {
    sb.classList.remove("mini");         // drawer selalu full
    closeSidebar();                       // pastikan tertutup
    applyDesktopContentMargin();
  } else {
    closeSidebar();
    var mini = false;
    try { mini = localStorage.getItem("sidebar_mini") === "1"; } catch (e) {}
    sb.classList.toggle("mini", mini);
    applyDesktopContentMargin();
  }
}

// Bind events
if (SIDEBAR_MQ.addEventListener) {
  SIDEBAR_MQ.addEventListener("change", applySidebarMode);
} else if (SIDEBAR_MQ.addListener) {
  SIDEBAR_MQ.addListener(applySidebarMode);
}

document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") closeSidebar();
});

document.addEventListener("DOMContentLoaded", function () {
  applySidebarMode();
  // Tutup drawer setelah user pilih menu
  var sb = document.getElementById("sidebar");
  if (sb) {
    sb.addEventListener("click", function (e) {
      if (isMobileNav() && e.target.closest("a[data-page]")) closeSidebar();
    });
  }
});

// Restore state saat startup sudah dipindah ke applySidebarMode() di atas.
(function restoreSidebarStateNoop() { /* no-op, lihat applySidebarMode */ })();


// =================== THEME TOGGLE (dark / light) ===================
// Note: anti-flash early-apply sudah dipindah ke <head> index.html (inline script)

function chartGridColor() {
  return document.documentElement.dataset.theme === "light"
    ? "rgba(15, 23, 42, 0.06)"
    : "rgba(42, 53, 80, 0.4)";
}

function refreshChartTheme() {
  Object.values(Chart.instances || {}).forEach(c => {
    const y = c.options?.scales?.y;
    if (y?.grid) y.grid.color = chartGridColor();
    c.update("none");
  });
}

function toggleTheme() {
  const cur = document.documentElement.dataset.theme || "dark";
  const next = (cur === "light") ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("theme", next);
  refreshChartTheme();
  _syncThemeIcon(next);
  _refreshChartTheme();
}

function _syncThemeIcon(theme) {
  const i = document.querySelector("#theme-toggle i");
  if (!i) return;
  if (theme === "light") {
    i.className = "fas fa-moon";   // klik berikutnya = balik ke dark
  } else {
    i.className = "fas fa-sun";    // klik berikutnya = ke light
  }
}

// Sinkronkan ikon saat halaman siap
document.addEventListener("DOMContentLoaded", () => {
  _syncThemeIcon(document.documentElement.dataset.theme || "dark");
});

// =================== CHART THEME RE-APPLY ===================
// Canvas tidak baca CSS variables, jadi re-apply warna setelah toggle.
function _refreshChartTheme() {
  if (typeof cpuChart === "undefined" || !cpuChart) return;
  const isLight = document.documentElement.dataset.theme === "light";

  const tickColor  = isLight ? "#475569" : "#8896b3";
  const gridColor  = isLight ? "rgba(15,23,42,0.08)" : "rgba(42,53,80,0.4)";
  const tipBg      = isLight ? "#ffffff" : "#1a2336";
  const tipBorder  = isLight ? "#e2e8f0" : "#2a3550";
  const tipTitle   = isLight ? "#0f172a" : "#eef2ff";
  const tipBody    = isLight ? "#475569" : "#c7d2e8";

  const o = cpuChart.options;
  if (o.plugins?.legend?.labels) o.plugins.legend.labels.color = tickColor;
  if (o.plugins?.tooltip) {
    o.plugins.tooltip.backgroundColor = tipBg;
    o.plugins.tooltip.borderColor     = tipBorder;
    o.plugins.tooltip.titleColor      = tipTitle;
    o.plugins.tooltip.bodyColor       = tipBody;
  }
  ["x","y"].forEach(axis => {
    const sc = o.scales?.[axis];
    if (!sc) return;
    if (sc.ticks) sc.ticks.color = tickColor;
    if (sc.grid)  sc.grid.color  = gridColor;
  });
  cpuChart.update("none");
}

// Apply saat load (chart mungkin belum ada — cek nanti via setTimeout)
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(_refreshChartTheme, 800);
});

/* =====================================================================
   MOBILE BATCH 2+3
   ===================================================================== */
function waOnReady(fn) {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
  else fn();
}

/* ---------- 1. Clipboard yang jalan di HTTP ---------- */
function waLegacyCopy(text) {
  const prev = document.activeElement;
  const host = (prev && prev.closest && prev.closest(".modal-content")) || document.body;
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;padding:0;border:0;opacity:0;font-size:16px;";
  host.appendChild(ta);
  ta.focus({ preventScroll: true });
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) {}
  ta.remove();
  if (prev && prev.focus) prev.focus({ preventScroll: true });
  return ok;
}

window.copyText = function(text) {
  text = String(text == null ? "" : text);
  const cb = navigator.clipboard;
  if (window.isSecureContext && cb && !cb.__polyfill) {
    return cb.writeText(text).then(function() { return true; }, function() { return waLegacyCopy(text); });
  }
  return Promise.resolve(waLegacyCopy(text));
};

if (!navigator.clipboard) {
  try {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        __polyfill: true,
        writeText: function(t) { return waLegacyCopy(String(t == null ? "" : t)) ? Promise.resolve() : Promise.reject(new Error("copy gagal")); },
        readText: function() { return Promise.reject(new Error("readText tidak tersedia di HTTP")); }
      }
    });
  } catch (e) {}
}

/* ---------- 2. Klik di mana saja pada sel = toggle checkbox ---------- */
document.addEventListener("click", function(e) {
  if (e.target.closest("input, a, button, select, textarea, label, summary")) return;
  const cell = e.target.closest(".table-premium td, .table-premium th");
  if (!cell) return;
  const boxes = cell.querySelectorAll("input[type=checkbox]");
  if (boxes.length !== 1) return;
  if (cell.querySelector("button, a, select, textarea, input:not([type=checkbox])")) return;
  const cb = boxes[0];
  if (cb.disabled) return;
  e.stopPropagation();
  cb.click();
}, true);

/* ---------- 3. Chart.js: resize saat canvas jadi terlihat ---------- */
waOnReady(function() {
  function chartOf(c) {
    if (!window.Chart) return null;
    return Chart.getChart ? Chart.getChart(c) : null;
  }
  let raf = 0;
  function resizeVisible() {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(function() {
      document.querySelectorAll("canvas").forEach(function(c) {
        if (c.offsetParent === null) return;
        const ch = chartOf(c);
        if (ch) ch.resize();
      });
    });
  }
  const io = "IntersectionObserver" in window
    ? new IntersectionObserver(function(es) { if (es.some(function(x) { return x.isIntersecting; })) resizeVisible(); })
    : null;
  function watchCanvases() {
    if (io) document.querySelectorAll("canvas").forEach(function(c) { io.observe(c); });
  }
  let mraf = 0;
  const content = document.querySelector(".content");
  if (content) {
    new MutationObserver(function() {
      cancelAnimationFrame(mraf);
      mraf = requestAnimationFrame(watchCanvases);
    }).observe(content, { childList: true, subtree: true });
  }
  watchCanvases();
  document.addEventListener("click", function(e) {
    if (e.target.closest("[data-page]")) {
      setTimeout(resizeVisible, 80);
      setTimeout(resizeVisible, 350);
    }
  });
  window.addEventListener("resize", resizeVisible);
  window.addEventListener("orientationchange", function() { setTimeout(resizeVisible, 200); });
  window.waResizeVisibleCharts = resizeVisible;
});

/* ---------- 4. Enhancer tabel ---------- */
const WA_TABLE_RULES = [
  { page: "onus",    cls: ["t-cards"] },
  { page: "optical", cls: ["sticky-first"] },
  { page: "port",    cls: ["sticky-first"] },
  { page: "vlans",   cls: ["sticky-first"] },
  { page: "users",   cls: ["sticky-first"] },
  { page: "audit",   keep: 3 }
];

function waCleanText(s) { return (s || "").replace(/\s+/g, " ").trim(); }

function waLabelCells(t) {
  const hr = t.tHead && t.tHead.rows[0];
  if (!hr) return;
  const heads = Array.prototype.slice.call(hr.cells).map(function(th) {
    return waCleanText(th.dataset.label || th.textContent);
  });
  Array.prototype.forEach.call(t.tBodies, function(tb) {
    Array.prototype.forEach.call(tb.rows, function(tr) {
      const single = tr.cells.length === 1;
      Array.prototype.forEach.call(tr.cells, function(td, i) {
        const lab = single ? "" : (heads[i] || "");
        if (td.dataset.label !== lab) td.dataset.label = lab;
      });
    });
  });
}

function waMarkSecondaryCols(t, keep) {
  const hr = t.tHead && t.tHead.rows[0];
  if (!hr) return;
  const first = hr.cells[0];
  const skip = first && (first.querySelector("input[type=checkbox]") || !waCleanText(first.textContent)) ? 1 : 0;
  const limit = skip + keep;
  Array.prototype.forEach.call(t.rows, function(tr) {
    if (tr.cells.length === 1) return;
    Array.prototype.forEach.call(tr.cells, function(c, i) {
      c.classList.toggle("col-hide-m", i >= limit);
    });
  });
}

function waEnhanceTablesNew(root) {
  if (!root) return;
  root.querySelectorAll("table").forEach(function(t) {
    if (!t.parentElement.classList.contains("table-wrap")) {
      const w = document.createElement("div");
      w.className = "table-wrap";
      t.parentNode.insertBefore(w, t);
      w.appendChild(t);
    }
  });
  WA_TABLE_RULES.forEach(function(rule) {
    root.querySelectorAll("#page-" + rule.page + " table.table-premium").forEach(function(t) {
      (rule.cls || []).forEach(function(c) { t.classList.add(c); });
      if (rule.keep) waMarkSecondaryCols(t, rule.keep);
    });
  });
  root.querySelectorAll("table.t-cards").forEach(waLabelCells);
}

waOnReady(function() {
  const content = document.querySelector(".content");
  if (!content) return;
  let raf = 0;
  new MutationObserver(function() {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(function() { waEnhanceTablesNew(content); });
  }).observe(content, { childList: true, subtree: true });
  waEnhanceTablesNew(content);
});

/* ---------- 5. Pager Audit Log ---------- */
waOnReady(function() {
  const PAGE = 50;
  const st = { all: [], set: new Set(), shown: PAGE, bar: null };
  function getTable() { return document.querySelector("#page-audit table.table-premium"); }

  function renderBar(t) {
    const total = st.all.length;
    const anchor = t.closest(".table-wrap") || t;
    if (total <= PAGE) { if (st.bar) st.bar.hidden = true; return; }

    if (!st.bar || !st.bar.isConnected || st.bar.previousElementSibling !== anchor) {
      if (st.bar) st.bar.remove();
      st.bar = document.createElement("div");
      st.bar.className = "pager-bar";
      st.bar.innerHTML = '<span class="pager-info"></span><button type="button" class="pager-btn"></button>';
      st.bar.querySelector("button").addEventListener("click", function() { st.shown += PAGE; sync(); });
      anchor.insertAdjacentElement("afterend", st.bar);
    }
    const shown = Math.min(st.shown, total);
    const remain = total - shown;
    const info = st.bar.querySelector(".pager-info");
    const btn = st.bar.querySelector(".pager-btn");
    const infoTxt = "Menampilkan " + shown + " dari " + total + " log";
    const btnTxt = "Muat " + Math.min(PAGE, remain) + " lagi";
    if (info.textContent !== infoTxt) info.textContent = infoTxt;
    if (btn.textContent !== btnTxt) btn.textContent = btnTxt;
    btn.hidden = remain <= 0;
    st.bar.hidden = false;
  }

  function sync() {
    const t = getTable();
    const tb = t && t.tBodies[0];
    if (!tb) return;
    const cur = Array.prototype.slice.call(tb.rows);
    if (!cur.length) { st.all = []; st.set = new Set(); }
    else if (!st.set.has(cur[0])) {
      st.all = cur;
      st.set = new Set(cur);
    }
    const want = st.all.slice(0, st.shown);
    if (want.length !== cur.length || want.some(function(r, i) { return r !== cur[i]; })) {
      tb.replaceChildren.apply(tb, want);
    }
    renderBar(t);
  }

  const host = document.getElementById("page-audit") || document.querySelector(".content");
  if (!host) return;
  let raf = 0;
  new MutationObserver(function() {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(sync);
  }).observe(host, { childList: true, subtree: true });
  sync();

  window.auditPagerReset = function() { st.shown = PAGE; sync(); };
});

/* ---------- 6. Warna latar sticky mengikuti tema ---------- */
waOnReady(function() {
  function syncStickyBg() {
    try {
      const start = document.querySelector(".table-wrap") || document.querySelector(".content");
      const layers = [];
      for (let el = start; el; el = el.parentElement) {
        const m = getComputedStyle(el).backgroundColor.match(/[\d.]+/g);
        if (!m || m.length < 3) continue;
        const a = m.length > 3 ? parseFloat(m[3]) : 1;
        if (a > 0) layers.push([+m[0], +m[1], +m[2], a]);
        if (a >= 0.98) break;
      }
      let r = 255, g = 255, b = 255;
      for (let i = layers.length - 1; i >= 0; i--) {
        const lr = layers[i][0], lg = layers[i][1], lb = layers[i][2], la = layers[i][3];
        r = lr * la + r * (1 - la);
        g = lg * la + g * (1 - la);
        b = lb * la + b * (1 - la);
      }
      document.documentElement.style.setProperty("--table-sticky-bg",
        "rgb(" + Math.round(r) + "," + Math.round(g) + "," + Math.round(b) + ")");
    } catch (e) {}
  }
  syncStickyBg();
  const mo = new MutationObserver(function() { setTimeout(syncStickyBg, 60); });
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-theme"] });
  mo.observe(document.body, { attributes: true, attributeFilter: ["class", "data-theme"] });
  document.addEventListener("click", function(e) {
    if (e.target.closest("[data-page], [id*=theme], [class*=theme]")) setTimeout(syncStickyBg, 120);
  });
  window.syncStickyBg = syncStickyBg;
});

/* ==========================================================================
   PENGATURAN — 4 tab (Umum, Akun, Bantuan, About)
   Menggunakan helper existing: api(), toast(), escapeHtml(), logout()
   ========================================================================== */
(function() {
  'use strict';

  const CFG = {
    appName: 'OLT Manager',
    frontendVersion: '1.0.0',
    supportEmail: 'syafiqkhoirkamal1@gmail.com',
    mailSubject: '[OLT Manager] Bantuan / Laporan masalah',
    developer: 'Developer OLT Manager',
    license: 'Proprietary',
    year: new Date().getFullYear(),
    api: {
      systemInfo: '/system/info',           // backend: /api/v1/system/info
      changePassword: '/users/me/password', // existing
    },
    credits: ['FastAPI', 'Uvicorn', 'SQLAlchemy', 'Pydantic', 'Chart.js'],
  };

  /* ====================================================================
     1. PREFS — local storage (per device)
     ==================================================================== */
  const PREFS_KEY = 'oltmgr_prefs_v1';
  const DEFAULT_PREFS = {
    refreshMs: 10000,     // 5s | 10s | 30s | 0 (off)
    timeFormat: '24h',    // 12h | 24h
    sidebarDefault: 'full', // mini | full
  };

  function loadPrefs() {
    try {
      const raw = localStorage.getItem(PREFS_KEY);
      if (!raw) return { ...DEFAULT_PREFS };
      return { ...DEFAULT_PREFS, ...JSON.parse(raw) };
    } catch (e) { return { ...DEFAULT_PREFS }; }
  }
  function savePrefs(p) {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(p));
      window.dispatchEvent(new CustomEvent('prefs:change', { detail: p }));
    } catch (e) {}
  }

  let _prefs = loadPrefs();

  window.AppPrefs = {
    get: () => ({ ..._prefs }),
    set: (k, v) => { _prefs[k] = v; savePrefs(_prefs); },
    refreshMs: () => _prefs.refreshMs,
    formatTime: (d) => {
      const dt = (d instanceof Date) ? d : new Date(d);
      if (isNaN(dt)) return '-';
      const opts = _prefs.timeFormat === '12h'
        ? { hour: '2-digit', minute: '2-digit', hour12: true }
        : { hour: '2-digit', minute: '2-digit', hour12: false };
      return dt.toLocaleTimeString('id-ID', opts);
    },
    reset: () => { _prefs = { ...DEFAULT_PREFS }; savePrefs(_prefs); },
  };

  /* ====================================================================
     2. TAB SWITCHER
     ==================================================================== */
  function switchSetTab(tab) {
    document.querySelectorAll('#page-settings .set-tabs .alert-tab-btn').forEach(btn => {
      const isActive = btn.getAttribute('data-set-tab') === tab;
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    document.querySelectorAll('#page-settings .set-panel').forEach(p => {
      p.hidden = (p.id !== 'set-panel-' + tab);
    });
    location.hash = '#pengaturan/' + tab;
    if (tab === 'about') loadAboutPanel();
  }

  function bindTabs() {
    document.querySelectorAll('#page-settings .set-tabs .alert-tab-btn').forEach(btn => {
      btn.addEventListener('click', () => switchSetTab(btn.getAttribute('data-set-tab')));
    });
    // Deep link
    const m = location.hash.match(/^#pengaturan\/(\w+)$/);
    if (m && ['umum','akun','bantuan','about'].includes(m[1])) {
      switchSetTab(m[1]);
    }
  }

  /* ====================================================================
     3. TAB UMUM
     ==================================================================== */
  function renderUmum() {
    const pane = document.getElementById('set-panel-umum');
    if (!pane) return;
    const p = _prefs;
    pane.innerHTML = `
      <div class="set-card">
        <h3 class="set-card-title">Perilaku</h3>
        <div class="set-row">
          <div class="set-row-label">
            <div class="set-label">Auto-refresh data</div>
            <div class="set-hint">Interval refresh halaman Traffic & Dashboard</div>
          </div>
          <div class="set-row-control">
            <div class="set-seg" data-pref="refreshMs">
              ${[5000,10000,30000,0].map(v => `
                <button class="set-seg-btn ${p.refreshMs===v?'active':''}" data-val="${v}">
                  ${v===0?'Off':(v/1000)+'s'}
                </button>`).join('')}
            </div>
          </div>
        </div>
        <div class="set-row">
          <div class="set-row-label">
            <div class="set-label">Format waktu</div>
            <div class="set-hint">Cara menampilkan jam di tooltip & log</div>
          </div>
          <div class="set-row-control">
            <div class="set-seg" data-pref="timeFormat">
              <button class="set-seg-btn ${p.timeFormat==='24h'?'active':''}" data-val="24h">24 jam</button>
              <button class="set-seg-btn ${p.timeFormat==='12h'?'active':''}" data-val="12h">12 jam</button>
            </div>
          </div>
        </div>
        <div class="set-row">
          <div class="set-row-label">
            <div class="set-label">Sidebar default (desktop)</div>
            <div class="set-hint">Tampilan awal sidebar saat aplikasi dibuka di PC</div>
          </div>
          <div class="set-row-control">
            <div class="set-seg" data-pref="sidebarDefault">
              <button class="set-seg-btn ${p.sidebarDefault==='full'?'active':''}" data-val="full">Full</button>
              <button class="set-seg-btn ${p.sidebarDefault==='mini'?'active':''}" data-val="mini">Mini</button>
            </div>
          </div>
        </div>
        <div class="set-info">
          <i class="fas fa-info-circle"></i>
          Pengaturan ini tersimpan per perangkat (browser ini saja). Tidak sinkron ke akun.
        </div>
      </div>

      <div class="set-card">
        <h3 class="set-card-title">Reset</h3>
        <div class="set-row">
          <div class="set-row-label">
            <div class="set-label">Kembalikan ke bawaan</div>
            <div class="set-hint">Semua preferensi di tab ini akan direset</div>
          </div>
          <div class="set-row-control">
            <button class="set-btn set-btn-secondary" id="set-reset-prefs">
              <i class="fas fa-undo"></i> Reset
            </button>
          </div>
        </div>
      </div>
    `;

    // Bind segmented buttons
    pane.querySelectorAll('.set-seg').forEach(seg => {
      const prefKey = seg.getAttribute('data-pref');
      seg.querySelectorAll('.set-seg-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          let val = btn.getAttribute('data-val');
          if (prefKey === 'refreshMs') val = parseInt(val, 10);
          AppPrefs.set(prefKey, val);
          seg.querySelectorAll('.set-seg-btn').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          toast('Pengaturan disimpan', 'success');
          // Sidebar default: apply langsung
          if (prefKey === 'sidebarDefault') {
            const sb = document.getElementById('sidebar');
            if (sb && !window.matchMedia('(max-width: 768px)').matches) {
              sb.classList.toggle('mini', val === 'mini');
            }
          }
        });
      });
    });

    // Reset
    const btnReset = document.getElementById('set-reset-prefs');
    if (btnReset) {
      btnReset.addEventListener('click', () => {
        if (!confirm('Kembalikan semua pengaturan ke bawaan?')) return;
        AppPrefs.reset();
        _prefs = AppPrefs.get();
        renderUmum();
        toast('Pengaturan direset ke bawaan', 'success');
      });
    }
  }

  /* ====================================================================
     4. TAB AKUN
     ==================================================================== */
  function renderAkun() {
    const pane = document.getElementById('set-panel-akun');
    if (!pane) return;
    const user = (typeof CURRENT_USER !== 'undefined' && CURRENT_USER) ? CURRENT_USER : {};
    pane.innerHTML = `
      <div class="set-card">
        <h3 class="set-card-title">Info Akun</h3>
        <div class="set-info-row"><span>Username</span><span>${escapeHtml(user.username || '-')}</span></div>
        <div class="set-info-row"><span>Role</span><span>${escapeHtml(user.role || '-')}</span></div>
        <div class="set-info-row"><span>Privilege</span><span>${user.privilege != null ? 'Level ' + user.privilege : '-'}</span></div>
      </div>

      <div class="set-card">
        <h3 class="set-card-title">Ganti Password</h3>
        <div class="set-form-group">
          <label>Password Lama</label>
          <div class="set-pass-wrap">
            <input type="password" id="set-p-old" class="set-input" placeholder="Password saat ini" autocomplete="current-password">
            <button type="button" class="set-pass-toggle" data-toggle="set-p-old" aria-label="Tampilkan"><i class="fas fa-eye"></i></button>
          </div>
        </div>
        <div class="set-form-group">
          <label>Password Baru</label>
          <div class="set-pass-wrap">
            <input type="password" id="set-p-new" class="set-input" placeholder="Minimal 6 karakter" autocomplete="new-password">
            <button type="button" class="set-pass-toggle" data-toggle="set-p-new" aria-label="Tampilkan"><i class="fas fa-eye"></i></button>
          </div>
        </div>
        <div class="set-form-group">
          <label>Konfirmasi Password Baru</label>
          <div class="set-pass-wrap">
            <input type="password" id="set-p-new2" class="set-input" placeholder="Ulangi password baru" autocomplete="new-password">
            <button type="button" class="set-pass-toggle" data-toggle="set-p-new2" aria-label="Tampilkan"><i class="fas fa-eye"></i></button>
          </div>
        </div>
        <div class="set-info" style="background:rgba(251,191,36,.08);border-color:rgba(251,191,36,.25)">
          <i class="fas fa-exclamation-triangle" style="color:#fbbf24"></i>
          Setelah ganti password, Anda akan <b>logout otomatis</b> dan harus login ulang.
        </div>
        <div style="margin-top:14px">
          <button class="set-btn set-btn-primary" id="set-p-submit">
            <i class="fas fa-key"></i> Ganti Password
          </button>
        </div>
      </div>

      <div class="set-card">
        <h3 class="set-card-title">Logout</h3>
        <div class="set-row">
          <div class="set-row-label">
            <div class="set-label">Keluar dari sesi ini</div>
            <div class="set-hint">Anda harus login ulang untuk masuk</div>
          </div>
          <div class="set-row-control">
            <button class="set-btn set-btn-danger" id="set-logout">
              <i class="fas fa-sign-out-alt"></i> Logout
            </button>
          </div>
        </div>
      </div>
    `;

    // Toggle password visibility
    pane.querySelectorAll('.set-pass-toggle').forEach(btn => {
      btn.addEventListener('click', () => {
        const input = document.getElementById(btn.getAttribute('data-toggle'));
        if (!input) return;
        const isPass = input.type === 'password';
        input.type = isPass ? 'text' : 'password';
        btn.querySelector('i').className = isPass ? 'fas fa-eye-slash' : 'fas fa-eye';
      });
    });

    // Submit ganti password
    const btnSubmit = document.getElementById('set-p-submit');
    if (btnSubmit) {
      btnSubmit.addEventListener('click', async () => {
        const oldP = (document.getElementById('set-p-old').value || '').trim();
        const newP = (document.getElementById('set-p-new').value || '').trim();
        const newP2 = (document.getElementById('set-p-new2').value || '').trim();
        if (!oldP) return toast('Password lama wajib diisi', 'error');
        if (newP.length < 6) return toast('Password baru minimal 6 karakter', 'error');
        if (newP !== newP2) return toast('Konfirmasi password tidak sama', 'error');
        if (oldP === newP) return toast('Password baru harus beda dari yang lama', 'error');

        btnSubmit.disabled = true;
        try {
          await api(CFG.api.changePassword, {
            method: 'PUT',
            body: JSON.stringify({ old_password: oldP, new_password: newP }),
          });
          toast('Password berhasil diubah. Login ulang...', 'success');
          setTimeout(() => { if (typeof logout === 'function') logout(); }, 1500);
        } catch (e) {
          let msg = e && e.message ? e.message : 'Error';
          try { const p = JSON.parse(msg); msg = p.detail || p.message || msg; } catch(_) {}
          toast('Gagal: ' + msg, 'error');
          btnSubmit.disabled = false;
        }
      });
    }

    // Logout
    const btnLogout = document.getElementById('set-logout');
    if (btnLogout) {
      btnLogout.addEventListener('click', () => {
        if (confirm('Logout dari sesi ini?')) { if (typeof logout === 'function') logout(); }
      });
    }
  }

  /* ====================================================================
     5. TAB BANTUAN
     ==================================================================== */
  const HELP_TOPICS = [
    {
      q: 'Setup Awal — Menghubungkan OLT',
      a: `<ol>
        <li>Buka menu <b>OLT</b> di sidebar.</li>
        <li>Klik <b>Tambah OLT</b>, isi alamat IP, username, dan password OLT.</li>
        <li>Klik <b>Tes Koneksi</b> untuk memastikan data sudah benar.</li>
        <li>Klik <b>Simpan</b>, tunggu 30–60 detik sampai data pertama masuk.</li>
        <li>Cek di <b>Dashboard</b>: OLT muncul dengan status <i>Online</i>.</li>
      </ol>`
    },
    {
      q: 'Monitoring Harian',
      a: `<p style="margin:0 0 8px"><b>Alur rekomendasi:</b> mulai dari Dashboard → cek Alert → masuk ke halaman detail sesuai kebutuhan.</p>
      <ul>
        <li><b>Dashboard</b> — ringkasan semua OLT, jumlah ONU, alert aktif.</li>
        <li><b>ONU</b> — daftar lengkap semua ONU. Klik baris untuk detail.</li>
        <li><b>Optical</b> — pantau redaman. Aman: <code>-8 s/d -25 dBm</code>. Warning: &lt;-25, Critical: &lt;-28.</li>
        <li><b>Traffic</b> — grafik bandwidth. Bisa dipantau dari 3 sisi: <b>Port Uplink</b> (koneksi ke router/internet), <b>PON</b> (port ke pelanggan FTTH), atau <b>ONU</b> (per pelanggan).</li>
      </ul>`
    },
    {
      q: 'Traffic — Pilih Sisi Mana?',
      a: `<p style="margin:0 0 8px">Halaman <b>Traffic</b> menampilkan grafik bandwidth dari 3 sisi berbeda. Pilih dari dropdown di atas:</p>
      <ul>
        <li><b>Port Uplink</b> — port OLT yang terhubung ke router/switch/internet. Nama port seperti <code>gei_1/3/1</code> atau <code>xgei_1/3/2</code>.
          <ul>
            <li><code>gei</code> = Gigabit Ethernet (1 Gbps)</li>
            <li><code>xgei</code> = 10 Gigabit Ethernet (10 Gbps)</li>
            <li>Angka <code>1/3/1</code> = slot 1, sub-slot 3, port 1</li>
          </ul>
        </li>
        <li><b>PON</b> — port FTTH yang menuju ke pelanggan. Contoh: <code>gpon-olt_1/1/1</code> (port PON 1 di OLT 1).</li>
        <li><b>ONU</b> — per pelanggan. Nama = nama pelanggan yang di-provision. Cocok kalau ada komplain "internet lambat" dari pelanggan tertentu.</li>
      </ul>
      <p style="margin:8px 0 0"><b>Tips:</b></p>
      <ul>
        <li>Kartu dengan badge <b>IDLE</b> = port kosong / tidak ada trafik.</li>
        <li>Kartu dengan badge <b>ACTIVE</b> = sedang ada trafik.</li>
        <li>Klik range waktu (4 jam / 24 jam) untuk lihat pola trafik.</li>
        <li>Untuk traffic per pelanggan, pilih <b>ONU</b> lalu cari nama pelanggan.</li>
      </ul>`
    },
    {
      q: 'Alerts — Apa Artinya?',
      a: `<ul>
        <li><b>Critical</b> — gangguan langsung, segera tindak lanjut.</li>
        <li><b>Warning</b> — potensi masalah, pantau berkala.</li>
        <li><b>Info</b> — catatan sistem, tidak urgent.</li>
      </ul>
      <p><b>Tips:</b> kalau satu PON banyak ONU offline sekaligus, kemungkinan masalah di kabel feeder atau OLT, bukan pelanggan satu-satu.</p>`
    },
    {
      q: 'Provisioning ONU Baru',
      a: `<ol>
        <li>Pasang ONU di pelanggan, nyalakan.</li>
        <li>Buka menu <b>Deployment</b> → <b>GPON Profiles</b> → pastikan profile sudah ada.</li>
        <li>Buka <b>Dashboard</b> bagian <b>ONU Terdeteksi Belum Terdaftar</b>.</li>
        <li>Klik <b>Daftarkan</b> pada ONU yang mau diprovision.</li>
        <li>Pilih <b>GPON Profile</b> dan <b>VLAN</b>, isi nama pelanggan.</li>
        <li>Klik <b>Simpan</b>, tunggu proses selesai.</li>
        <li>Cek di menu <b>ONU</b>: status harus <i>Online</i>, redaman normal.</li>
      </ol>`
    },
    {
      q: 'WhatsApp Bot & Notifikasi',
      a: `<ol>
        <li>Buka menu <b>WhatsApp Bot</b>.</li>
        <li>Tab <b>Setting</b>: pilih provider (Fonnte/Baileys/dll), isi URL gateway, token, nomor target.</li>
        <li>Centang <b>Bot aktif</b>, klik <b>Simpan</b>, lalu <b>Test Kirim</b>.</li>
        <li>Tab <b>Alert Rules</b>: nyalakan <b>Master toggle</b>, lalu centang event yang mau dikirim (ONU LOS, Offline, dll).</li>
        <li>Klik <b>Test Kirim Contoh</b> di card untuk cek.</li>
      </ol>`
    },
    {
      q: 'AI Assistant — Cara Pakai',
      a: `<ul>
        <li>Klik ikon chat di kanan bawah layar.</li>
        <li>Tanya dengan bahasa sehari-hari, misalnya: <i>"Berapa ONU yang LOS hari ini?"</i></li>
        <li>Semakin spesifik pertanyaan, semakin akurat jawabannya.</li>
        <li><b>Selalu periksa</b> hasil penting dari AI sebelum bertindak.</li>
      </ul>`
    },
    {
      q: 'Mengalami Bug atau Butuh Bantuan?',
      a: `<p>Kalau ada masalah atau saran fitur, hubungi developer lewat tombol di bawah. 
      Sertakan <b>screenshot</b> dan langkah-langkah yang Anda lakukan.</p>
      <p>Tombol <b>Hubungi Developer</b> di bawah otomatis mengisi info teknis (versi, halaman, browser) supaya cepat ditangani.</p>`
    },
  ];

  function buildSupportMessage() {
    const user = (typeof CURRENT_USER !== 'undefined' && CURRENT_USER) ? CURRENT_USER : {};
    const subject = CFG.mailSubject + ' - v' + CFG.frontendVersion;
    const lines = [
      'Halo Tim ' + CFG.appName + ',',
      '',
      'Jenis: (pilih) Masalah / Pertanyaan / Saran',
      '',
      'Yang terjadi:',
      '(ceritakan singkat)',
      '',
      'Langkah-langkah:',
      '1.',
      '2.',
      '',
      '--- Info otomatis, mohon jangan dihapus ---',
      'Aplikasi : ' + CFG.appName + ' v' + CFG.frontendVersion,
      'Pengguna : ' + (user.username || '-') + ' (' + (user.role || '-') + ')',
      'Halaman  : ' + (typeof currentPage !== 'undefined' ? currentPage : '-'),
      'Browser  : ' + navigator.userAgent,
      'Layar    : ' + window.innerWidth + 'x' + window.innerHeight,
      'Waktu    : ' + new Date().toLocaleString('id-ID'),
    ];
    return { subject, body: lines.join('\r\n') };
  }

  // Gmail web compose — paling reliable di semua platform
  function buildGmailUrl() {
    const m = buildSupportMessage();
    return 'https://mail.google.com/mail/?view=cm&fs=1' +
      '&to=' + encodeURIComponent(CFG.supportEmail) +
      '&su=' + encodeURIComponent(m.subject) +
      '&body=' + encodeURIComponent(m.body);
  }

  // mailto — fallback untuk email client (Apple Mail, Thunderbird, Gmail app Android)
  function buildMailto() {
    const m = buildSupportMessage();
    return 'mailto:' + CFG.supportEmail +
      '?subject=' + encodeURIComponent(m.subject) +
      '&body=' + encodeURIComponent(m.body);
  }

  // Copy info ke clipboard
  async function copySupportInfo() {
    const m = buildSupportMessage();
    const text = 'Kepada: ' + CFG.supportEmail + '\n' +
                 'Subject: ' + m.subject + '\n\n' +
                 m.body;
    try {
      if (window.copyText) {
        await window.copyText(text);
      } else if (navigator.clipboard) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback lama
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;top:0;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      if (typeof toast === 'function') toast('Info sudah disalin. Tempel di email Anda.', 'success');
    } catch (e) {
      if (typeof toast === 'function') toast('Gagal menyalin: ' + e.message, 'error');
    }
  }

  function openGmail() {
    const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
    if (isMobile) {
      window.location.href = buildMailto();
    } else {
      window.open(buildGmailUrl(), '_blank', 'noopener');
    }
  }

  function openMailto() {
    window.location.href = buildMailto();
  }

  function renderBantuan() {
    const pane = document.getElementById('set-panel-bantuan');
    if (!pane) return;
    pane.innerHTML = `
      <div class="set-card">
        <h3 class="set-card-title">Panduan Pemakaian</h3>
        <div class="set-accordion">
          ${HELP_TOPICS.map((t, i) => `
            <details class="set-acc-item" ${i === 0 ? 'open' : ''}>
              <summary>${escapeHtml(t.q)}</summary>
              <div class="set-acc-body">${t.a}</div>
            </details>
          `).join('')}
        </div>
      </div>

      <div class="set-card">
        <h3 class="set-card-title">Butuh Bantuan?</h3>
        <p style="font-size:13px;color:var(--text-2);line-height:1.6;margin:0 0 12px">
          Kirim email ke developer dengan detail masalah, saran, atau pertanyaan.
          Info teknis (versi, browser, halaman) akan otomatis diisi.
        </p>
        <p style="font-size:12px;color:var(--text-dim);margin:0 0 12px">
          Email: <b>${escapeHtml(CFG.supportEmail)}</b>
        </p>
        <div class="set-btn-row" style="display:flex;flex-wrap:wrap;gap:8px">
          <button class="set-btn set-btn-primary" id="set-contact-gmail" type="button">
            <i class="fab fa-google"></i> Buka Gmail
          </button>
          <button class="set-btn set-btn-secondary" id="set-contact-mailto" type="button">
            <i class="fas fa-envelope"></i> Buka Aplikasi Email
          </button>
          <button class="set-btn set-btn-secondary" id="set-contact-copy" type="button">
            <i class="fas fa-copy"></i> Salin Info
          </button>
        </div>
      </div>
    `;

    // Bind 3 tombol kontak developer
    const btnGmail = document.getElementById('set-contact-gmail');
    if (btnGmail) btnGmail.addEventListener('click', openGmail);

    const btnMailto = document.getElementById('set-contact-mailto');
    if (btnMailto) btnMailto.addEventListener('click', openMailto);

    const btnCopy = document.getElementById('set-contact-copy');
    if (btnCopy) btnCopy.addEventListener('click', copySupportInfo);

    // Hide "Buka Aplikasi Email" di desktop (mailto: sering tidak ada handler)
    // Android/iOS pakai mailto untuk buka Gmail app / Apple Mail — tetap tampil
    const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
    if (!isMobile) {
      const btnMailtoEl = document.getElementById('set-contact-mailto');
      if (btnMailtoEl) btnMailtoEl.style.display = 'none';
    }
  }

  /* ====================================================================
     6. TAB ABOUT
     ==================================================================== */
  let _aboutLoaded = false;
  async function loadAboutPanel() {
    if (_aboutLoaded) return;
    const pane = document.getElementById('set-panel-about');
    if (!pane) return;
    pane.innerHTML = '<div class="set-loading"><i class="fas fa-spinner fa-spin"></i> Memuat info sistem...</div>';
    try {
      const info = await api(CFG.api.systemInfo);
      renderAbout(info);
      _aboutLoaded = true;
    } catch (e) {
      renderAbout({
        backend_version: '?', uptime_seconds: 0,
        disk_used_gb: 0, disk_total_gb: 0, disk_free_gb: 0,
        counts: {}, error: (e && e.message) || 'Gagal memuat',
      });
    }
  }

  function formatUptime(sec) {
    if (!sec) return '-';
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    return m + ' menit';
  }

  function renderAbout(info) {
    const pane = document.getElementById('set-panel-about');
    if (!pane) return;
    const usedPct = info.disk_total_gb
      ? Math.round((info.disk_used_gb / info.disk_total_gb) * 100)
      : 0;
    const diskCls = usedPct > 90 ? 'bad' : usedPct > 75 ? 'warn' : '';

    pane.innerHTML = `
      <div class="set-card">
        <h3 class="set-card-title">Informasi Aplikasi</h3>
        <div class="set-info-row"><span>Aplikasi</span><span>${escapeHtml(CFG.appName)}</span></div>
        <div class="set-info-row"><span>Versi Frontend</span><span>v${escapeHtml(CFG.frontendVersion)}</span></div>
        <div class="set-info-row"><span>Versi Backend</span><span>v${escapeHtml(info.backend_version || '?')}</span></div>
        <div class="set-info-row"><span>Server Uptime</span><span>${formatUptime(info.uptime_seconds)}</span></div>
      </div>

      <div class="set-card">
        <h3 class="set-card-title">Penyimpanan</h3>
        <div class="set-info-row"><span>Disk Terpakai</span><span>${info.disk_used_gb || 0} GB / ${info.disk_total_gb || 0} GB</span></div>
        <div class="set-info-row"><span>Disk Sisa</span><span>${info.disk_free_gb || 0} GB</span></div>
        <div class="set-disk-bar"><div class="set-disk-fill ${diskCls}" style="width:${usedPct}%"></div></div>
        <div style="font-size:11.5px;color:var(--text-dim);margin-top:6px">${usedPct}% terpakai</div>
      </div>

      <div class="set-card">
        <h3 class="set-card-title">Statistik</h3>
        <div class="set-info-row"><span>Total OLT</span><span>${(info.counts && info.counts.olts) || 0}</span></div>
        <div class="set-info-row"><span>Total ONU</span><span>${(info.counts && info.counts.onus) || 0}</span></div>
        <div class="set-info-row"><span>Alert Aktif</span><span>${(info.counts && info.counts.alerts_active) || 0}</span></div>
      </div>

      <div class="set-card">
        <h3 class="set-card-title">Bantuan Cepat</h3>
        <div class="set-row">
          <div class="set-row-label">
            <div class="set-label">Salin info sistem</div>
            <div class="set-hint">Untuk laporan bug ke developer</div>
          </div>
          <div class="set-row-control">
            <button class="set-btn set-btn-secondary" id="set-copy-info">
              <i class="fas fa-copy"></i> Salin
            </button>
          </div>
        </div>
      </div>

      <div class="set-card">
        <h3 class="set-card-title">Kredit & Lisensi</h3>
        <div class="set-credits">
          Dibuat oleh <b>${escapeHtml(CFG.developer)}</b> · ${CFG.year}<br>
          Lisensi: <b>${escapeHtml(CFG.license)}</b><br><br>
          Library: ${CFG.credits.map(c => '<code>' + escapeHtml(c) + '</code>').join(' ')}
        </div>
      </div>
    `;

    const btnCopy = document.getElementById('set-copy-info');
    if (btnCopy) {
      btnCopy.addEventListener('click', async () => {
        const user = (typeof CURRENT_USER !== 'undefined' && CURRENT_USER) ? CURRENT_USER : {};
        const text = [
          '=== Info Sistem ' + CFG.appName + ' ===',
          'Frontend : v' + CFG.frontendVersion,
          'Backend  : v' + (info.backend_version || '?'),
          'Uptime   : ' + formatUptime(info.uptime_seconds),
          'Disk     : ' + (info.disk_used_gb || 0) + '/' + (info.disk_total_gb || 0) + ' GB',
          'User     : ' + (user.username || '-') + ' (' + (user.role || '-') + ')',
          'Browser  : ' + navigator.userAgent,
          'Waktu    : ' + new Date().toLocaleString('id-ID'),
        ].join('\n');
        try {
          if (window.copyText) await window.copyText(text);
          else await navigator.clipboard.writeText(text);
          toast('Info sistem disalin', 'success');
        } catch (e) { toast('Gagal salin', 'error'); }
      });
    }
  }

  /* ====================================================================
     7. INIT PAGE
     ==================================================================== */
  function initSettingsPage() {
    if (!document.getElementById('page-settings')) return;
    bindTabs();
    renderUmum();
    renderAkun();
    renderBantuan();
    // About di-load on-demand saat tab dibuka
  }

  // Hook ke window supaya bisa dipanggil dari loadPage()
  window.initSettingsPage = initSettingsPage;

})();
