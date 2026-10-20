// Exits tab for the SRNet app (srnet-exits add-on). The SRNet core serves this file from its own
// origin and forwards calls to the add-on backend through /api/addon/exits/*. Nothing here signs
// or sends a transaction: it shows a payment (address, exact amount, QR) and watches for it.
"use strict";
(() => {
  const NAME = "exits";
  let sh = null, root = null, polls = {}, statusTimer = null;
  const call = (c, body) => sh.api("addon/" + NAME + "/" + c, body || {});
  const el = (t, txt, cls) => { const e = sh.el(t, txt); if (cls) e.className = cls; return e; };
  const hhmm = (t) => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  const left = (s) => s >= 3600 ? Math.floor(s / 3600) + " h " + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + " min" : Math.max(0, Math.floor(s / 60)) + " min";
  const ago = (s) => s < 3600 ? Math.floor(s / 60) + "m ago" : Math.floor(s / 3600) + "h" + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m ago";
  const q = (id) => root.querySelector("[data-x='" + id + "']");
  function log(m) { const t = new Date().toLocaleTimeString(); q("log").textContent = t + "  " + m + "\n" + q("log").textContent; }
  function copyBtn(text) { const b = el("button", "Copy"); b.addEventListener("click", async () => { try { await navigator.clipboard.writeText(text); b.textContent = "Copied"; } catch (e) { b.textContent = "Select and copy"; } }); return b; }

  function build() {
    root.replaceChildren();
    const h = el("h2", "Exits "); h.appendChild(el("span", "Beta", "warn")); root.appendChild(h);
    root.appendChild(el("p", "Buy time on an exit and route your traffic through it. You pay the seller directly in USDC on Arbitrum or Base from any wallet, and pay the network fee yourself. Payments are final. Nothing renews by itself: you get a reminder about 10 minutes before the end, and buying again adds time.", "muted"));
    const ending = el("p", "", "warn"); ending.dataset.x = "ending"; ending.hidden = true; root.appendChild(ending);
    root.appendChild(el("h3", "Paid time"));
    const lt = el("table"); lt.className = "kv"; lt.dataset.x = "leases"; root.appendChild(lt);
    const conn = el("p", "", "muted"); conn.dataset.x = "conn"; root.appendChild(conn);
    const dis = el("button", "Disconnect"); dis.addEventListener("click", () => act("disconnect", {})); root.appendChild(dis);
    const pay = el("section"); pay.dataset.x = "pay"; pay.hidden = true; root.appendChild(pay);
    root.appendChild(el("h3", "Exit sellers"));
    const info = el("p", "Loading the signed exit board…", "muted"); info.dataset.x = "boardinfo"; root.appendChild(info);
    const tb = el("table"); tb.className = "grid"; const th = el("thead"); const tr = el("tr");
    ["Seller", "Country", "USDC/hr", "Chains", "Exit DNS", "Last refresh", ""].forEach((x) => tr.appendChild(el("th", x)));
    th.appendChild(tr); tb.appendChild(th); const body = el("tbody"); body.dataset.x = "board"; tb.appendChild(body); root.appendChild(tb);
    const rf = el("button", "Refresh"); rf.addEventListener("click", () => { board(); status(); }); root.appendChild(rf);
    root.appendChild(el("h3", "Settings"));
    const st = el("div");
    const lim = el("input"); lim.size = 6; lim.dataset.x = "limit";
    const ch = el("select"); ch.dataset.x = "chain"; [["", "Arbitrum if the seller takes it"], ["arbitrum", "Arbitrum"], ["base", "Base"]].forEach(([v, t]) => { const o = el("option", t); o.value = v; ch.appendChild(o); });
    const save = el("button", "Save"); save.addEventListener("click", async () => {
      const r = await call("settings", {max_payment_usdc: lim.value.trim(), chain: ch.value});
      log(r.ok ? "settings saved: limit " + r.max_payment_usdc + " USDC" : "settings: " + r.err); });
    const l1 = el("label", "Largest single payment (USDC) "); l1.appendChild(lim);
    const l2 = el("label", " Pay on "); l2.appendChild(ch);
    st.append(l1, l2, save); root.appendChild(st);
    const lg = el("pre", "", "log"); lg.dataset.x = "log"; root.appendChild(lg);
  }

  async function needAck(r) {
    return new Promise((res) => sh.sheet("Before you use an exit", "read this once", String(r.warning || "").split("\n"),
      [["I understand", async () => { const a = await call("ack", {text: "I UNDERSTAND"}); res(!!a.ok); }], ["Cancel", async () => res(false)]]));
  }

  async function status() {
    const r = await call("status", {});
    if (!r.ok) { log("status: " + r.err); return; }
    const lt = q("leases"); lt.replaceChildren();
    const now = Date.now() / 1000;
    if (!r.leases.length) { const tr = el("tr"); tr.appendChild(el("td", "No paid time right now.")); lt.appendChild(tr); }
    let soon = [];
    r.leases.forEach((l) => { const tr = el("tr"); tr.appendChild(el("th", l.seller.slice(0, 16) + "…"));
      tr.appendChild(el("td", "until " + hhmm(l.end) + " (" + left(l.end - now) + " left)"));
      const c = el("button", "Connect"); c.addEventListener("click", () => act("connect", {seller: l.seller}));
      const td = el("td"); td.appendChild(c); tr.appendChild(td); lt.appendChild(tr);
      if (l.ending) soon.push(l); });
    const e = q("ending");
    e.hidden = !soon.length;
    if (soon.length) e.textContent = "Your exit time with " + soon[0].seller.slice(0, 12) + "… ends at " + hhmm(soon[0].end) + ". Buy more below to keep going.";
    q("conn").textContent = r.connected ? "Connected through " + String(r.connected.seller).slice(0, 16) + "… · DNS: " + r.dns : "Not connected.";
    if (!q("limit").value) q("limit").value = r.max_payment_usdc || "20";
    (r.quotes || []).forEach((x) => { if (!polls[x.id]) watch(x.id); });
  }

  async function board() {
    const info = q("boardinfo"), tb = q("board");
    info.textContent = "Loading the signed exit board…"; tb.replaceChildren();
    const r = await call("board", {});
    if (!r.ok) { info.textContent = "Couldn't load the exit board (" + (r.err || "?") + "). Is Session Router running? Try Refresh."; return; }
    info.textContent = "Board " + r.seq + " from " + (r.source === "cache" ? "the cached copy" : "SRNet") + (r.needs_update ? " · " + r.needs_update + " seller(s) need to update" : "");
    if (!r.listings.length) { const tr = el("tr"); tr.appendChild(el("td", "No sellers right now.")); tb.appendChild(tr); }
    r.listings.forEach((x) => {
      const tr = el("tr"); const s = el("td", x.seller.slice(0, 16) + "…"); s.title = x.seller; tr.appendChild(s);
      [x.country || x.region || "-", x.price || "-", (x.chains || []).join(", ") || "-", x.dns ? "yes" : "NONE", ago(x.age_s)].forEach((v) => tr.appendChild(el("td", v)));
      const td = el("td");
      if (!x.buyable) td.appendChild(el("span", x.note || "can't buy", "muted"));
      else if (!x.dns) td.appendChild(el("span", "no exit DNS (use the command line with --system-dns)", "muted"));
      else {
        const hrs = el("input"); hrs.type = "number"; hrs.min = "1"; hrs.max = String(x.max_hours || 24); hrs.value = "1"; hrs.style.width = "4em";
        const b = el("button", "Buy"); b.addEventListener("click", () => buy(x, parseInt(hrs.value, 10) || 1));
        td.append(hrs, el("span", " h "), b);
      }
      tr.appendChild(td); tb.appendChild(tr); });
  }

  async function buy(x, hours) {
    const total = (Number(x.price) * hours).toFixed(2);
    sh.sheet("Buy " + hours + " h of exit time?", x.seller, ["about " + total + " USDC plus the network fee", "paid directly to the seller from your own wallet", "final: no refund, even if the exit doesn't work"],
      [["Get payment details", async () => {
        let r = await call("quote", {seller: x.seller, hours, chain: q("chain").value});
        if (r.need_ack) { if (!(await needAck(r))) return; r = await call("quote", {seller: x.seller, hours, chain: q("chain").value}); }
        if (!r.ok) { log("no quote: " + r.err); return; }
        showPay(r); watch(r.id); }], ["Cancel", async () => {}]]);
  }

  async function showPay(r) {
    const p = q("pay"); p.replaceChildren(); p.hidden = false;
    p.appendChild(el("h3", "Pay " + r.amount_text + " USDC on " + r.chain));
    const t = el("table"); t.className = "kv";
    const row = (k, v, copy) => { const tr = el("tr"); tr.appendChild(el("th", k)); const td = el("td", v); if (copy) { td.appendChild(document.createTextNode(" ")); td.appendChild(copyBtn(v)); } tr.appendChild(td); t.appendChild(tr); };
    row("Send exactly", r.amount_text + " USDC", false); row("Amount", r.amount_text, true); row("To", r.address, true);
    if (r.price_text) row("Includes", r.price_text + " USDC for " + r.hours + " h + " + r.extra_text + " USDC payment ID", false);
    row("Network", r.chain + " (chain id " + r.chainid + ")"); row("USDC contract", r.token_address); row("Good until", hhmm(r.expires));
    p.appendChild(t);
    if (r.qr) {
      const res = await fetch("/api/addon/" + NAME + "/qr.svg?q=" + encodeURIComponent(r.id), {headers: {"X-Core": "1"}});
      if (res.ok) { const img = el("img"); img.alt = "payment QR"; img.width = 220; img.height = 220; img.className = "qr"; img.src = URL.createObjectURL(await res.blob()); p.appendChild(img); }
    }
    const link = el("p", r.uri, "muted"); p.appendChild(link);
    p.appendChild(el("p", "Send that exact amount on " + r.chain + ". A different amount or another chain can't be matched to you. You pay the network fee.", "warn"));
    const stl = el("p", "Waiting for your payment…"); stl.dataset.x = "paystate"; p.appendChild(stl);
    const tx = el("input"); tx.size = 66; tx.placeholder = "transaction hash (only if it isn't picked up)";
    const tb = el("button", "I paid: check this tx"); tb.addEventListener("click", async () => {
      const a = await call("paid", {id: r.id, tx: tx.value.trim()}); log(a.ok ? "checking " + tx.value.trim().slice(0, 12) + "…" : a.err); if (a.ok) watch(r.id); });
    p.append(tx, tb);
  }

  function watch(id) {
    clearInterval(polls[id]);
    polls[id] = setInterval(async () => {
      const r = await call("watch", {id});
      const stl = q("paystate");
      const say = (m) => { if (stl) stl.textContent = m; };
      if (!r.ok) { say(r.err); clearInterval(polls[id]); delete polls[id]; return; }
      if (r.state === "watching") say("Waiting for your payment…" + (r.left ? " " + left(r.left) + " left on this quote." : "") + (r.near && r.near.length ? " Seen other amounts: " + r.near.join(", ") + " USDC (not a match)." : "") + (r.message ? " (" + r.message + ")" : ""));
      else if (r.state === "seen" || r.state === "confirming") say("Payment seen" + (r.tx ? " (" + r.tx.slice(0, 12) + "…)" : "") + ". Waiting for the seller to confirm…");
      else {
        clearInterval(polls[id]); delete polls[id];
        if (r.state === "admitted") { say("Paid. Exit time runs until " + (r.lease_end ? hhmm(r.lease_end) : "?") + ". Press Connect above."); log("paid: quote " + id); }
        else { say(r.message || ("Quote " + r.state)); log("quote " + id + ": " + (r.message || r.state)); }
        status();
      }
    }, 5000);
  }

  async function act(name, body) {
    log(name + "…");
    let r = await call(name, body);
    if (r.need_ack) { if (!(await needAck(r))) return; r = await call(name, body); }
    log((r.ok ? "ok " : "failed ") + (r.err || "") + (r.out ? "\n" + r.out : ""));
    status();
  }

  window.SRNetAddons = window.SRNetAddons || {};
  window.SRNetAddons[NAME] = {
    mount(section, shell) { root = section; sh = shell; build(); },
    show() { board(); status(); clearInterval(statusTimer); statusTimer = setInterval(status, 30000); },
    hide() { clearInterval(statusTimer); },
  };
})();
