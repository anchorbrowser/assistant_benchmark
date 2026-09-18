/* abench — shared client behaviour. Pages render server-side; this only enhances:
   sortable tables, scorecard filters, the compare picker, and copy buttons.
   No dependencies, no data fetching (data is baked into each page). */
(function () {
  "use strict";

  // ---- sortable tables -------------------------------------------------
  function cellKey(row, i) {
    var td = row.children[i];
    if (!td) return "";
    if (td.dataset.sort !== undefined) return parseFloat(td.dataset.sort);
    var t = td.textContent.trim().replace(/[%s,]/g, "");
    var n = parseFloat(t);
    return isNaN(n) ? td.textContent.trim().toLowerCase() : n;
  }
  function makeSortable(table) {
    var ths = table.tHead ? table.tHead.rows[0].cells : [];
    Array.prototype.forEach.call(ths, function (th, i) {
      if (th.classList.contains("nosort")) return;
      th.classList.add("sortable", "sortarrow");
      th.addEventListener("click", function () {
        var asc = th.getAttribute("aria-sort") !== "ascending";
        Array.prototype.forEach.call(ths, function (o) { o.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", asc ? "ascending" : "descending");
        var body = table.tBodies[0];
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var ka = cellKey(a, i), kb = cellKey(b, i);
          if (ka < kb) return asc ? -1 : 1;
          if (ka > kb) return asc ? 1 : -1;
          return 0;
        });
        rows.forEach(function (r) { body.appendChild(r); });
      });
    });
  }

  // ---- scorecard filters ----------------------------------------------
  function wireFilters() {
    var bar = document.querySelector("[data-filters]");
    if (!bar) return;
    var table = document.querySelector(bar.getAttribute("data-filters"));
    if (!table) return;
    var controls = bar.querySelectorAll("[data-filter]");
    function apply() {
      Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
        var show = true;
        Array.prototype.forEach.call(controls, function (c) {
          var want = c.value;
          if (!want) return;
          var have = row.dataset[c.getAttribute("data-filter")] || "";
          if (c.tagName === "INPUT") {
            if (have.toLowerCase().indexOf(want.toLowerCase()) === -1) show = false;
          } else if (have.split(" ").indexOf(want) === -1) {
            show = false;
          }
        });
        row.style.display = show ? "" : "none";
      });
    }
    Array.prototype.forEach.call(controls, function (c) {
      c.addEventListener("input", apply);
      c.addEventListener("change", apply);
    });
    var reset = bar.querySelector(".reset");
    if (reset) reset.addEventListener("click", function () {
      Array.prototype.forEach.call(controls, function (c) { c.value = ""; });
      apply();
    });
  }

  // ---- compare picker --------------------------------------------------
  function wireCompare() {
    var form = document.querySelector("[data-compare]");
    if (!form) return;
    var L = form.querySelector("[data-cmp-left]");
    var R = form.querySelector("[data-cmp-right]");
    var go = form.querySelector("[data-cmp-go]");
    var prefix = form.getAttribute("data-compare") || "compare/";
    function navigate() {
      var a = L.value, b = R.value;
      if (!a || !b || a === b) return;
      var pair = [a, b].sort();
      window.location.href = prefix + pair[0] + "-vs-" + pair[1] + ".html";
    }
    if (go) go.addEventListener("click", navigate);
  }

  // ---- copy buttons ----------------------------------------------------
  function wireCopy() {
    document.querySelectorAll("[data-copy]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var sel = btn.getAttribute("data-copy");
        var el = document.querySelector(sel);
        if (!el) return;
        var text = el.textContent;
        navigator.clipboard.writeText(text).then(function () {
          var old = btn.textContent;
          btn.textContent = "copied";
          setTimeout(function () { btn.textContent = old; }, 1200);
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("table[data-sortable]").forEach(makeSortable);
    wireFilters();
    wireCompare();
    wireCopy();
  });
})();
