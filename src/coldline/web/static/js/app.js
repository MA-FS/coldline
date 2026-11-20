// ColdLine UI glue. No framework: htmx does the server round-trips, this file does small client niceties.
(function () {
  "use strict";
  var root = document.documentElement;

  // theme toggle (dark default, persisted per browser)
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-theme-toggle]");
    if (btn) {
      var next = root.dataset.theme === "light" ? "dark" : "light";
      root.dataset.theme = next;
      try { localStorage.setItem("coldline-theme", next); } catch (err) { /* private mode */ }
      return;
    }
    // toast dismiss
    var close = e.target.closest("[data-dismiss]");
    if (close) { var t = close.closest(".toast"); if (t) t.remove(); return; }
    // close open <details class="menu"> when clicking outside or on an item
    document.querySelectorAll("details.menu[open]").forEach(function (d) {
      if (!d.contains(e.target) || e.target.closest(".menu-item")) d.removeAttribute("open");
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") document.querySelectorAll("details.menu[open]").forEach(function (d) { d.removeAttribute("open"); });
  });

  // Toasts: server can send `HX-Trigger: {"toast": {"title": "...", "text": "...", "tone": "ok|amber|red"}}`
  function toast(detail) {
    var box = document.getElementById("toasts");
    if (!box || !detail) return;
    var el = document.createElement("div");
    el.className = "toast toast--" + (detail.tone || "neutral");
    el.setAttribute("role", "status");
    var body = document.createElement("div");
    body.className = "toast-body";
    body.textContent = detail.title || "";
    if (detail.text) { var s = document.createElement("small"); s.textContent = detail.text; body.appendChild(s); }
    var dot = document.createElement("span");
    dot.className = "icon";
    var x = document.createElement("button");
    x.className = "toast-close"; x.type = "button"; x.setAttribute("data-dismiss", ""); x.setAttribute("aria-label", "Dismiss"); x.textContent = "×";
    el.appendChild(dot); el.appendChild(body); el.appendChild(x);
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 6000);
  }
  document.body.addEventListener("toast", function (e) { toast(e.detail); });
  // refused requests (403, 404, 429) without their own toast: say why instead of failing silently
  document.body.addEventListener("htmx:responseError", function (e) {
    var xhr = e.detail && e.detail.xhr;
    if (!xhr || xhr.getResponseHeader("HX-Trigger")) return;
    var text = "";
    try { text = JSON.parse(xhr.responseText).detail || ""; } catch (err) { /* not JSON */ }
    toast({ title: "Request refused (" + xhr.status + ")", text: text, tone: "red" });
  });
  window.coldlineToast = toast;

  // SSE fragments are applied at most once: anything whose element id is already on the page (a step or an
  // approval card from the server-rendered snapshot, or from before a reconnect) is dropped before the swap.
  document.body.addEventListener("htmx:sseBeforeMessage", function (e) {
    var data = e.detail && e.detail.data;
    if (typeof data !== "string" || data.indexOf("id=") < 0 || !window.htmx) return;
    var tpl = document.createElement("template");
    tpl.innerHTML = data;
    var dropped = false;
    tpl.content.querySelectorAll("[id]").forEach(function (el) {
      if (document.getElementById(el.id)) { el.remove(); dropped = true; }
    });
    if (!dropped) return;
    e.preventDefault();
    tpl.content.querySelectorAll("[hx-swap-oob]").forEach(function (el) { if (!el.children.length) el.remove(); });
    var rest = tpl.innerHTML.trim();
    if (rest) htmx.swap(e.target, rest, { swapStyle: "beforeend" });
  });

  // The header (approvals count, mode pill, clock) sits outside the regions htmx swaps. Refresh it out of
  // band after anything that changes state: approval decisions announce "plant-changed", the stream ends, or
  // a streamed step queues a proposal.
  var shellTimer = null;
  function refreshShell() {
    if (!window.htmx) return;
    clearTimeout(shellTimer);
    shellTimer = setTimeout(function () { htmx.ajax("GET", "/shell", { target: "#mode-pill", swap: "none" }); }, 150);
  }
  document.body.addEventListener("plant-changed", refreshShell);
  document.body.addEventListener("htmx:sseMessage", function (e) {
    var data = e.detail && e.detail.data;
    if (typeof data === "string" && data.indexOf("hx-swap-oob") >= 0) refreshShell();
  });

  // keep the agent step list scrolled to the newest step while streaming
  document.body.addEventListener("htmx:sseMessage", function () {
    var sc = document.querySelector(".agent-scroll");
    if (sc) sc.scrollTop = sc.scrollHeight;
  });
  // the SSE stream sends `event: done` when a run finishes: drop the "working" skeleton
  document.body.addEventListener("htmx:sseMessage", function (e) {
    if (e.detail && e.detail.type === "done") { var w = document.getElementById("agent-thinking"); if (w) w.remove(); }
  });

  // finished runs: open the agent panel scrolled to the conclusion and pending approvals
  var sc0 = document.querySelector(".agent-scroll");
  if (sc0 && sc0.scrollHeight > sc0.clientHeight) {
    var target = document.getElementById("agent-conclusion");
    if (target && target.children.length) sc0.scrollTop = target.offsetTop - sc0.offsetTop - 220;
  }

  // when a run finishes the server sends `done` and the stream closes (sse-close="done"):
  // re-render the page region so totals, approvals, holds and the mimic show the final state
  document.body.addEventListener("htmx:sseClose", function (e) {
    if (!e.detail || e.detail.type !== "message" || !window.htmx) return;
    refreshShell();
    var w = document.getElementById("agent-thinking"); if (w) w.remove();
    if (document.getElementById("overview")) {
      htmx.ajax("GET", "/", { target: "#overview", select: "#overview", swap: "outerHTML" }).then(function () {
        var sc = document.querySelector(".agent-scroll"), c = document.getElementById("agent-conclusion");
        if (sc && c) sc.scrollTop = c.offsetTop - sc.offsetTop - 160;
      });
    } else if (document.getElementById("trace-steps")) {
      htmx.ajax("GET", location.pathname, { target: "#main", select: "#main", swap: "outerHTML" });
    }
  });

  // copy buttons: <button data-copy="#selector">
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-copy]");
    if (!btn) return;
    var src = document.querySelector(btn.getAttribute("data-copy"));
    if (src && navigator.clipboard) {
      navigator.clipboard.writeText(src.innerText).then(function () { toast({ title: "Copied", tone: "ok" }); });
    }
  });

  // mimic units are focusable: Enter or Space opens the chiller detail like a click
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    var unit = e.target.closest && e.target.closest(".m-unit");
    if (!unit) return;
    e.preventDefault();
    unit.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });

  // Narrow screens: the mimic and the nav scroll sideways. Bring the faulted (or selected) chiller and the
  // current tab into view, and keep the visitor's own scroll position across the mimic's 5 s refresh.
  var mimicState = { key: null, left: 0 };
  function faultKey() {
    return Array.prototype.map.call(document.querySelectorAll(".mimic .m-frame--amber, .mimic .m-frame--red"),
      function (f) { var u = f.closest(".m-unit"); return u ? u.id : ""; }).join(",");
  }
  function placeMimic() {
    var wrap = document.querySelector(".mimic-wrap");
    if (!wrap || wrap.scrollWidth <= wrap.clientWidth + 4) return;
    var key = faultKey();
    if (key === mimicState.key) { wrap.scrollLeft = mimicState.left; return; }
    mimicState.key = key;
    var frame = document.querySelector(".mimic .m-frame--red, .mimic .m-frame--amber");
    var unit = frame ? frame.closest(".m-unit") : document.querySelector('.m-unit[aria-pressed="true"]');
    if (!unit) return;
    var u = unit.getBoundingClientRect(), w = wrap.getBoundingClientRect();
    wrap.scrollLeft = Math.max(0, wrap.scrollLeft + (u.left - w.left) - (w.width - u.width) / 2);
    mimicState.left = wrap.scrollLeft;
  }
  document.addEventListener("scroll", function (e) {
    if (e.target && e.target.classList && e.target.classList.contains("mimic-wrap")) mimicState.left = e.target.scrollLeft;
  }, true);
  document.body.addEventListener("htmx:afterSettle", placeMimic);
  placeMimic();
  var nav = document.querySelector(".nav"), current = nav && nav.querySelector('[aria-current="page"]');
  if (nav && current && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = current.offsetLeft - 16;

  // mark the selected chiller on the mimic
  document.addEventListener("click", function (e) {
    var unit = e.target.closest && e.target.closest(".m-unit");
    if (!unit) return;
    document.querySelectorAll(".m-unit").forEach(function (u) { u.setAttribute("aria-pressed", u === unit ? "true" : "false"); });
  });
})();
