// =================== STATE ===================
const API = "/api/v1";
let TOKEN = localStorage.getItem("token") || null;
let CURRENT_USER = JSON.parse(localStorage.getItem("user") || "null");
let CURRENT_OLT_ID = 1;
let refreshTimer = null;
let recoveryTimer = null;
let lastRecoveryTs = 0;   // track timestamp recovery terakhir
let cpuChart = null;
let lastCpuValue = null;
let pageAbortController = null;   // untuk batalkan request saat pindah tab
let currentPage = "dashboard";
let uncfgCache = null;   // cache hasil uncfg dari sync-pons
let _inventoryCache = null;   // cache inventory (VLAN, T-CONT, ONU type)

// =================== HTTP ===================
async function api(path, opts = {}) {
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
    CURRENT_USER = res.user;
    localStorage.setItem("token", TOKEN);
    localStorage.setItem("user", JSON.stringify(CURRENT_USER));
    showApp();
  } catch (ex) {
    err.textContent = ex.message;
  } finally {
    btn.disabled = false;
    btn.querySelector("span").textContent = "Masuk";
  }
});

function logout() {
  TOKEN = null;
  CURRENT_USER = null;
  localStorage.removeItem("token");
  localStorage.removeItem("user");
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  if (recoveryTimer) { clearInterval(recoveryTimer); recoveryTimer = null; }
  lastRecoveryTs = 0;
  lastAlertIds = new Set();
  isFirstAlertPoll = true;
  if (pageAbortController) pageAbortController.abort();
  document.getElementById("login-screen").classList.remove("hidden");
  document.getElementById("main-screen").classList.add("hidden");
}

document.getElementById("logout-btn").addEventListener("click", logout);

function showApp() {
  document.getElementById("login-screen").classList.add("hidden");
  document.getElementById("main-screen").classList.remove("hidden");
  document.getElementById("user-name").textContent = CURRENT_USER.username;
  document.getElementById("user-role").textContent = `${CURRENT_USER.role} · priv ${CURRENT_USER.privilege}`;

  if (CURRENT_USER.privilege < 15) {
    const u = document.querySelector('[data-page="users"]');
    if (u) u.style.display = "none";
  }
  if (CURRENT_USER.privilege < 10) {
    const a = document.querySelector('[data-page="audit"]');
    if (a) a.style.display = "none";
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
    } else if (currentPage === "pons") {
      loadPONDetail();
    } else if (currentPage === "vlans") {
      loadVLANs();
    } else if (currentPage === "alerts") {
      loadAlerts();
    } else if (currentPage === "interfaces") {
      loadInterfaces();
    }
  }, 5000);
}

// =================== NAVIGATION ===================
const PAGE_TITLES = {
  dashboard: ["Dashboard", "Ringkasan sistem dan status perangkat"],
  olts: ["OLT Management", "Kelola perangkat OLT terdaftar"],
  pons: ["PON Ports", "Status 16 port GPON"],
  onus: ["ONU Management", "Daftar dan provisioning ONU"],
  optical: ["Optical Power", "Monitoring redaman optik semua ONU"],
  "onu-detail": ["Detail ONU", "Informasi lengkap & troubleshooting"],
  interfaces: ["Interfaces", "Status uplink GEI / XGEI"],
  vlans: ["VLAN", "Manajemen VLAN database"],
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
  currentPage = page;
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

  if (page === "dashboard") { loadDashboardStats(); loadPONGrid(); loadRecentAlerts(); loadONUOptical(); drawChart(); loadAlertBadge(); loadUncfg(); }
  if (page === "olts") loadOLTs(opts);
  if (page === "pons") loadPONDetail(opts);
  if (page === "onus") loadONUs(opts);
  if (page === "optical") loadOpticalPage(opts);
  if (page === "onu-detail") loadONUDetailPage(opts);
  if (page === "interfaces") loadInterfaces(opts);
  if (page === "vlans") loadVLANs(opts);
  if (page === "alerts") loadAlerts(opts);
  if (page === "audit") loadAudit(opts);
  if (page === "users") loadUsers(opts);
}

