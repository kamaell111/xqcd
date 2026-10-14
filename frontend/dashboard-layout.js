/* ITERASI 3 — penanda layout dashboard.
   Tidak mengubah HTML dan tidak menyentuh id apa pun. Script ini hanya
   menambahkan class ke elemen yang sudah ada, dengan mencocokkan judul
   di .widget-head:
     "Alert terbaru", "Redaman optik ONU" -> .is-retired (display:none lewat CSS;
                                             elemen & id tetap ada, jadi app.js
                                             tidak error)
     "Port PON"                          -> .dash-pon  (+ induknya .dash-split,
                                             kontainer port .dash-ports)
     "Beban CPU & Memory"                -> .dash-chart (+ wadah canvas .dash-chart-box)
   Pasang SETELAH app.js, di akhir <body>. */
(function () {
  function tagPorts(w) {
    // kontainer port = turunan pertama dengan >= 8 anak (legend & header jauh di bawah itu)
    var all = w.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      if (all[i].children.length >= 8) { all[i].classList.add('dash-ports'); return; }
    }
  }

  function tagChart(w) {
    var canvas = w.querySelector('canvas');
    if (!canvas) return;
    if (canvas.parentElement && canvas.parentElement !== w) {
      canvas.parentElement.classList.add('dash-chart-box');
    }
    // Chart.js default menghitung tinggi dari rasio lebar; matikan agar mengikuti
    // tinggi wadah (300px di CSS). Chart dibuat setelah data tiba, jadi tunggu sebentar.
    var tries = 0;
    var timer = setInterval(function () {
      var c = window.Chart && Chart.getChart ? Chart.getChart(canvas) : null;
      if (c) {
        clearInterval(timer);
        if (c.options.maintainAspectRatio !== false) {
          c.options.maintainAspectRatio = false;
          c.resize();
        }
      } else if (++tries > 40) {
        clearInterval(timer);
      }
    }, 250);
  }

  function run() {
    var page = document.getElementById('page-dashboard');
    if (!page) return;
    var retired = [];

    page.querySelectorAll('.widget').forEach(function (w) {
      var head = w.querySelector('.widget-header');
      var t = head ? head.textContent.replace(/\s+/g, ' ').trim().toLowerCase() : '';
      if (t.indexOf('alert terbaru') > -1 || t.indexOf('redaman optik') > -1) {
        w.classList.add('is-retired');
        retired.push(w);
      } else if (t.indexOf('port pon') > -1) {
        w.classList.add('dash-pon');
        if (w.parentElement && w.parentElement !== page) w.parentElement.classList.add('dash-split');
        tagPorts(w);
      } else if (t.indexOf('beban cpu') > -1) {
        w.classList.add('dash-chart');
        tagChart(w);
      }
    });

    // Induk yang seluruh anaknya disembunyikan ikut disembunyikan (hilangkan sisa jarak)
    retired.forEach(function (w) {
      var p = w.parentElement;
      if (!p || p === page) return;
      var allHidden = Array.prototype.every.call(p.children, function (c) {
        return c.classList.contains('is-retired');
      });
      if (allHidden) p.classList.add('is-retired');
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
  else run();
})();
