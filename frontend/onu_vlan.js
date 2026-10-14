// Edit VLAN per ONU — dimuat SETELAH app.js. Memakai: openModal, closeModal, api, submitJob, toast, escapeHtml.
(function () {
  "use strict";
  let C = null;   // konteks ONU yang sedang dibuka

  const st = document.createElement("style");
  st.textContent = ".ov-cmd{margin:8px 0;padding:10px 12px;border-radius:8px;background:rgba(255,255,255,.05);font:12px/1.5 monospace;white-space:pre-wrap;word-break:break-all}" +
    ".ov-note{margin:0 0 12px;padding:10px 14px;border-radius:10px;font-size:12px;line-height:1.5;background:var(--amber-soft);color:var(--amber)}" +
    ".ov-err{background:var(--red-soft);color:var(--red)}";
  document.head.appendChild(st);

  const base = () => `/onu/${C.olt}/${C.id}/vlan`;
  const esc = (s) => escapeHtml(String(s ?? ""));
  const errMsg = (e) => { let m = (e && e.message) || "Unknown error"; try { const p = JSON.parse(m); m = p.detail || p.message || m; } catch (_) {} return typeof m === "string" ? m : JSON.stringify(m); };

  window.onuVlanAttach = function (o) {
    const sec = [...document.querySelectorAll("#onu-detail-content .detail-section")]
      .find((s) => /Service\s*&\s*VLAN/i.test(s.textContent));
    if (!sec) { console.warn("[onu_vlan] section 'Service & VLAN' tidak ditemukan"); return; }
    if (sec.querySelector(".onu-vlan-btn")) return;
    C = {
      olt: typeof CURRENT_ONU_OLT_ID !== "undefined" && CURRENT_ONU_OLT_ID ? CURRENT_ONU_OLT_ID : CURRENT_OLT_ID,
      id: CURRENT_ONU_ID, serial: o && o.serial_number, name: o && o.name, vlan: o && o.vlan,
    };
    const b = document.createElement("button");
    b.className = "btn-secondary onu-vlan-btn";
    b.style.marginTop = "10px";
    b.innerHTML = '<i class="fas fa-pen"></i> Edit VLAN';
    b.onclick = () => step1();
    sec.appendChild(b);
  };

  function step1() {
    openModal(`Edit VLAN — ${C.name || ""}`, `
      <div class="ov-note">Mengubah VLAN memutus layanan pelanggan ini sesaat. Langkah berikutnya hanya memeriksa (tidak menulis ke OLT).</div>
      <div class="form-group"><label>VLAN sekarang</label><input value="${esc(C.vlan ?? "-")}" readonly></div>
      <div class="form-group"><label>VLAN baru</label><input type="number" id="ov-new" min="2" max="4094" placeholder="mis. 101"></div>
    `, [
      { label: "Batal", cls: "btn-secondary", action: closeModal },
      { label: "Cek rencana", cls: "btn-primary", action: preview },
    ]);
  }

  async function preview() {
    const v = parseInt(document.getElementById("ov-new").value, 10);
    if (!(v >= 2 && v <= 4094)) { toast("VLAN baru harus 2-4094", "error"); return; }
    try {
      const r = await api(`${base()}/preview`, { method: "POST", body: JSON.stringify({ new_vlan: v }) });
      if (C.serial && r.onu.serial_number && String(C.serial).toUpperCase() !== String(r.onu.serial_number).toUpperCase()) {
        toast("ONU di server tidak cocok dengan halaman ini. Dibatalkan.", "error"); return;
      }
      step2(v, r);
    } catch (e) { toast(errMsg(e), "error"); }
  }

  function step2(v, r) {
    const head = `
      <div class="form-group"><label>ONU</label><input value="${esc(r.onu.name)} — ${esc(r.onu.serial_number)} (${esc(r.onu.interface_name)})" readonly></div>
      <div class="form-group"><label>Perubahan</label><input value="VLAN ${esc(r.current_vlan ?? C.vlan)} → ${esc(v)}" readonly></div>`;
    if (r.errors && r.errors.length) {
      openModal("Tidak bisa dilanjutkan", head + `<div class="ov-note ov-err">${r.errors.map(esc).join("<br>")}</div>`,
        [{ label: "Kembali", cls: "btn-secondary", action: step1 }]);
      return;
    }
    openModal("Konfirmasi Edit VLAN", head + `
      <div class="ov-note">Perintah yang akan dikirim ke OLT:</div>
      <div class="ov-cmd">${r.commands.map(esc).join("\n")}</div>
      <div class="ov-note">Jika gagal, otomatis dikembalikan ke VLAN ${esc(r.current_vlan)}.</div>
      <div class="form-group"><label>Ketik serial ONU untuk konfirmasi</label><input id="ov-confirm" placeholder="${esc(r.onu.serial_number)}" autocomplete="off"></div>
      <label style="display:block;margin:8px 0"><input type="checkbox" id="ov-dry" checked> Dry-run (cek saja, TIDAK dikirim ke OLT)</label>
      <label style="display:block;margin:8px 0"><input type="checkbox" id="ov-save"> Simpan permanen (write) setelah terverifikasi</label>
      ${r.write_enabled ? "" : '<div class="ov-note ov-err">Fitur tulis dimatikan di server: hanya dry-run yang bisa dijalankan.</div>'}
    `, [
      { label: "Kembali", cls: "btn-secondary", action: step1 },
      { label: "Jalankan", cls: "btn-primary", action: () => run(v, r) },
    ]);
  }

  async function run(v, r) {
    const serial = document.getElementById("ov-confirm").value.trim();
    const dry = document.getElementById("ov-dry").checked;
    const save = document.getElementById("ov-save").checked;
    if (serial.toUpperCase() !== String(r.onu.serial_number).toUpperCase()) { toast("Serial tidak cocok", "error"); return; }
    const body = { new_vlan: v, confirm_serial: serial, dry_run: dry, save };
    closeModal();
    if (dry) {
      try {
        await api(`${base()}/apply`, { method: "POST", body: JSON.stringify(body) });
        toast("Dry-run OK: tidak ada yang dikirim ke OLT", "info");
      } catch (e) { toast(errMsg(e), "error"); }
      return;
    }
    await submitJob(`${base()}/apply`, {
      method: "POST", body, label: "Edit VLAN ONU", subject: serial,
      refresh: () => loadONUDetailPage(),
    });
  }
})();
