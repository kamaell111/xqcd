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

  const signal = pageAbortController.signal;
  const opts = { signal };

  if (page === "dashboard") { loadDashboardStats(); loadPONGrid(); loadRecentAlerts(); loadONUOptical(); drawChart(); loadAlertBadge(); loadUncfg(); }
  if (page === "olts") loadOLTs(opts);
  if (page === "pons") loadPONDetail(opts);
  if (page === "onus") loadONUs(opts);
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

    // CPU
    const cpuEl = document.getElementById("stat-cpu");
    if (s.cpu_usage != null) {
      const cpu = Math.round(s.cpu_usage * 10) / 10;
      cpuEl.textContent = `${cpu}%`;
      const bar = document.getElementById("cpu-bar");
      bar.style.width = Math.min(cpu, 100) + "%";
      bar.classList.toggle("warning", cpu > 70 && cpu <= 85);
      bar.classList.toggle("danger", cpu > 85);

      const trend = document.getElementById("cpu-trend");
      if (lastCpuValue != null) {
        const delta = cpu - lastCpuValue;
        if (Math.abs(delta) > 0.5) {
          trend.textContent = `${delta > 0 ? "+" : ""}${delta.toFixed(1)}%`;
          trend.className = `stat-trend ${delta > 0 ? "down" : ""}`;
        }
      }
      lastCpuValue = cpu;
    } else {
      cpuEl.textContent = "N/A";
    }

    // Memory
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

    // Total ONU (dari DB, sudah real dari sync-pons)
    try {
      const onus = await api(`/olts/${CURRENT_OLT_ID}/onus`);
      const onusEl = document.getElementById("stat-onus");
      const onusSub = document.getElementById("onus-sub");
      if (onusEl) {
        const online = onus.filter(o => o.status === "online").length;
        onusEl.textContent = onus.length;
        if (onusSub) {
          onusSub.textContent = `${online} online · ${onus.length - online} offline`;
          onusSub.style.color = online === onus.length ? "var(--green)" : "var(--yellow)";
        }
      }
    } catch (_) {}

    // Alerts
    const alertCount = s.active_alerts ?? 0;
    document.getElementById("stat-alerts").textContent = alertCount;
    const alertSub = document.getElementById("alert-sub");
    alertSub.textContent = alertCount === 0 ? "Semua aman" : `${alertCount} alert aktif`;
    alertSub.style.color = alertCount === 0 ? "var(--green)" : "var(--yellow)";

    // Hero counters
    try {
      const [pons, onus] = await Promise.all([
        api(`/olts/${CURRENT_OLT_ID}/pons`),
        api(`/olts/${CURRENT_OLT_ID}/onus`),
      ]);
      document.getElementById("hero-pons").textContent = pons.filter(p => p.status === "up").length;
      document.getElementById("hero-onus").textContent = onus.filter(o => o.status === "online").length;
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
    grid.innerHTML = pons.map(p => `
      <div class="pon-cell ${escapeHtml(p.status)}" title="PON ${escapeHtml(p.port_no)} · ${escapeHtml(p.status)} · ${p.onu_count} ONU"
           data-port="${escapeHtml(p.port_no)}">
        <div class="pon-num">${escapeHtml(p.port_no.split("/").pop())}</div>
        <div class="pon-onu">${p.onu_count} ONU</div>
      </div>
    `).join("");
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

async function loadONUOptical() {
  const el = document.getElementById("onu-optical-list");
  if (!el) return;
  try {
    const onus = await api(`/olts/${CURRENT_OLT_ID}/onus`);
    const online = onus.filter(o => o.status === "online" && o.optical_rx != null);
    if (!online.length) {
      el.innerHTML = `<p style="color:var(--text-dim);padding:20px;text-align:center">Tidak ada ONU online</p>`;
      return;
    }
    el.innerHTML = online.slice(0, 6).map(o => {
      const rx = o.optical_rx;
      let cls = "good";
      if (rx < -28) cls = "bad";
      else if (rx < -25) cls = "warn";
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

    // Tampilkan toast untuk setiap recovery baru
    recoveries.forEach(r => {
      const shortTitle = (r.title || "").replace(/ONU\s+/, "").slice(0, 60);
      toast(`✅ ${shortTitle}`, "success");
    });

    // Update timestamp ke yang paling baru
    lastRecoveryTs = Math.max(...recoveries.map(r => r.ts));
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
function renderInternetStatus(o) {
  const onuStatus = (o.status || "unknown").toLowerCase();
  const status = (o.pppoe_status || "unknown").toLowerCase();
  const dur = o.pppoe_online_duration || 0;
  let cls, label, icon, title;

  // ⭐ ATURAN LOGIS: kalau ONU LOS atau OFFLINE → internet PASTI mati
  // (fiber putus / modem mati = tidak mungkin ada internet)
  if (onuStatus === "los" || onuStatus === "offline") {
    cls = "offline";
    label = "DISCONNECTED";
    icon = "fa-times-circle";
    title = onuStatus === "los"
      ? "Fiber putus (LOS) — internet pasti mati"
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
      <table>
        <thead><tr>
          <th>Interface</th><th>Serial Number</th><th>Nama</th><th>ONU</th>
          <th>Internet</th><th>RX (dBm)</th><th>Distance</th><th>VLAN</th><th>PPPoE</th><th>Aksi</th>
        </tr></thead>
        <tbody>
          ${filtered.map(o => `
            <tr>
              <td><b>${escapeHtml(o.interface_name || `${o.pon_port}:${o.onu_id}`)}</b></td>
              <td><code>${escapeHtml(o.serial_number || "-")}</code></td>
              <td>${escapeHtml(o.name || "-")}</td>
              <td><span class="status-badge ${escapeHtml(o.status)}">${escapeHtml(o.status)}</span></td>
              <td>${renderInternetStatus(o)}</td>
              <td>${o.optical_rx != null ? o.optical_rx + " dBm" : "-"}</td>
              <td>${o.distance ? o.distance + " m" : "-"}</td>
              <td>${o.user_vlan || "-"} → ${o.vlan || "-"}</td>
              <td>${escapeHtml(o.pppoe_user || "-")}</td>
              <td>
                <button class="btn-icon" onclick="showONUDetail(${o.onu_id})" title="Detail"><i class="fas fa-eye"></i></button>
                <button class="btn-icon" onclick="rebootONU(${o.onu_id})" title="Reboot"><i class="fas fa-power-off"></i></button>
                <button class="btn-icon" onclick="deleteONU(${o.onu_id})" title="Hapus dari OLT" style="color:var(--red)"><i class="fas fa-trash"></i></button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}

document.getElementById("onu-search")?.addEventListener("input", debounce(loadONUs, 300));
document.getElementById("onu-status-filter")?.addEventListener("change", loadONUs);

async function showONUDetail(onuId) {
  try {
    const o = await api(`/olts/${CURRENT_OLT_ID}/onus/${onuId}`);
    openModal(`ONU · ${o.name || o.serial_number}`, `
      <table>
        <tr><td>Interface</td><td><code>${escapeHtml(o.interface_name || "")}</code></td></tr>
        <tr><td>Serial Number</td><td><code>${escapeHtml(o.serial_number || "")}</code></td></tr>
        <tr><td>Tipe</td><td>${escapeHtml(o.type || "")}</td></tr>
        <tr><td>Status</td><td><span class="status-badge ${escapeHtml(o.status)}">${escapeHtml(o.status)}</span></td></tr>
        <tr><td>Optical TX / RX</td><td>${o.optical_tx ?? "-"} / ${o.optical_rx ?? "-"} dBm</td></tr>
        <tr><td>Distance</td><td>${o.distance ?? "-"} m</td></tr>
        <tr><td>TCONT / GEMPORT</td><td>${escapeHtml(o.tcont || "-")} / ${o.gemport || "-"}</td></tr>
        <tr><td>Service Port</td><td>${o.service_port || "-"}</td></tr>
        <tr><td>VLAN</td><td>${o.user_vlan || "-"} → ${o.vlan || "-"}</td></tr>
        <tr><td>PPPoE User</td><td>${escapeHtml(o.pppoe_user || "-")}</td></tr>
        <tr><td>NAT</td><td>${o.pppoe_nat ? "Enabled" : "Disabled"}</td></tr>
        <tr><td><b>Status Internet</b></td><td>${renderInternetStatus(o)}</td></tr>
        <tr><td>Internet Online Duration</td><td>${o.pppoe_online_duration ? formatUptime(o.pppoe_online_duration) : "-"}</td></tr>
        <tr><td>Internet Dicek</td><td>${o.internet_checked_at ? new Date(o.internet_checked_at).toLocaleString("id-ID") : "-"}</td></tr>
      </table>
    `);
  } catch (e) { toast(e.message, "error"); }
}

async function rebootONU(onuId) {
  if (!confirm("Reboot ONU ini?")) return;
  try {
    await api(`/onu/${CURRENT_OLT_ID}/${onuId}/reboot`, { method: "POST" });
    toast("Perintah reboot terkirim", "success");
  } catch (e) { toast(e.message, "error"); }
}

async function deleteONU(onuId) {
  if (!confirm(`Hapus ONU ID ${onuId} dari OLT?\n\nIni akan menghapus config ONU di OLT dan di database aplikasi.`)) return;
  try {
    const r = await api(`/onu/${CURRENT_OLT_ID}/${onuId}`, { method: "DELETE" });
    if (r.ok) {
      toast("ONU berhasil dihapus dari OLT", "success");
      loadONUs();
      uncfgCache = null;   // invalidate cache biar sync ulang
    } else {
      toast("Gagal hapus ONU", "error");
    }
  } catch (e) { toast(e.message, "error"); }
}

// =================== PROVISION WIZARD ===================
let provisionData = {};
let provisionStep = 0;

document.getElementById("btn-provision-onu")?.addEventListener("click", () => {
  provisionData = {};
  provisionStep = 0;
  openModal("Provision ONU Baru", `<div id="wizard-content"></div>`);
  renderWizard();
});

function renderWizard() {
  const c = document.getElementById("wizard-content");
  if (!c) return;
  const steps = ["Pilih PON", "Data ONU", "Service", "Preview"];
  let html = `<div class="wizard-steps">` +
    steps.map((s, i) => `<div class="wizard-step ${i === provisionStep ? "active" : i < provisionStep ? "done" : ""}">${i + 1}. ${s}</div>`).join("") +
    `</div>`;

  if (provisionStep === 0) {
    html += `
      <div class="form-group"><label>PON Port</label>
        <select id="w-pon">${Array.from({ length: 16 }, (_, i) => `<option value="1/1/${i + 1}">1/1/${i + 1}</option>`).join("")}</select>
      </div>
      <div class="form-group"><label>ONU ID</label><input type="number" id="w-onu-id" value="${provisionData.onu_id || 1}" min="1" max="128"></div>
    `;
  } else if (provisionStep === 1) {
    html += `
      <div class="form-group"><label>Serial Number</label><input id="w-sn" placeholder="YYKC37D4BADA" value="${escapeHtml(provisionData.serial_number || "")}"></div>
      <div class="form-group"><label>Nama ONU</label><input id="w-name" placeholder="CLIENT-001" value="${escapeHtml(provisionData.name || "")}"></div>
      <div class="form-group"><label>Tipe ONU</label>
        <select id="w-type">
          <option>F609</option><option>F601</option><option>F660</option><option>F670L</option>
        </select>
      </div>
    `;
  } else if (provisionStep === 2) {
    html += `
      <div class="form-group"><label>TCONT Profile</label><input id="w-tcont" value="${escapeHtml(provisionData.tcont_profile || "1G")}"></div>
      <div class="form-group"><label>TCONT Name</label><input id="w-tcont-name" value="${escapeHtml(provisionData.tcont_name || "PON1")}"></div>
      <div class="form-group"><label>GEMPORT ID</label><input type="number" id="w-gem" value="${provisionData.gemport_id || 1}"></div>
      <div class="form-group"><label>Traffic Limit</label><input id="w-traffic" value="${escapeHtml(provisionData.traffic_limit || "1G")}"></div>
      <div class="form-group"><label>Service Port</label><input type="number" id="w-sp" value="${provisionData.service_port || 1}"></div>
      <div class="form-group"><label>VPort</label><input type="number" id="w-vport" value="${provisionData.vport || 1}"></div>
      <div class="form-group"><label>User VLAN</label><input type="number" id="w-uvlan" value="${provisionData.user_vlan || 15}"></div>
      <div class="form-group"><label>VLAN</label><input type="number" id="w-vlan" value="${provisionData.vlan || 15}"></div>
      <div class="form-group"><label>PPPoE User (opsional)</label><input id="w-pppoe" value="${escapeHtml(provisionData.pppoe_user || "")}"></div>
      <div class="form-group"><label>PPPoE Password (opsional)</label><input type="password" id="w-pppoe-pass"></div>
    `;
  } else if (provisionStep === 3) {
    html += `<h4 style="margin-bottom:10px;font-size:13px;color:var(--text-dim);text-transform:uppercase;letter-spacing:1px">Preview CLI ZXAN</h4>
      <div class="cli-preview">${escapeHtml(generatePreview())}</div>
      <p style="margin-top:12px;color:var(--text-dim);font-size:12px">
        Periksa konfigurasi sebelum apply.
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
    provisionData.onu_type = document.getElementById("w-type").value;
  } else if (provisionStep === 2) {
    provisionData.tcont_profile = document.getElementById("w-tcont").value;
    provisionData.tcont_name = document.getElementById("w-tcont-name").value;
    provisionData.gemport_id = parseInt(document.getElementById("w-gem").value);
    provisionData.traffic_limit = document.getElementById("w-traffic").value;
    provisionData.service_port = parseInt(document.getElementById("w-sp").value);
    provisionData.vport = parseInt(document.getElementById("w-vport").value);
    provisionData.user_vlan = parseInt(document.getElementById("w-uvlan").value);
    provisionData.vlan = parseInt(document.getElementById("w-vlan").value);
    provisionData.pppoe_user = document.getElementById("w-pppoe").value || null;
    provisionData.pppoe_password = document.getElementById("w-pppoe-pass").value || null;
  }
  return true;
}

function generatePreview() {
  const d = provisionData;
  const iface = `gpon-onu_${d.pon_port}:${d.onu_id}`;
  return `configure terminal
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
  ${d.pppoe_user ? `pppoe 1 nat enable user ${d.pppoe_user} password ${d.pppoe_password || "zte"}` : ""}
  firewall enable level low anti-hack disable
  security-mgmt 1 state enable mode forward protocol web
  wan 1 service internet host 1
exit`;
}

async function doProvision() {
  // === 1. Disable tombol Provision saja ===
  const footer = document.getElementById("modal-footer");
  const buttons = footer.querySelectorAll("button");
  const provisionBtn = buttons[buttons.length - 1];
  provisionBtn.disabled = true;
  provisionBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Mengirim...';

  try {
    // === 2. POST — return INSTAN (<1 detik) ===
    const r = await api("/onu/provision", {
      method: "POST",
      body: JSON.stringify({ olt_id: CURRENT_OLT_ID, ...provisionData }),
    });

    // === 3. Tutup modal LANGSUNG ===
    closeModal();

    // === 4. Tampilkan floating job card ===
    const sn = provisionData.serial_number || "ONU";
    if (r.duplicate) {
      toast(`ℹ️ ONU ${sn} sedang diproses (job sudah jalan)`, "info");
    } else {
      toast(`⚡ Job dimulai: Provision ONU ${sn}`, "info");
    }
    showJobCard(r.job_id, "Provision ONU", sn);

  } catch (e) {
    provisionBtn.disabled = false;
    provisionBtn.innerHTML = '<i class="fas fa-rocket"></i> Provision';
    toast(`❌ Gagal mulai job: ${e.message}`, "error");
  }
}


// =================== FLOATING JOB CARD ===================
const _jobPollers = {};   // {job_id: intervalId}

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
  const ponShort = ponFull.split("/").slice(-1)[0];  // 1
  toast(`Buka form Provision untuk SN ${sn} (PON 1/1/${ponShort}:${onuId})`, "info");
  // Trigger tombol Provision ONU bawaan
  document.querySelector('[data-page="onus"]').click();
  setTimeout(() => {
    const btn = document.getElementById("btn-provision-onu");
    if (btn) btn.click();
    setTimeout(() => {
      const snInput = document.getElementById("w-sn");
      const ponSel = document.getElementById("w-pon");
      const onuInput = document.getElementById("w-onu-id");
      if (snInput) snInput.value = sn;
      if (ponSel) ponSel.value = "1/1/" + ponShort;
      if (onuInput) onuInput.value = onuId;
    }, 100);
  }, 200);
}


function showJobCard(jobId, jobType, subject) {
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
      <table>
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
              <td>
                <button class="btn-icon" onclick="togglePort('uplink', '${escapeHtml(i.name)}', '${i.admin_state === "no shutdown" ? "disable" : "enable"}')" title="${i.admin_state === "no shutdown" ? "Disable port" : "Enable port"}">
                  <i class="fas fa-power-off" style="color:${i.admin_state === "no shutdown" ? "var(--green)" : "var(--text-dim)"}"></i>
                </button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
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
      <table>
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
      </table>
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
      toast(`⏳ Menambahkan VLAN ${vlanId}...`, "info");

      try {
        await api(`/olts/${CURRENT_OLT_ID}/vlans`, {
          method: "POST",
          body: JSON.stringify({
            vlan_id: vlanId,
            name: document.getElementById("v-name").value,
            description: document.getElementById("v-desc").value,
          }),
        });
        toast(`✅ VLAN ${vlanId} berhasil ditambahkan`, "success");
        closeModal();
        loadVLANs();
      } catch (e) {
        toast(`❌ Gagal tambah VLAN: ${e.message}`, "error");
        // Restore tombol biar bisa coba lagi
        saveBtn.innerHTML = origHTML;
        btns.forEach(b => b.disabled = false);
      }
    }},
  ]);
});

async function deleteVLAN(id) {
  if (!confirm(`Hapus VLAN ${id}?\n\nVLAN akan dihapus dari OLT dan database.`)) return;
  toast(`⏳ Menghapus VLAN ${id}...`, "info");
  try {
    await api(`/olts/${CURRENT_OLT_ID}/vlans/${id}`, { method: "DELETE" });
    toast(`✅ VLAN ${id} berhasil dihapus`, "success");
    loadVLANs();
  } catch (e) { toast(`❌ Gagal hapus VLAN: ${e.message}`, "error"); }
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
      <table>
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
      </table>
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
    el.innerHTML = `
      <table>
        <thead><tr><th>Username</th><th>Privilege</th><th>Role</th><th>Aktif</th><th>Last Login</th><th>Aksi</th></tr></thead>
        <tbody>
          ${users.map(u => `
            <tr>
              <td><b>${escapeHtml(u.username)}</b></td>
              <td>${u.privilege}</td>
              <td>${escapeHtml(u.role)}</td>
              <td>${u.is_active ? "✓" : "✗"}</td>
              <td>${u.last_login ? new Date(u.last_login).toLocaleString("id-ID") : "-"}</td>
              <td>${u.username !== CURRENT_USER.username ? `<button class="btn-icon" onclick="deleteUser(${u.id})"><i class="fas fa-trash"></i></button>` : ""}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "error");
  }
}

document.getElementById("btn-add-user")?.addEventListener("click", () => {
  openModal("Tambah User", `
    <div class="form-group"><label>Username</label><input id="u-name"></div>
    <div class="form-group"><label>Password</label><input type="password" id="u-pass"></div>
    <div class="form-group"><label>Privilege (0-15)</label><input type="number" id="u-priv" value="5" min="0" max="15"></div>
    <div class="form-group"><label>Role</label>
      <select id="u-role">
        <option value="admin">Admin</option>
        <option value="operator">Operator</option>
        <option value="viewer">Viewer</option>
        <option value="field_tech">Field Tech</option>
      </select>
    </div>
  `, [
    { label: "Kembali", cls: "btn-secondary", action: closeModal },
    { label: "Simpan", cls: "btn-primary", action: async () => {
      try {
        await api("/users", {
          method: "POST",
          body: JSON.stringify({
            username: document.getElementById("u-name").value,
            password: document.getElementById("u-pass").value,
            privilege: parseInt(document.getElementById("u-priv").value),
            role: document.getElementById("u-role").value,
          }),
        });
        toast("User ditambahkan", "success");
        closeModal(); loadUsers();
      } catch (e) { toast(e.message, "error"); }
    }},
  ]);
});

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
