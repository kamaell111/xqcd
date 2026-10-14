/* vlanif.js v2 -- GANTI seluruh blok vlanif.js sebelumnya di app.js dengan ini. Tab VLANIF: daftar + Add + Edit (Delete BELUM ada).
   Memakai: api, openModal, closeModal, submitJob, escapeHtml, CURRENT_OLT_ID. Baris ADAPT bergantung pada helper yang belum kulihat. */

let VLANIF_ITEMS = [];
let VLANIF_VLAN_LIST = [];   // cache list VLAN untuk dropdown
let VLANIF_META = { write_enabled: false, dhcp_allowed: false };

async function loadVlanifTable(opts = {}) {
  const msg = document.getElementById("vlanif-msg");
  if (!document.getElementById("vlanif-table") || !msg) return;
  if (!CURRENT_OLT_ID) { VLANIF_ITEMS = []; msg.textContent = "Pilih OLT terlebih dahulu."; renderVlanifTable(); return; }
  const firstLoad = !VLANIF_ITEMS.length && !msg.textContent;
  if (firstLoad || msg.className === "vlanif-error") {
    msg.className = "muted"; msg.textContent = "Membaca OLT…";
  }
  try {
    const [r, vlans] = await Promise.all([
      api(`/olts/${CURRENT_OLT_ID}/vlanif`, opts),
      VLANIF_VLAN_LIST.length ? Promise.resolve(VLANIF_VLAN_LIST) : api(`/olts/${CURRENT_OLT_ID}/vlans`, opts)
    ]);
    VLANIF_ITEMS = Array.isArray(r.items) ? r.items : [];
    if (Array.isArray(vlans)) VLANIF_VLAN_LIST = vlans;
    VLANIF_META = { write_enabled: !!r.write_enabled, dhcp_allowed: !!r.dhcp_allowed };
    msg.className = r.ok ? "muted" : "vlanif-error";
    msg.textContent = r.ok ? "" : (r.error || "Gagal membaca VLANIF");
  } catch (e) {
    if (e && e.name === "AbortError") return;
    VLANIF_ITEMS = [];
    msg.className = "vlanif-error";
    msg.textContent = "Gagal memuat VLANIF: " + (e.message || e);
  }
  renderVlanifTable();
  renderVlanifPending();
}

function renderVlanifTable() {
  const box = document.getElementById("vlanif-table");
  if (!box) return;
  const q = (document.getElementById("vlanif-search")?.value || "").trim().toLowerCase().replace(/^vlan\s*/, "");
  const rows = VLANIF_ITEMS.filter(it => !q || String(it.vlan_id).includes(q) || (it.ip_address || "").includes(q));
  if (!rows.length) {
    box.innerHTML = `<div class="muted">${VLANIF_ITEMS.length ? "Tidak ada yang cocok dengan pencarian." : "Belum ada VLANIF di OLT ini."}</div>`;
    return;
  }
  const label = { up: "Up", down: "Down", admin_down: "Admin down" };
  const body = rows.map(it => {
    const action = it.protected
      ? `<span class="vlanif-lock" title="${escapeHtml(it.protected_reason)}"><i class="fas fa-lock"></i> Terlindungi</span>`
      : `<button class="vlanif-edit-btn" data-vlan="${escapeHtml(it.vlan_id)}">Edit</button>
        <button type="button" class="vlanif-del-btn" onclick="vlanifDelete(${Number(it.vlan_id)}, '${escapeHtml(it.ip_address || "")}')">Hapus</button>`;
    return `<tr>
      <td>${escapeHtml(it.vlan_id)}</td>
      <td>${it.ip_address ? escapeHtml(it.ip_address) : "—"}</td>
      <td>${it.mask ? escapeHtml(it.mask) : "—"}</td>
      <td><span class="vlanif-status vlanif-${escapeHtml(it.status)}">${escapeHtml(label[it.status] || it.status)}</span></td>
      <td>${it.description ? escapeHtml(it.description) : "—"}</td>
      <td>${action}</td></tr>`;
  }).join("");
  box.innerHTML = `
    <div class="table-wrap"><table class="table-premium">
      <thead><tr><th>VLAN ID</th><th>IP Address</th><th>Mask</th><th>Status</th><th>Description</th><th>Aksi</th></tr></thead>
      <tbody>${body}</tbody>
    </table></div>`;
  box.querySelectorAll(".vlanif-edit-btn").forEach(b => b.addEventListener("click", () =>
    openVlanifModal(VLANIF_ITEMS.find(x => String(x.vlan_id) === b.dataset.vlan))));
}