// =================== DASHBOARD ===================
async function loadDashboardStats() {
  try {
    const s = await api(`/olts/${CURRENT_OLT_ID}/status`);

    // Hero
    document.getElementById("hero-hostname").textContent = s.hostname || "ZXAN";
    document.getElementById("hero-ip").textContent = s.ip_address || "-";
    document.getElementById("hero-firmware").textContent = `${s.model || "C320"} · ${s.firmware || "-"}`;
    document.getElementById("hero-location").textContent = s.location || "-";
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
            grid: { color: "rgba(42, 53, 80, 0.4)", drawTicks: false },
            border: { display: false },
          },
          y: {
            ticks: { color: "#8896b3", font: { size: 10 }, padding: 8 },
            grid: { color: "rgba(42, 53, 80, 0.4)", drawTicks: false },
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
async function loadOLTs(opts = {}) {
  const el = document.getElementById("olt-list");
  try {
    const olts = await api("/olts", opts);
    el.innerHTML = olts.map(o => `
      <div class="widget">
        <div class="widget-header">
          <h3><i class="fas fa-server"></i> ${escapeHtml(o.hostname)} <span class="status-badge ${escapeHtml(o.status)}">${escapeHtml(o.status)}</span></h3>
        </div>
        <table>
          <tr><td>IP Address</td><td><code>${escapeHtml(o.ip_address)}</code></td></tr>
          <tr><td>Model / Firmware</td><td>${escapeHtml(o.model)} / ${escapeHtml(o.firmware)}</td></tr>
          <tr><td>CPU / Memory</td><td>${o.cpu_usage?.toFixed(1) ?? "-"}% / ${o.memory_usage?.toFixed(1) ?? "-"}%</td></tr>
          <tr><td>Uptime</td><td>${formatUptime(o.uptime_seconds)}</td></tr>
          <tr><td>Location</td><td>${escapeHtml(o.location || "-")}</td></tr>
        </table>
      </div>
    `).join("");
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}

// =================== PON ===================
async function loadPONDetail(opts = {}) {
  const el = document.getElementById("pon-detail");
  try {
    const pons = await api(`/olts/${CURRENT_OLT_ID}/pons`, opts);
    el.innerHTML = `
      <div class="widget">
        <div class="widget-header">
          <h3><i class="fas fa-broadcast-tower"></i> 16 PON Ports</h3>
        </div>
        <table>
          <thead><tr><th>Port</th><th>Status</th><th>Admin</th><th>TX (dBm)</th><th>RX (dBm)</th><th>ONU</th><th>Linktrap</th><th>Aksi</th></tr></thead>
          <tbody>
            ${pons.map(p => `
              <tr>
                <td><b>${escapeHtml(p.port_no)}</b></td>
                <td><span class="status-badge ${escapeHtml(p.status)}">${escapeHtml(p.status)}</span></td>
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
          <th>Interface</th><th>Serial Number</th><th>Nama</th><th>Merek</th><th>ONU</th>
          <th>Internet</th><th>RX (dBm)</th><th>Distance</th><th>VLAN</th><th>PPPoE</th><th>Aksi</th>
        </tr></thead>
        <tbody>
          ${filtered.map(o => `
            <tr>
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
                <button class="btn-icon" onclick="showONUDetail(${o.onu_id})" title="Detail"><i class="fas fa-eye"></i></button>
                ${(o.provisioning_mode === "bridge") ? (
                  o.bridge_configured
                    ? `<button class="btn-icon" onclick="markDisconnected(${o.onu_id})" title="Tandai Internet Mati" style="color:var(--yellow)"><i class="fas fa-times-circle"></i></button>`
                    : `<button class="btn-icon" onclick="markConnected(${o.onu_id})" title="Tandai Internet Aktif" style="color:var(--green)"><i class="fas fa-check-circle"></i></button>`
                ) : ""}
                <button class="btn-icon" onclick="rebootONU(${o.onu_id})" title="Reboot"><i class="fas fa-power-off"></i></button>
                <button class="btn-icon" onclick="deleteONU(${o.onu_id})" title="Hapus dari OLT" style="color:var(--red)"><i class="fas fa-trash"></i></button>
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

let CURRENT_ONU_ID = null;

async function showONUDetail(onuId) {
  CURRENT_ONU_ID = onuId;
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
    const o = await api(`/olts/${CURRENT_OLT_ID}/onus/${CURRENT_ONU_ID}`, opts);
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
          <button onclick="rebootONU(${o.onu_id})"><i class="fas fa-power-off"></i> Reboot</button>
          ${o.provisioning_mode === "bridge" ? (
            o.bridge_configured
              ? `<button onclick="markDisconnected(${o.onu_id})"><i class="fas fa-times-circle"></i> Tandai Disconnected</button>`
              : `<button onclick="markConnected(${o.onu_id})"><i class="fas fa-check-circle"></i> Tandai Connected</button>`
          ) : ""}
          <button onclick="loadONUDetailPage()"><i class="fas fa-sync"></i> Refresh</button>
          <button class="danger" onclick="deleteONU(${o.onu_id}).then(() => loadPage('onus'))"><i class="fas fa-trash"></i> Hapus</button>
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

async function markConnected(onuId) {
  if (!confirm("Tandai ONU ini sebagai CONNECTED? (user sudah set PPPoE di GUI modem)")) return;
  try {
    await api(`/onu/${CURRENT_OLT_ID}/${onuId}/mark-connected`, { method: "POST" });
    toast("✅ Status: CONNECTED", "success");
    loadONUs();
  } catch (e) { toast(e.message, "error"); }
}

async function markDisconnected(onuId) {
  if (!confirm("Tandai ONU ini sebagai BELUM SETUP?")) return;
  try {
    await api(`/onu/${CURRENT_OLT_ID}/${onuId}/mark-disconnected`, { method: "POST" });
    toast("Status: BELUM SETUP", "warning");
    loadONUs();
  } catch (e) { toast(e.message, "error"); }
}

async function rebootONU(onuId) {
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
        await api(`/onu/${CURRENT_OLT_ID}/${onuId}/reboot`, { method: "POST" });
        toast("Perintah reboot terkirim", "success");
        loadONUs();
      } catch (e) { toast(e.message, "error"); }
    },
  });
}

async function deleteONU(onuId) {
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
      await submitJob(`/onu/${CURRENT_OLT_ID}/${onuId}`, {
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

    const tcontProfiles = (inv.tcont_profiles && inv.tcont_profiles.length) ? inv.tcont_profiles : ["1G","100M","50M","20M"];
    const tcontOpts = tcontProfiles.map(t => `<option ${provisionData.tcont_profile === t ? "selected" : ""}>${t}</option>`).join("");
    const trafficOpts = tcontProfiles.map(t => `<option ${provisionData.traffic_limit === t ? "selected" : ""}>${t}</option>`).join("");

    const vlans = inv.vlans || [];
    const vlanOpts = vlans.length
      ? vlans.map(v => `<option value="${v.id}" ${provisionData.user_vlan === v.id ? "selected" : ""}>${v.id} - ${v.name}</option>`).join("")
      : `<option value="15">15 (default)</option>`;

    html += `
      ${pppoeInfo}
      <div class="form-group"><label>T-CONT Profile</label>
        <select id="w-tcont">${tcontOpts}</select>
      </div>
      <div class="form-group"><label>T-CONT Name</label><input id="w-tcont-name" value="${escapeHtml(provisionData.tcont_name || "PON1")}"></div>
      <div class="form-group"><label>GEMPORT ID</label><input type="number" id="w-gem" value="${provisionData.gemport_id || 1}" min="1" max="32"></div>
      <div class="form-group"><label>Traffic Limit</label>
        <select id="w-traffic">${trafficOpts}</select>
      </div>
      <div class="form-group"><label>Service Port</label><input type="number" id="w-sp" value="${provisionData.service_port || 1}" min="1"></div>
      <div class="form-group"><label>VPort</label><input type="number" id="w-vport" value="${provisionData.vport || 1}"></div>
      <div class="form-group"><label>User VLAN</label>
        <select id="w-uvlan">${vlanOpts}</select>
      </div>
      <div class="form-group"><label>VLAN</label>
        <select id="w-vlan">${vlanOpts}</select>
      </div>
      <div class="form-group" style="${pppoeStyle}"><label>PPPoE User ${routedOk ? "(opsional)" : "(tidak tersedia)"}</label><input id="w-pppoe" ${pppoeDisabled} value="${escapeHtml(provisionData.pppoe_user || "")}"></div>
      <div class="form-group" style="${pppoeStyle}"><label>PPPoE Password</label><input type="password" id="w-pppoe-pass" ${pppoeDisabled}></div>
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

async function loadWizardInventory() {
  try {
    _inventoryCache = await api(`/onu/inventory?olt_id=${CURRENT_OLT_ID}`);
  } catch (e) {
    console.warn("Inventory fetch failed:", e);
    _inventoryCache = {};
  }
}

async function openSNPicker() {
  let data;
  try {
    data = await api("/onu/uncfg");
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
async function loadInterfaces(opts = {}) {
  const el = document.getElementById("iface-table");
  try {
    const ifaces = await api(`/olts/${CURRENT_OLT_ID}/interfaces`, opts);
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
                <button class="btn-icon" onclick="openPortEdit('${escapeHtml(i.name)}', '${escapeHtml(i.switchport_mode || "")}', ${i.vlans ? `'${escapeHtml(i.vlans)}'` : "null"}, ${i.hybrid_attribute ? `'${escapeHtml(i.hybrid_attribute)}'` : "null"})" title="Edit port VLAN">
                  <i class="fas fa-pen"></i>
                </button>
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
          <th>Username</th><th>Nama Lengkap</th><th>Role</th><th>Privilege</th>
          <th>Status</th><th>Last Login</th><th style="text-align:right">Aksi</th>
        </tr></thead>
        <tbody>
          ${users.map(u => {
            const isSelf = u.username === CURRENT_USER.username;
            const statusBadge = u.is_active
              ? '<span class="status-badge online">Aktif</span>'
              : '<span class="status-badge offline">Nonaktif</span>';
            return `<tr>
              <td><b>${escapeHtml(u.username)}</b>${isSelf ? ' <span style="font-size:10px;color:var(--text-dim)">(Anda)</span>' : ''}</td>
              <td>${escapeHtml(u.full_name || "-")}</td>
              <td><span class="status-badge info" style="text-transform:capitalize">${escapeHtml(u.role)}</span></td>
              <td>${u.privilege}</td>
              <td>${statusBadge}</td>
              <td style="font-size:12px;color:var(--text-dim)">${u.last_login ? new Date(u.last_login).toLocaleString("id-ID", {day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"}) : "Belum pernah"}</td>
              <td style="text-align:right;white-space:nowrap">
                <button class="btn-icon" onclick="openUserEdit(${u.id})" title="Edit user">
                  <i class="fas fa-pen"></i>
                </button>
                <button class="btn-icon" onclick="openUserPasswordReset(${u.id})" title="Reset password">
                  <i class="fas fa-key"></i>
                </button>
                ${!isSelf ? `<button class="btn-icon" onclick="deleteUser(${u.id})" title="Hapus user" style="color:var(--red)">
                  <i class="fas fa-trash"></i>
                </button>` : ""}
              </td>
            </tr>`;
          }).join("")}
        </tbody>
      </table></div>
    `;
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}


// =================== MODAL TAMBAH USER ===================
function openUserCreate() {
  openModal("Tambah User Baru", `
    <div class="form-group"><label>Username</label><input id="u-username" placeholder="budi"></div>
    <div class="form-group"><label>Nama Lengkap</label><input id="u-fullname" placeholder="Budi Santoso"></div>
    <div class="form-group"><label>Password</label><input type="password" id="u-password" placeholder="Minimal 6 karakter"></div>
    <div class="form-group"><label>Role</label>
      <select id="u-role">
        <option value="admin">Admin (privilege 15) — akses penuh</option>
        <option value="operator">Operator (privilege 10) — provisioning, config</option>
        <option value="field_tech">Field Tech (privilege 8) — reboot, lihat</option>
        <option value="viewer" selected>Viewer (privilege 5) — hanya lihat</option>
      </select>
      <div style="font-size:11px;color:var(--text-dim);margin-top:4px">Role menentukan hak akses. Privilege di-set otomatis.</div>
    </div>
  `, [
    { label: "Kembali", cls: "btn-secondary", action: closeModal },
    { label: "Simpan", cls: "btn-primary", action: async () => {
      const username = document.getElementById("u-username").value.trim();
      const full_name = document.getElementById("u-fullname").value.trim() || null;
      const password = document.getElementById("u-password").value;
      const role = document.getElementById("u-role").value;
      if (!username || !password) { toast("Username & password wajib diisi", "error"); return; }
      if (password.length < 6) { toast("Password minimal 6 karakter", "error"); return; }
      try {
        await api("/users", { method: "POST",
          body: JSON.stringify({ username, password, full_name, role }) });
        toast("User berhasil ditambahkan", "success");
        closeModal(); loadUsers();
      } catch (e) { toast(e.message, "error"); }
    }},
  ]);
}


// =================== MODAL EDIT USER ===================
async function openUserEdit(userId) {
  let u;
  try {
    const users = await api("/users");
    u = users.find(x => x.id === userId);
    if (!u) { toast("User tidak ditemukan", "error"); return; }
  } catch (e) { toast(e.message, "error"); return; }

  const isSelf = u.username === CURRENT_USER.username;
  openModal(`Edit User — ${u.username}`, `
    <div class="form-group"><label>Username</label><input value="${escapeHtml(u.username)}" disabled style="opacity:0.6"></div>
    <div class="form-group"><label>Nama Lengkap</label><input id="ue-fullname" value="${escapeHtml(u.full_name || "")}"></div>
    <div class="form-group"><label>Role</label>
      <select id="ue-role">
        <option value="admin" ${u.role === "admin" ? "selected" : ""}>Admin (15)</option>
        <option value="operator" ${u.role === "operator" ? "selected" : ""}>Operator (10)</option>
        <option value="field_tech" ${u.role === "field_tech" ? "selected" : ""}>Field Tech (8)</option>
        <option value="viewer" ${u.role === "viewer" ? "selected" : ""}>Viewer (5)</option>
      </select>
    </div>
    <div class="form-group">
      <label>Status Akun</label>
      <select id="ue-active" ${isSelf ? 'disabled style="opacity:0.6"' : ''}>
        <option value="true" ${u.is_active ? "selected" : ""}>Aktif</option>
        <option value="false" ${!u.is_active ? "selected" : ""}>Nonaktif (tidak bisa login)</option>
      </select>
      ${isSelf ? '<div style="font-size:11px;color:var(--yellow);margin-top:4px">Tidak bisa menonaktifkan akun sendiri</div>' : ''}
    </div>
    <div style="background:var(--bg);padding:10px 14px;border-radius:8px;font-size:11px;color:var(--text-dim);line-height:1.5">
      <i class="fas fa-info-circle"></i> Perubahan role atau status akan <b>menginvalidasi token</b> user tersebut. Mereka harus login ulang.
    </div>
  `, [
    { label: "Batal", cls: "btn-secondary", action: closeModal },
    { label: "Simpan", cls: "btn-primary", action: async () => {
      const full_name = document.getElementById("ue-fullname").value.trim() || null;
      const role = document.getElementById("ue-role").value;
      const is_active = document.getElementById("ue-active").value === "true";
      try {
        await api(`/users/${userId}`, { method: "PUT",
          body: JSON.stringify({ full_name, role, is_active }) });
        toast("User berhasil diupdate", "success");
        closeModal(); loadUsers();
      } catch (e) {
        let msg = e.message;
        try { const p = JSON.parse(msg); msg = p.detail || p.message || msg; } catch (_) {}
        toast(`Gagal: ${msg}`, "error");
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
      showApp();
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
  document.querySelectorAll(".widget.stat-card, #hero-card").forEach(el => {
    el.classList.toggle("data-stale", ageSec > staleThreshold);
  });
}

// Auto-update freshness display tiap 3 detik (hitung umur tanpa hit API)
let _lastPolledCache = null;
setInterval(() => {
  if (_lastPolledCache) updateFreshness(_lastPolledCache);
}, 3000);


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
      refresh: loadInterfaces,
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

// =================== SIDEBAR COMPACT MODE ===================
function toggleSidebarCompact() {
  const sb = document.getElementById("sidebar");
  if (!sb) return;
  const isMini = sb.classList.toggle("mini");
  localStorage.setItem("sidebar_mini", isMini ? "1" : "0");

  // Content margin — adjust manual (biar nggak nunggu CSS sibling)
  const content = document.querySelector(".content");
  if (content) {
    if (isMini) {
      content.style.marginLeft = "72px";
      content.style.maxWidth = "calc(100vw - 72px)";
    } else {
      content.style.marginLeft = "";
      content.style.maxWidth = "";
    }
  }
}

// Restore state saat startup
(function restoreSidebarState() {
  function apply() {
    if (localStorage.getItem("sidebar_mini") === "1") {
      const sb = document.getElementById("sidebar");
      const content = document.querySelector(".content");
      if (sb) sb.classList.add("mini");
      if (content) {
        content.style.marginLeft = "72px";
        content.style.maxWidth = "calc(100vw - 72px)";
      }
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", apply);
  } else {
    apply();
  }
})();


// =================== THEME TOGGLE (dark / light) ===================
// Note: anti-flash early-apply sudah dipindah ke <head> index.html (inline script)

function toggleTheme() {
  const cur = document.documentElement.dataset.theme || "dark";
  const next = (cur === "light") ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("theme", next);
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
