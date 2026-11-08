/* Riffle Pro: licence tab, upgrade dialog, trial chip, per-set duplicate removal,
   monthly auto clean-up switch, growth history in Insights. Loaded last. */
"use strict";
let LIC = null;
const PRO_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.6 6.6 19.5l1.2-6L3.3 9.3l6.1-.7z"/></svg>';
const FEATURE_LINE = {
  visual:"Visual search is part of Riffle Pro.", "dupes-bulk":"Removing every extra copy at once is part of Riffle Pro.",
  "clean-all":"Cleaning this card is part of Riffle Pro. Temp files, caches and the Recycle Bin stay free.",
  auto:"Automatic monthly clean-up is part of Riffle Pro.", history:"Disk growth history is part of Riffle Pro."
};
const isPro = () => LIC && (LIC.tier === "pro" || LIC.tier === "trial");

async function loadLicense(){
  LIC = await api("/api/license").catch(() => null);
  paintChip();
  if ($("#set").classList.contains("on")) paintPro();
  return LIC;
}

/* ── any 402 from the server opens the upgrade dialog ────────── */
const _fetch = window.fetch.bind(window);
window.fetch = async (...a) => {
  const r = await _fetch(...a);
  if (r.status === 402){
    r.clone().json().then(j => showUpsell(j.feature)).catch(() => showUpsell());
  }
  return r;
};

/* ── upgrade dialog ─────────────────────────────────────────── */
document.body.insertAdjacentHTML("beforeend", `
<div class="dlg upsell" id="upsell" role="dialog" aria-modal="true" aria-labelledby="upT">
  <div class="box">
    <div class="up-top"><span class="logo">Riffle</span><span class="pill lbl-review">Pro</span></div>
    <h2 id="upT">Do more with <span class="mk">Riffle Pro</span></h2>
    <p class="up-why" id="upWhy"></p>
    <ul class="up-list" id="upList"></ul>
    <div class="up-price" id="upPrice"></div>
    <div class="up-btns">
      <button class="btn" id="upKey">I have a key</button>
      <button class="btn ink big" id="upBuy">Get Riffle Pro</button>
    </div>
    <p class="hint up-free">Search, the quick bar, preview, Insights and every scan stay free, forever.</p>
  </div>
</div>`);
function showUpsell(feature){
  if (!LIC) return loadLicense().then(() => showUpsell(feature));
  $("#upWhy").textContent = FEATURE_LINE[feature] || "Riffle Pro adds visual search, bulk clean-up and disk history.";
  $("#upList").innerHTML = LIC.features.map(f => `<li>${PRO_ICON}<span><b>${esc(f.title)}</b>${esc(f.desc)}</span></li>`).join("");
  const p = LIC.prices;
  $("#upPrice").innerHTML = `<b>₹${p.inrLaunch}</b> <s>₹${p.inr}</s> · <b>$${p.usdLaunch}</b> <s>$${p.usd}</s>
    <span>one-time, for 3 PCs · launch price</span>`;
  $("#upsell").classList.add("on");
  $("#upBuy").focus();
}
const closeUpsell = () => $("#upsell").classList.remove("on");
$("#upsell").addEventListener("click", e => { if (e.target.id === "upsell") closeUpsell(); });
$("#upsell").addEventListener("keydown", e => { if (e.key === "Escape"){ e.stopPropagation(); closeUpsell(); } });
$("#upBuy").onclick = () => { api("/api/license/buy", {}); toast("Opening the Riffle Pro page in your browser"); };
$("#upKey").onclick = () => { closeUpsell(); openSettings("pro"); setTimeout(() => $("#pKey") && $("#pKey").focus(), 150); };

/* ── trial / Pro chip in the footer ─────────────────────────── */
$(".foot").insertAdjacentHTML("beforeend", `<button class="prochip" id="proChip" hidden></button>`);
function paintChip(){
  const c = $("#proChip");
  if (!LIC){ c.hidden = true; return; }
  c.hidden = false;
  c.className = "prochip " + LIC.tier;
  c.textContent = LIC.tier === "pro" ? "Pro" : LIC.tier === "trial"
    ? `Pro trial · ${LIC.trialDaysLeft} day${LIC.trialDaysLeft === 1 ? "" : "s"} left` : "Get Pro";
}
$("#proChip").onclick = () => LIC && LIC.tier === "free" ? showUpsell() : openSettings("pro");