// ---------- Tahap 1: form ----------
function openVlanifModal(item) {
  const edit = !!item;
  openModal(edit ? `Edit VLANIF ${item.vlan_id}` : "Tambah VLANIF",
    `<div class="form-group">
       <label>VLAN ID</label>
       ${edit
         ? `<input id="vf-vlan" type="text" readonly value="${item.vlan_id}">`
         : `<select id="vf-vlan">${VLANIF_VLAN_LIST.length
             ? VLANIF_VLAN_LIST.map(v => `<option value="${v.vlan_id}">${v.vlan_id} - ${escapeHtml(v.name || "VLAN" + v.vlan_id)}</option>`).join("")
             : '<option value="">(belum ada VLAN — buat dulu di tab Daftar VLAN)</option>'}</select>`}
     </div>
     <div class="form-group">
       <label>Mode IP</label>
       <select id="vf-mode">
         <option value="static">Static</option>
         ${VLANIF_META.dhcp_allowed ? '<option value="dhcp">DHCP</option>' : ""}
       </select>
     </div>
     <div class="form-group">
       <label>IPv4 Address</label>
       <input id="vf-ip" placeholder="192.168.30.2">
     </div>
     <div class="form-group">
       <label>Subnet Mask</label>
       <input id="vf-mask" placeholder="255.255.255.0">
     </div>
     <div class="form-group">
       <label>Description</label>
       <input id="vf-desc" maxlength="100" placeholder="Opsional, maks 100 karakter">
     </div>
     ${VLANIF_META.write_enabled ? "" : '<p class="warn">Mode <b>dry-run</b>: tulis ke OLT dimatikan di server. Kamu tetap bisa preview.</p>'}
     <div id="vf-msg" class="muted"></div>`,
    [{ label: "Batal", cls: "btn", action: closeModal },
     { label: "Preview", cls: "btn primary", action: () => vlanifPreview(edit ? "edit" : "create") }]);
  if (edit) {
    document.getElementById("vf-ip").value = item.ip_address || "";
    document.getElementById("vf-mask").value = item.mask || "";
    document.getElementById("vf-desc").value = item.description || "";
  }
  const sync = () => {
    const dhcp = document.getElementById("vf-mode").value === "dhcp";
    document.getElementById("vf-ip").disabled = dhcp; document.getElementById("vf-mask").disabled = dhcp;
  };
  document.getElementById("vf-mode").addEventListener("change", sync);
}

function vlanifPayload(action) {
  const mode = document.getElementById("vf-mode").value;
  return { action, vlan: Number(document.getElementById("vf-vlan").value), mode,
           ip: mode === "static" ? document.getElementById("vf-ip").value.trim() : null,
           mask: mode === "static" ? document.getElementById("vf-mask").value.trim() : null,
           description: document.getElementById("vf-desc").value.trim() };
}

async function vlanifPreview(action) {
  const msg = document.getElementById("vf-msg");
  const payload = vlanifPayload(action);
  if (!Number.isInteger(payload.vlan) || payload.vlan < 1 || payload.vlan > 4094) { msg.textContent = "VLAN ID harus 1-4094"; return; }
  msg.textContent = "Membaca OLT (read-only)…";
  try {
    const plan = await api(`/olts/${CURRENT_OLT_ID}/vlanif/preview`, { method: "POST", body: JSON.stringify(payload) });
    vlanifStage2(payload, plan);
  } catch (e) { msg.textContent = "Gagal: " + (e.message || e); }
}

