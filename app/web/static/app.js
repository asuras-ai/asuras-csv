// Asuras CSV front-end behaviour: theme, row menus, bulk selection, table filters, toasts, mobile sidebar and
// the poll-failure badge. Plain JS, no build step. Event delegation means htmx swaps need no re-binding.
(function () {
  "use strict";
  var root = document.documentElement;
  var darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

  function effectiveTheme() {
    return root.dataset.theme || (darkQuery.matches ? "dark" : "light");
  }
  function announceTheme() {
    document.dispatchEvent(new CustomEvent("themechange", { detail: effectiveTheme() }));
  }
  function toggleTheme() {
    var next = effectiveTheme() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) { /* storage blocked: the choice lasts for this page */ }
    announceTheme();
  }
  darkQuery.addEventListener("change", function () { if (!root.dataset.theme) announceTheme(); });

  function closeMenus(except) {
    document.querySelectorAll("details.menu[open]").forEach(function (menu) {
      if (!except || !menu.contains(except)) menu.open = false;
    });
  }
  // Menus sit inside horizontally scrolling table cards, so the list is position: fixed and placed here.
  document.addEventListener("toggle", function (ev) {
    var menu = ev.target;
    if (!(menu instanceof HTMLDetailsElement) || !menu.classList.contains("menu") || !menu.open) return;
    var list = menu.querySelector(".menu-list");
    var r = menu.querySelector("summary").getBoundingClientRect();
    list.style.top = Math.max(8, Math.min(r.bottom + 4, window.innerHeight - list.offsetHeight - 8)) + "px";
    list.style.left = Math.max(8, r.right - list.offsetWidth) + "px";
  }, true);
  window.addEventListener("scroll", function (ev) {
    if (!(ev.target instanceof Element && ev.target.closest(".menu-list"))) closeMenus(null);
  }, true);

  function boxes() { return document.querySelectorAll('input[name="ids"]'); }
  function syncSelection() {
    var bar = document.getElementById("zip-form");
    if (!bar) return;
    var all = boxes(), checked = 0;
    all.forEach(function (box) {
      var row = box.closest("tr");
      if (row) row.classList.toggle("selected", box.checked);
      if (box.checked) checked++;
    });
    bar.hidden = checked === 0;
    document.getElementById("bulk-count").textContent = checked + " selected";
    var head = document.getElementById("select-all");
    if (head) {
      head.checked = all.length > 0 && checked === all.length;
      head.indeterminate = checked > 0 && checked < all.length;
    }
  }

  function applyFilters() {
    var bar = document.querySelector("[data-filters]");
    if (!bar) return;
    var text = bar.querySelector('[data-filter="text"]').value.trim().toLowerCase();
    var provider = bar.querySelector('[data-filter="provider"]').value;
    var status = bar.querySelector('[data-filter="status"]').value;
    var rows = document.querySelectorAll("tr[data-symbol]"), shown = 0;
    rows.forEach(function (row) {
      var match = (!text || row.dataset.symbol.indexOf(text) !== -1) &&
        (!provider || row.dataset.provider === provider) &&
        (!status || row.dataset.status === status);
      row.hidden = !match;
      if (match) shown++;
    });
    var empty = document.getElementById("filter-empty");
    if (empty) empty.hidden = shown > 0 || rows.length === 0;
  }

  function setReconnecting(on) {
    var badge = document.getElementById("reconnecting");
    if (badge) badge.hidden = !on;
  }

  document.addEventListener("click", function (ev) {
    var t = ev.target;
    if (!(t instanceof Element)) return;
    if (t.closest("[data-theme-toggle]")) { toggleTheme(); return; }
    if (t.closest("[data-sidebar-toggle]")) { document.body.classList.toggle("sidebar-open"); return; }
    var toastClose = t.closest(".toast-close");
    if (toastClose) { toastClose.closest(".toast").remove(); return; }
    if (t.closest("[data-clear-selection]")) {
      boxes().forEach(function (box) { box.checked = false; });
      syncSelection();
      return;
    }
    closeMenus(t);
    var row = t.closest("tr[data-href]");
    if (row && !t.closest("a, button, input, label, select, summary, details, form")) window.location.href = row.dataset.href;
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { closeMenus(null); document.body.classList.remove("sidebar-open"); }
  });
  document.addEventListener("change", function (ev) {
    var t = ev.target;
    if (t.id === "select-all") {
      boxes().forEach(function (box) { if (!box.closest("tr").hidden) box.checked = t.checked; });
    }
    if (t.name === "ids" || t.id === "select-all") syncSelection();
    if (t.closest && t.closest("[data-filters]")) applyFilters();
  });
  document.addEventListener("input", function (ev) {
    if (ev.target.closest && ev.target.closest("[data-filters]")) applyFilters();
  });
  document.addEventListener("submit", function (ev) {
    if (ev.target.id === "zip-form" && !document.querySelector('input[name="ids"]:checked')) ev.preventDefault();
  });
  document.addEventListener("htmx:afterSwap", function () { applyFilters(); syncSelection(); });
  document.addEventListener("htmx:sendError", function () { setReconnecting(true); });
  document.addEventListener("htmx:responseError", function () { setReconnecting(true); });
  document.addEventListener("htmx:afterRequest", function (ev) { if (ev.detail.successful) setReconnecting(false); });

  document.querySelectorAll(".toast").forEach(function (toast) { setTimeout(function () { toast.remove(); }, 4000); });
  applyFilters();
  syncSelection();
})();
