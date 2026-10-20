// The small shell around the Exits tab for `srnet-exits ui` (the SRNet app has its own).
// It gives tab.js the same helpers the app does (api, el and sheet), plus headers() for its QR fetch.
// `srnet-exits ui` opens /#t=TOKEN: the token moves to sessionStorage, leaves the address bar, and
// goes with every API call as X-Token.
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const KEY = "srnet-exits-token";
  const m = /^#t=([A-Za-z0-9_-]+)$/.exec(location.hash);
  if (m) { sessionStorage.setItem(KEY, m[1]); history.replaceState(null, "", location.pathname); }
  const token = sessionStorage.getItem(KEY) || "";
  const headers = () => ({"X-Core": "1", "X-Token": token});
  const STALE = "This page is from an earlier run. Open the address `srnet-exits ui` printed (it ends in #t=...).";
  async function api(path, body) {
    const opt = {headers: headers()};
    if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch("/api/" + path, opt);
    if (r.status === 403) return {ok: false, err: STALE};
    return r.json();
  }
  function el(t, txt) { const e = document.createElement(t); if (txt !== undefined) e.textContent = String(txt); return e; }
  function sheet(title, sub, lines, buttons) {
    $("sheet-title").textContent = title; $("sheet-sub").textContent = sub || "";
    $("sheet-body").textContent = lines.join("\n");
    const b = $("sheet-buttons"); b.replaceChildren();
    buttons.forEach(([label, fn]) => { const x = el("button", label); x.addEventListener("click", async () => { $("sheet").hidden = true; await fn(x); }); b.appendChild(x); });
    $("sheet").hidden = false;
  }
  window.addEventListener("load", () => {
    const t = (window.SRNetAddons || {}).exits;
    if (!t) { $("exits").textContent = "The Exits page did not load."; return; }
    if (!token) { $("exits").textContent = STALE; return; }
    t.mount($("exits"), {api, el, sheet, headers, status: () => {}});
    t.show();
  });
})();