// ---------- Tahap 2: preflight + command + konfirmasi ----------
function vlanifStage2(payload, plan) {
  const checks = plan.checks.map(c =>
    `<li>${c.ok ? "✓" : "✗"} ${escapeHtml(c.name)}${c.detail ? " — " + escapeHtml(c.detail) : ""}</li>`).join("");
  const canWrite = plan.ok && plan.write_enabled;
  openModal(`${payload.action === "create" ? "Tambah" : "Edit"} VLANIF ${payload.vlan}`,
    `<ul>${checks}</ul>
     <p>Command yang akan dikirim:</p>
     <pre>${escapeHtml(plan.commands.join("\n") || "(tidak ada — preflight gagal)")}</pre>
     <p class="warn">Perubahan belum permanen sampai "Simpan Permanen". Jika OLT tak terjangkau setelah perubahan, sistem mencoba rollback,
        tetapi <b>rollback lewat jaringan tidak bisa dijamin</b> (cadangan: console/serial atau jalur manajemen lain).</p>
     ${canWrite ? `<input id="vf-confirm" type="text" placeholder="Ketik VLAN ID (${escapeHtml(plan.confirm_hint)}) untuk konfirmasi">`
                : `<p class="muted">${plan.ok ? "Tulis dimatikan di server (dry-run)." : "Preflight gagal — tidak bisa diterapkan."}</p>`}`,
    [{ label: "Kembali", cls: "btn", action: () => openVlanifModal(payload.action === "edit" ? VLANIF_ITEMS.find(x => x.vlan_id === payload.vlan) : undefined) },
     { label: "Terapkan", cls: "btn danger", action: () => vlanifApply(payload, plan) }]);
  const apply = document.querySelectorAll("#modal-footer button")[1];
  apply.disabled = true;
  const confirm = document.getElementById("vf-confirm");
  if (confirm) confirm.addEventListener("input", () => { apply.disabled = confirm.value !== String(payload.vlan); });
}

async function vlanifApply(payload, plan) {
  const confirm_text = document.getElementById("vf-confirm").value;
  closeModal();
  await submitJob(`/olts/${CURRENT_OLT_ID}/vlanif/apply`, {
    method: "POST",
    body: { ...payload, dry_run: false, confirm_text, plan_token: plan.plan_token },
    label: payload.action === "create" ? "Tambah VLANIF" : "Edit VLANIF", subject: `VLAN ${payload.vlan}`,
    refresh: () => loadVlanifTable(),
  });
}

// ---------- Banner "belum permanen" ----------
async function renderVlanifPending() {
  const el = document.getElementById("vlanif-pending");
  if (!el || !CURRENT_OLT_ID) return;
  try {
    const s = await api(`/olts/${CURRENT_OLT_ID}/vlanif/status`);
    if (!s.pending || !s.pending.length) { el.replaceChildren(); return; }
    const box = document.createElement("div");
    box.className = "warn-banner";                   // ADAPT: kelas banner AURORA OPS
    const t = document.createElement("span");
    t.textContent = `Perubahan VLANIF belum permanen (VLAN ${s.pending.map(p => p.vlan).join(", ")}). ` +
                    `"Simpan Permanen" menyimpan SELURUH running-config OLT, termasuk perubahan lain yang belum tersimpan.`;
    const b = document.createElement("button");
    b.className = "btn primary"; b.textContent = "Simpan Permanen";
    b.onclick = () => submitJob(`/olts/${CURRENT_OLT_ID}/vlanif/commit`,
      { method: "POST", label: "Simpan permanen", subject: "OLT", refresh: () => loadVlanifTable() });
    box.append(t, b);
    el.replaceChildren(box);
  } catch (e) { el.replaceChildren(); }
}