/* ── Settings › Riffle Pro ──────────────────────────────────── */
paintSettings = (orig => function(){ orig(); paintPro(); })(paintSettings);
function paintPro(){
  const pane = $("#sp-pro"); if (!pane || !LIC) return;
  const L = LIC, p = L.prices;
  const head = L.tier === "pro"
    ? `<div class="pro-state on"><b>Riffle Pro is active</b><span>${esc(L.email || "")}${L.plan === "business" ? " · business licence" : ""}</span></div>`
    : L.tier === "trial"
    ? `<div class="pro-state trial"><b>Free trial: ${L.trialDaysLeft} day${L.trialDaysLeft === 1 ? "" : "s"} left</b><span>Everything in Pro is switched on while it lasts.</span></div>`
    : `<div class="pro-state"><b>You're on Riffle Free</b><span>Search, the quick bar, preview, Insights and every scan are yours to keep.</span></div>`;
  pane.innerHTML = head
    + `<ul class="up-list">${L.features.map(f => `<li>${PRO_ICON}<span><b>${esc(f.title)}</b>${esc(f.desc)}</span></li>`).join("")}</ul>`
    + (L.tier === "pro"
      ? row("This PC", "Move Pro to another PC by deactivating it here first. Each key works on 3 PCs.",
            `<button class="btn" id="pDeact">Deactivate</button>`)
      : `<div class="pro-buy"><div><div class="up-price"><b>₹${p.inrLaunch}</b> <s>₹${p.inr}</s> · <b>$${p.usdLaunch}</b> <s>$${p.usd}</s></div>
           <span class="hint">One payment, yours for life, on up to 3 PCs. UPI in India, PayPal everywhere else.</span></div>
           <button class="btn ink big" id="pBuy">Get Riffle Pro</button></div>
         <div class="sgroup">Already bought it?</div>
         <div class="pro-key"><textarea id="pKey" rows="3" spellcheck="false" placeholder="Paste your licence key: RIFFLE1.…" aria-label="Licence key"></textarea>
           <button class="btn ink" id="pAct">Activate</button></div>
         <p class="hint" id="pMsg" role="status"></p>`)
    + `<p class="hint" style="margin-top:14px">Activating sends only the key and this PC's name to riffle.bliink.in, once. Riffle never sends anything about your files.</p>`;
}
$("#set").addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.id === "pBuy"){ api("/api/license/buy", {}); toast("Opening the Riffle Pro page in your browser"); }
  if (b.id === "pAct"){
    const key = $("#pKey").value.trim();
    if (!key) return $("#pKey").focus();
    b.disabled = true; $("#pMsg").textContent = "Activating…";
    const r = await api("/api/license/activate", { key }).catch(() => ({ ok:false, error:"Riffle didn't answer. Try again." }));
    b.disabled = false;
    if (r.ok){ toast("Riffle Pro is active. Thank you!"); await loadLicense(); pollVisual(); }
    else $("#pMsg").textContent = r.error;
  }
  if (b.id === "pDeact" && await ask("Deactivate Riffle Pro on this PC?", "Deactivate", "You can activate the same key again here or on another PC.", false)){
    await api("/api/license/deactivate", {}); await loadLicense(); toast("Deactivated on this PC");
  }
});
// Settings opens on General; Riffle Pro is the first tab but not the default
showTab("general");

/* ── visual search: free tier sees what it does and how to get it ── */
paintVisual = (orig => function(s){
  if (s.pro === false || (LIC && !isPro())){
    $("#visPick").hidden = $("#visPath").hidden = $("#visSuggest").hidden = true;
    $("#visFolders").innerHTML = "";
    $("#visStat").innerHTML = `Find the original of any photo or video, even after crops, filters or text, across your folders.
      <br><button class="btn ink" id="visPro" style="margin-top:8px">${PRO_ICON} Get Riffle Pro</button>`;
    $("#visPro").onclick = () => showUpsell("visual");
    return;
  }
  orig(s);
})(paintVisual);

/* ── duplicates: free tier removes one set at a time ────────── */
$("#dOut").addEventListener("click", async e => {
  const b = e.target.closest("[data-gset]"); if (!b) return;
  const g = dupes.groups.find(x => x.hash === b.dataset.gset); if (!g) return;
  const paths = g.files.filter(f => dupes.picked.has(f.path)).map(f => f.path);
  if (!paths.length) return toast("Tick the copies to remove first");
  const r = await api("/api/dupes/remove", { paths });
  if (r.removed){ toast(`Recycled ${r.removed} file${r.removed === 1 ? "" : "s"} · ${fmtSize(r.freed)} freed`); dupes.load(); }
}, true);
dupes.paint = (orig => function(){
  orig.call(dupes);
  if (LIC && !isPro()){
    const big = $("#dOut [data-d=recycle]");
    if (big){ big.innerHTML = `${PRO_ICON} Recycle all extras`; big.classList.add("is-pro"); }
  }
})(dupes.paint);

