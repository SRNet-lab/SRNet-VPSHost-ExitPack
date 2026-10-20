// The small shell around the Exits tab for `srnet-exits ui` (the SRNet app has its own).
// It gives tab.js the same three helpers the app does: api, el and sheet.
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  async function api(path, body) {
    const opt = {headers: {"X-Core": "1"}};
    if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch("/api/" + path, opt);
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
    t.mount($("exits"), {api, el, sheet, status: () => {}});
    t.show();
  });
})();
