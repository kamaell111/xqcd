// Hapus VLANIF — dimuat SETELAH app.js. Memakai: openModal, closeModal, api, submitJob, toast, escapeHtml.
// Dipanggil dari tombol per baris:  vlanifDelete(vlan, ip)
(function () {
  "use strict";
  const st = document.createElement("style");
  st.textContent = ".vlanif-del-btn{margin-left:6px;padding:4px 10px;border-radius:8px;border:1px solid var(--red);background:transparent;color:var(--red);font-size:12px;font-weight:600;cursor:pointer}.vlanif-del-btn:hover{background:var(--red-soft)}";
  document.head.appendChild(st);
  const esc = (s) => escapeHtml(String(s ?? ""));
  const errMsg = (e) => { let m = (e && e.message) || "Unknown error"; try { const p = JSON.parse(m); m = p.detail || p.message || m; } catch (_) {} return typeof m === "string" ? m : JSON.stringify(m); };
  const url = () => `/olts/${CURRENT_OLT_ID}/vlanif/delete`;
  const reload = () => (typeof window.vlanifReload === "function" ? window.vlanifReload() : location.reload());
  const needText = (vlan, ip) => (ip && ip !== "-" ? String(ip).split("/")[0] : `vlan${vlan}`);

  window.vlanifDelete = function (vlan, ip) { step1(vlan, ip); };

  function step1(vlan, ip) {
    openModal(`Hapus VLANIF vlan${vlan}`, `
      <div class="form-group"><label>VLANIF</label><input value="vlan${esc(vlan)}  ${esc(ip || "(tanpa IP)")}" readonly></div>
      <div style="padding:10px 14px;border-radius:10px;font-size:12px;line-height:1.5;background:var(--red-soft);color:var(--red)">
        Interface ini dihapus dari OLT. Tidak bisa dibatalkan dari aplikasi. Yang dipakai jalur aplikasi, memegang default route, atau terdaftar sebagai dilindungi akan ditolak otomatis.</div>
    `, [
      { label: "Batal", cls: "btn-secondary", action: closeModal },
      { label: "Lanjut", cls: "btn-primary", action: () => step2(vlan, ip) },
    ]);
  }

  function step2(vlan, ip) {
    const need = needText(vlan, ip);
    openModal(`Konfirmasi hapus vlan${vlan}`, `
      <div class="form-group"><label>Ketik ${ip && ip !== "-" ? "IP lengkap" : "nama VLANIF"}: <b>${esc(need)}</b></label>
        <input id="vd-confirm" autocomplete="off" placeholder="${esc(need)}"></div>
      <label style="display:block;margin:8px 0"><input type="checkbox" id="vd-dry" checked> Preview saja (TIDAK menghapus, hanya tampilkan perintah)</label>
    `, [
      { label: "Kembali", cls: "btn-secondary", action: () => step1(vlan, ip) },
      { label: "Lanjut", cls: "btn-primary", action: () => run(vlan, ip) },
    ]);
  }

  async function run(vlan, ip) {
    const typed = document.getElementById("vd-confirm").value.trim();
    const dry = document.getElementById("vd-dry").checked;
    const body = { vlan: Number(vlan), confirm_ip: typed, dry_run: dry };
    if (dry) {
      try {
        const r = await api(url(), { method: "POST", body: JSON.stringify(body) });
        openModal("Preview — belum ada yang dihapus", `
          <div class="form-group"><label>Perintah yang akan dikirim ke OLT</label></div>
          <div style="padding:10px 12px;border-radius:8px;background:rgba(255,255,255,.05);font:12px/1.5 monospace;white-space:pre-wrap">${(r.commands || []).map(esc).join("\n")}</div>`, [
          { label: "Tutup", cls: "btn-secondary", action: closeModal },
          { label: "Hapus sungguhan", cls: "btn-primary", action: () => real(vlan, typed) },
        ]);
      } catch (e) { toast(errMsg(e), "error"); }
      return;
    }
    real(vlan, typed);
  }

  async function real(vlan, typed) {
    closeModal();
    await submitJob(url(), {
      method: "POST", body: { vlan: Number(vlan), confirm_ip: typed, dry_run: false },
      label: "Hapus VLANIF", subject: `vlan${vlan}`, refresh: reload,
    });
  }
})();