/* ── clean up: Pro marks and the monthly auto clean-up switch ── */
const FREE_CARDS = new Set(["temp", "caches", "bin"]);
clean.paint = (orig => function(){
  orig.call(clean);
  if (LIC && !isPro())
    document.querySelectorAll("#cOut .ccard").forEach(c => {
      if (FREE_CARDS.has(c.dataset.k)) return;
      const b = c.querySelector("[data-c=clean]"); if (b && !b.classList.contains("is-pro")){ b.classList.add("is-pro"); b.insertAdjacentHTML("afterbegin", PRO_ICON + " "); }
    });
  paintAuto();
})(clean.paint);
$("#cScan").closest(".ctrls").insertAdjacentHTML("afterend", `<label class="qs auto-qs" id="autoBox"><label class="sw"><input type="checkbox" id="autoOn"><i></i></label>
  <span>Clean temp files and caches every month<small id="autoInfo">Riffle Pro</small></span></label>`);
function paintAuto(){
  if (!LIC) return;
  const a = LIC.autoClean || {};
  $("#autoOn").checked = !!a.on && isPro();
  $("#autoInfo").textContent = !isPro() ? "Riffle Pro" : a.last
    ? `Last run ${new Date(a.last * 1000).toLocaleDateString()} · freed ${fmtSize(a.freed || 0)}` : a.on ? "First run within a day" : "Off";
}
$("#autoOn").addEventListener("change", async e => {
  if (!isPro()){ e.target.checked = false; return showUpsell("auto"); }
  const r = await api("/api/autoclean", { on:e.target.checked });
  LIC.autoClean = r; paintAuto(); toast(r.on ? "Monthly clean-up is on" : "Monthly clean-up is off");
});

/* ── Insights: growth history (Pro) ─────────────────────────── */
paintInsights = (orig => function(){
  orig();
  const d = ins.data; if (!d) return;
  const grid = $("#insOut .igrid"); if (!grid) return;
  let h = `<div class="icard wide hist-card"><h3>Disk growth<span class="sp"></span>${d.historyLocked ? `<span class="pill lbl-review">Pro</span>` : ""}</h3>`;
  if (d.historyLocked){
    h += `<p class="sub">${d.historyDays ? `${d.historyDays} day${d.historyDays === 1 ? "" : "s"} recorded so far.` : "Recording starts today."}</p>
      <div class="hist-lock"><svg viewBox="0 0 600 120" preserveAspectRatio="none" aria-hidden="true"><path d="M0 100 C80 95 120 80 200 78 S330 60 400 52 S520 30 600 20 L600 120 L0 120Z"/></svg>
      <button class="btn ink" id="histPro">${PRO_ICON} See your history with Riffle Pro</button></div>`;
  } else if ((d.history || []).length < 2){
    h += `<p class="sub">The trend shows after a few days.</p>`;
  } else {
    const H = d.history, W = 900, Ht = 160, pad = 4;
    const max = Math.max(...H.map(r => r.bytes)), min = Math.min(...H.map(r => r.bytes));
    const lo = Math.max(0, min - (max - min) * 0.2), hi = max + (max - min) * 0.1 || max + 1;
    const x = i => pad + i * (W - 2 * pad) / (H.length - 1), y = v => Ht - pad - (v - lo) / (hi - lo) * (Ht - 2 * pad);
    const line = H.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(r.bytes).toFixed(1)}`).join(" ");
    const first = H[0], last = H[H.length - 1], delta = last.bytes - first.bytes;
    h += `<p class="sub">${fmtSize(Math.abs(delta))} ${delta >= 0 ? "more" : "less"} than on ${new Date(first.day).toLocaleDateString()}.</p>
      <div class="hist-chart"><svg viewBox="0 0 ${W} ${Ht}" preserveAspectRatio="none" role="img" aria-label="Space used per day">
        <path class="area" d="${line} L${x(H.length - 1)} ${Ht} L${x(0)} ${Ht}Z"/><path class="ln" d="${line}"/>
        ${H.map((r, i) => `<rect x="${x(i) - 6}" y="0" width="12" height="${Ht}" fill="transparent" data-tip="<b>${fmtSize(r.bytes)}</b><br><span>${new Date(r.day).toLocaleDateString()} · ${r.files.toLocaleString()} files</span>"/>`).join("")}
      </svg></div>`;
  }
  grid.insertAdjacentHTML("afterbegin", h + `</div>`);
  const hp = $("#histPro"); if (hp) hp.onclick = () => showUpsell("history");
})(paintInsights);

loadLicense();
window.addEventListener("focus", () => { if (Date.now() - (window._licAt || 0) > 60000){ window._licAt = Date.now(); loadLicense(); } });
