/* Riffle 1.2 window: simple by default, power one click away.
   Loaded last. It moves the existing controls (same ids, same listeners) out of the rail into
   menus under the search box, and adds: List view, a start screen, "also inside N files",
   Search / Image / Voice modes in the search box (← → to switch), and a calmer preview. */
"use strict";
(() => {
const $$ = s => [...document.querySelectorAll(s)];
const SVG = {
  search:'<svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="7"/><path d="M21 21l-5.6-5.6"/></svg>',
  image:'<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="15.5" cy="9.5" r="1.6"/><path d="M3 17l5-5 4 4 3-3 6 6"/></svg>',
  mic:'<svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3"/></svg>',
  list:'<svg viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/></svg>',
  table:'<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="1"/><path d="M3 10h18M3 15h18M9 4v16"/></svg>',
};
const NO_DATE = 315619200;                   // 2 Jan 1980: anything earlier is "no date" (zip/DOS default)
const rel = t => {
  if (!t || t < NO_DATE) return "—";
  const s = Date.now() / 1000 - t, d = new Date(t * 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400 && d.getDate() === new Date().getDate()) return "Today, " + d.toLocaleTimeString(undefined, { hour:"2-digit", minute:"2-digit" });
  if (s < 2 * 86400) return "Yesterday";
  if (s < 7 * 86400) return d.toLocaleDateString(undefined, { weekday:"long" });
  if (s < 300 * 86400) return d.toLocaleDateString(undefined, { day:"numeric", month:"short" });
  return d.toLocaleDateString(undefined, { day:"numeric", month:"short", year:"numeric" });
};
/* "C:\Users\Hp\Documents\RAVI\riffle" -> "Documents › RAVI › riffle" (last three folders) */
function friendly(dir){
  let d = dir || "";
  const m = /^[A-Za-z]:\\Users\\[^\\]+\\?/.exec(d);
  if (m) d = d.slice(m[0].length);
  const parts = d.split("\\").filter(Boolean);
  if (!parts.length) return m ? "Home" : dir;
  return (parts.length > 3 ? "… › " : "") + parts.slice(-3).join(" › ");
}

/* ── stash: originals that stay as the source of truth but out of sight ── */
document.body.insertAdjacentHTML("beforeend", `<div id="v2stash"></div>`);
const stash = $("#v2stash");

/* ── 1. search box: Search · Image · Voice, and the Aa options ───────────── */
$(".qwrap").insertAdjacentHTML("beforeend", `
  <canvas id="qviz" width="380" height="60" aria-hidden="true"></canvas>
  <div class="qmodes" role="group" aria-label="Search mode (← → when the box is empty)">
    <button data-qm="search" aria-pressed="true" title="Search names and text inside files">${SVG.search}</button>
    <button data-qm="image" aria-pressed="false" title="Find the original of a picture">${SVG.image}</button>
    <button data-qm="voice" aria-pressed="false" title="Voice search">${SVG.mic}</button>
  </div>`);
$(".qwrap").insertAdjacentHTML("afterend", `<button class="icobtn" id="aaBtn" aria-haspopup="true" title="Search options: how names match, hidden files">Aa</button>`);
$("#visBtn").hidden = true;                     // image search is a mode of the box now
$("#q").placeholder = "Search files, apps' files and words inside documents";

/* ── 2. the filter row ───────────────────────────────────────────────────── */
const TYPES = [["document", "Documents"], ["image", "Pictures"], ["video", "Videos"], ["audio", "Music"]];
const MORE_CATS = [["code", "Code"], ["archive", "Archives"], ["other", "Other"]];
$(".main").insertAdjacentHTML("afterbegin", `
  <div class="frow" role="toolbar" aria-label="Filters">
    <button class="chip" data-f="all">All</button>
    ${TYPES.map(([k, l]) => `<button class="chip" data-f="cat" data-cat2="${k}">${l} <span class="n"></span></button>`).join("")}
    <button class="chip" data-f="folders">Folders</button>
    <span class="sp"></span>
    <label class="sr" for="v2sort" style="position:absolute;left:-9999px">Sort</label>
    <select id="v2sort" aria-label="Sort">
      <option value="best">Best match</option><option value="modified">Newest first</option>
      <option value="name">Name</option><option value="size">Biggest first</option><option value="path">Folder</option>
    </select>
    <div class="vtog" role="group" aria-label="View">
      <button data-v2view="list" title="List">${SVG.list}</button>
      <button data-v2view="details" title="Details: the table, with exact sizes and full paths">${SVG.table}</button>
    </div>
  </div>
  <div class="inote" id="inote" role="status"></div>`);

/* menus */
document.body.insertAdjacentHTML("beforeend", `
  <div class="pop" id="mAa" role="dialog" aria-label="Search options">
    <h4>How names match</h4>
    ${[["substring", "Smart", "Names with your words. Also lists files with the words inside."],
       ["fuzzy", "Close enough", "Letters in order: rpt finds report."],
       ["regex", "Pattern", "Regular expressions, e.g. ^IMG_\\d{4}"],
       ["content", "Inside files only", "Only documents that contain your words."]].map(([m, b, s]) =>
      `<button class="opt2" data-pm="${m}"><span class="dot2"></span><span><b>${b}</b><small>${s}</small></span></button>`).join("")}
    <h4>Also show</h4><div id="aaQs"></div>
  </div>
`);

/* filters live in the sidebar as accordions; Modified and Where start open */
$(".rail .nav").insertAdjacentHTML("afterend", `<div class="acc" aria-label="Filters">
  <details id="accTime" open><summary><span class="t">Modified</span><span class="v" id="ddTime">Any time</span></summary>
    <div class="accin" id="mTimeIn"></div></details>
  <details id="accWhere" open><summary><span class="t">Where</span><span class="v" id="ddWhere">Anywhere</span></summary>
    <div class="accin" id="mWhereIn"><h4>Only in this folder</h4></div></details>
  <details id="accSize"><summary><span class="t">Size</span><span class="v" id="ddSize">Any size</span></summary>
    <div class="accin"><div class="chips" id="sizeChips">
      ${[["", "Any size"], ["0-1048576", "Under 1 MB"], ["1048576-104857600", "1 – 100 MB"],
         ["104857600-1073741824", "100 MB – 1 GB"], ["1073741824-", "Over 1 GB"]].map(([v, l]) =>
        `<button class="chip" data-sz="${v}">${l}</button>`).join("")}
    </div><div id="mSizeIn"></div></div></details>
  <details id="accTypes"><summary><span class="t">More types</span><span class="v" id="ddTypes">All</span></summary>
    <div class="accin"><h4>Kinds</h4><div class="chips" id="moreCats"></div><h4>Extensions</h4><div id="mExt"></div></div></details>
  <button class="clear2" id="v2clear" hidden>Clear all filters</button>
</div>`);
$("#v2clear").onclick = () => {
  $("#clearAll").click();
  $$("#sizeChips .chip").forEach(c => c.setAttribute("aria-pressed", "false"));
  setTimeout(syncChips);
};
try{ const o = JSON.parse(localStorage.getItem("riffle.v2.acc") || "{}");
     for (const [id, on] of Object.entries(o)) if ($("#" + id)) $("#" + id).open = on; }catch(e){}
$$(".acc details").forEach(d => d.addEventListener("toggle", () => {
  try{ localStorage.setItem("riffle.v2.acc", JSON.stringify(Object.fromEntries($$(".acc details").map(x => [x.id, x.open])))); }catch(e){}
}));

/* move the originals into the menus (ids and listeners come along) */
$("#mExt").append($("#extField"), $("#extChips"));
$("#mTimeIn").append($("#dateChips"), $("#tHist").closest(".scope"));
$("#mSizeIn").append($("#sHist").closest(".scope"));
$("#mWhereIn").append($("#scopeField"));
$("#mWhereIn").insertAdjacentHTML("beforeend", `<h4 style="margin-top:6px">Leave out folders containing</h4>`);
$("#mWhereIn").append($("#excludeField"));
if ($("#cloudGrp")) $("#mWhereIn").append($("#cloudGrp"));
$("#scopeField").placeholder = "e.g. C:\\Users\\you\\Documents";
$("#excludeField").placeholder = "e.g. node_modules, backup";
const qs = document.querySelector(".rail .qs");
if (qs) $("#aaQs").append(qs);
stash.append($(".top .seg"), $("#kindChips"), $("#catChips"), $("#clearAll"));
$$(".rail > .grp").forEach(g => { if (g.id !== "visGrp") stash.append(g); });

/* popover plumbing */
let openPop = null;
function pop(id, anchor){
  const m = $("#" + id);
  if (openPop === m){ closePop(); return; }
  closePop();
  const r = anchor.getBoundingClientRect();
  m.classList.add("on");
  const w = m.offsetWidth;
  m.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.left)) + "px";
  m.style.top = (r.bottom + 6) + "px";
  openPop = m;
  syncAa(); syncMoreCats();
}
function closePop(){ if (openPop){ openPop.classList.remove("on"); openPop = null; } }
document.addEventListener("pointerdown", e => {
  if (openPop && !openPop.contains(e.target) && !e.target.closest("[data-pop],#aaBtn")) closePop();
});
document.addEventListener("keydown", e => { if (e.key === "Escape" && openPop){ closePop(); e.stopPropagation(); } }, true);
$(".frow").addEventListener("click", e => { const b = e.target.closest("[data-pop]"); if (b) pop(b.dataset.pop, b); });
$("#aaBtn").onclick = e => pop("mAa", e.currentTarget);

/* Aa: proxies for the original mode buttons */
function syncAa(){ $$("#mAa [data-pm]").forEach(b => b.setAttribute("aria-pressed", b.dataset.pm === state.mode)); }
$("#mAa").addEventListener("click", e => {
  const b = e.target.closest("[data-pm]"); if (!b) return;
  const orig = document.querySelector(`.seg [data-mode="${b.dataset.pm}"]`);
  if (orig) orig.click();
  syncAa(); $("#aaBtn").classList.toggle("on", state.mode !== "substring");
});

/* type chips: proxies for the original category and kind chips */
const realCat = k => document.querySelector(`#catChips [data-cat="${k}"]`);
const realKind = k => document.querySelector(`#kindChips [data-kind="${k}"]`);
$(".frow").addEventListener("click", e => {
  const b = e.target.closest("[data-f]"); if (!b) return;
  if (b.dataset.f === "all"){
    for (const k of [...state.cats]) realCat(k)?.click();
    if (state.kind !== "all") realKind("all")?.click();
  } else if (b.dataset.f === "folders"){
    realKind(state.kind === "folders" ? "all" : "folders")?.click();
  } else realCat(b.dataset.cat2)?.click();
  syncChips();
});
$("#moreCats").innerHTML = MORE_CATS.map(([k, l]) => `<button class="chip" data-mc="${k}">${l} <span class="n"></span></button>`).join("")
  + `<button class="chip" data-mk="files">Files only</button>`;
$("#moreCats").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.mc) realCat(b.dataset.mc)?.click();
  if (b.dataset.mk) realKind(state.kind === "files" ? "all" : "files")?.click();
  syncMoreCats(); syncChips();
});
function syncMoreCats(){
  $$("#moreCats [data-mc]").forEach(b => {
    const r = realCat(b.dataset.mc);
    b.setAttribute("aria-pressed", state.cats.has(b.dataset.mc));
    b.querySelector(".n").textContent = r ? r.querySelector(".n").textContent : "";
    b.disabled = !!(r && r.disabled);
  });
  const f = $("#moreCats [data-mk]"); if (f) f.setAttribute("aria-pressed", state.kind === "files");
}
function syncChips(){
  $$(".frow [data-cat2]").forEach(b => {
    const r = realCat(b.dataset.cat2);
    b.setAttribute("aria-pressed", state.cats.has(b.dataset.cat2));
    b.querySelector(".n").textContent = r ? r.querySelector(".n").textContent : "";
    b.disabled = !!(r && r.disabled);
  });
  const any = state.cats.size || state.kind !== "all";
  $(".frow [data-f=all]").setAttribute("aria-pressed", !any);
  $(".frow [data-f=folders]").setAttribute("aria-pressed", state.kind === "folders");
  const extra = MORE_CATS.filter(([k]) => state.cats.has(k)).length + state.exts.length + (state.kind === "files" ? 1 : 0);
  const dd = (id, on, label) => { const b = $("#" + id); b.textContent = label; b.classList.toggle("on", on); };
  dd("ddTypes", !!extra, extra ? `${extra} chosen` : "All");
  const dchip = $$("#dateChips .chip").find(c => c.getAttribute("aria-pressed") === "true");
  dd("ddTime", state.timeMin != null, state.timeMin == null ? "Any time" : dchip ? (dchip.textContent === "Year" ? "This year" : "Last " + dchip.textContent.replace("Today", "day").replace(" days", " days")) : $("#tLabel").textContent);
  const sz = $$("#sizeChips .chip").find(c => c.getAttribute("aria-pressed") === "true" && c.dataset.sz);
  dd("ddSize", state.sizeMin != null || state.sizeMax != null, sz ? sz.textContent : state.sizeMin == null && state.sizeMax == null ? "Any size" : $("#sLabel").textContent);
  const where = state.scope ? "In " + state.scope.split("\\").filter(Boolean).pop() : state.exclude ? "Not in " + state.exclude : state.cloud ? "Cloud" : "Anywhere";
  dd("ddWhere", !!(state.scope || state.exclude || state.cloud), where);
  $("#aaBtn").classList.toggle("on", state.mode !== "substring");
  $("#v2clear").hidden = !(any || extra || state.timeMin != null || state.sizeMin != null || state.sizeMax != null
                           || state.scope || state.exclude || state.cloud);
}
paintFacets = (orig => function(c){ orig(c); syncChips(); syncMoreCats(); })(paintFacets);
$("#dateChips").addEventListener("click", () => setTimeout(syncChips));
$("#sizeChips").addEventListener("click", e => {
  const b = e.target.closest("[data-sz]"); if (!b) return;
  const [lo, hi] = b.dataset.sz.split("-");
  $$("#sizeChips .chip").forEach(c => c.setAttribute("aria-pressed", c === b && !!b.dataset.sz));
  state.sizeMin = lo ? +lo : (b.dataset.sz ? 0 : null);
  state.sizeMax = hi ? +hi : null;
  if (!b.dataset.sz){ state.sizeMin = state.sizeMax = null; }
  state.sBrush = null; sH.paint(null); $("#sReset").hidden = !b.dataset.sz;
  updateBrushLabels(); scheduleSearch(0); syncChips();
});
$("#sReset").addEventListener("click", () => { $$("#sizeChips .chip").forEach(c => c.setAttribute("aria-pressed", "false")); setTimeout(syncChips); });
$("#tReset").addEventListener("click", () => setTimeout(syncChips));
["#scopeField", "#excludeField", "#extField"].forEach(s => $(s).addEventListener("input", () => setTimeout(syncChips, 300)));

/* sort */
$("#v2sort").value = state.sortPicked ? state.sort : "best";
$("#v2sort").onchange = e => {
  const v = e.target.value;
  state.sortPicked = v !== "best";
  if (v !== "best"){ state.sort = v; state.desc = v === "modified" || v === "size"; }
  $$("[data-sort]").forEach(x => { x.classList.toggle("act", x.dataset.sort === state.sort); x.classList.toggle("dsc", x.dataset.sort === state.sort && state.desc); });
  savePrefs(); scheduleSearch(0);
};

/* ── 3. List / Details ───────────────────────────────────────────────────── */
function setListView(v){
  const list = v !== "details";
  document.body.classList.toggle("v2list", list);
  ROW = list ? 52 : 30;
  document.documentElement.style.setProperty("--row", ROW + "px");
  $$(".vtog button").forEach(b => b.setAttribute("aria-pressed", b.dataset.v2view === (list ? "list" : "details")));
  $("#spacer").style.height = (state.total * ROW) + "px";
  try{ localStorage.setItem("riffle.v2.view", list ? "list" : "details"); }catch(e){}
  render();
}
$(".vtog").addEventListener("click", e => { const b = e.target.closest("[data-v2view]"); if (b) setListView(b.dataset.v2view); });
render = (orig => function(){
  orig();
  if (!document.body.classList.contains("v2list")) return;
  for (const el of $$("#rows .row[data-i]:not(.skel)")){
    const r = rowAt(+el.dataset.i); if (!r) continue;
    const p = el.querySelector(".c-path"), m = el.querySelector(".c-mod");
    if (p && !p.dataset.v2){ p.dataset.v2 = 1; p.title = r.dir; p.textContent = friendly(r.dir); }
    if (m && !m.dataset.v2){ m.dataset.v2 = 1; m.title = new Date(r.mtime * 1000).toLocaleString(); m.textContent = rel(r.mtime); }
  }
})(render);
let view0 = "list";
try{ view0 = localStorage.getItem("riffle.v2.view") || "list"; }catch(e){}

/* ── 4. "also inside N files" ────────────────────────────────────────────── */
let inoteTimer = 0, inoteTok = 0;
function inote(){
  clearTimeout(inoteTimer);
  const n = $("#inote"), q = state.q.trim();
  if (state.mode === "content" && q){
    n.innerHTML = `Showing documents with “${esc(q)}” inside. <button class="btn" data-in="names">Search names too</button>`;
    n.classList.add("on"); return;
  }
  n.classList.remove("on");
  if (state.mode !== "substring" || q.length < 3 || vis.on) return;
  const tok = ++inoteTok;
  inoteTimer = setTimeout(async () => {
    const d = await fetch("/api/search", { method:"POST", headers:{ "Content-Type":"application/json" },
      body:JSON.stringify({ q, mode:"content", filters:filters(), sort:"best", desc:false, offset:0, limit:1 }) })
      .then(r => r.json()).catch(() => null);
    if (!d || tok !== inoteTok || state.q.trim() !== q || state.mode !== "substring" || !d.total) return;
    n.innerHTML = `“${esc(q)}” also appears inside <b>${d.total.toLocaleString()} ${d.total === 1 ? "document" : "documents"}</b>.
      <button class="btn" data-in="inside">Show them</button>`;
    n.classList.add("on");
  }, 450);
}
$("#inote").addEventListener("click", e => {
  const b = e.target.closest("[data-in]"); if (!b) return;
  document.querySelector(`.seg [data-mode="${b.dataset.in === "inside" ? "content" : "substring"}"]`)?.click();
});
runSearch = (orig => async function(){
  await orig();
  if (!state.q.trim() && state.sel >= 0){ state.sel = -1; state.anchor = -1; render(); showPreview(true); }  // nothing chosen yet: no preview
  inote(); syncChips();
})(runSearch);

/* ── 5. start screen ─────────────────────────────────────────────────────── */
$("#scroll").insertAdjacentHTML("beforebegin", `<div id="home" aria-label="Start">
  <section><div class="hh"><h2>Recent files</h2><span>New or changed on this PC</span>
    <button class="lnk2" id="hAllRecent">All recent files →</button></div><ul class="hlist" id="hRecent"></ul></section>
  <section><div class="hh"><h2>Jump back in</h2><span>Files you opened with Riffle</span>
    <button class="lnk2" id="hHistory">Open history →</button></div><div id="hPicks"></div></section>
  <section><div class="hh"><h2>Quick finds</h2></div><div class="qfs" id="hQuick"></div></section>
  <section id="hSpace"><div class="hh"><h2>Free up space</h2></div><div class="spacecards">
    <button data-hv="clean"><span style="flex:1"><big id="hClean">Check</big><span>Temp files, caches and old installers you can safely remove</span></span></button>
    <button data-hv="dupes"><span style="flex:1"><big id="hDupes">Find</big><span>Exact copies of the same file. One of each is always kept</span></span></button>
  </div></section>
  <p class="htip">Tip: press <kbd>Ctrl</kbd> <kbd>Shift</kbd> <kbd>Space</kbd> in any app to search without this window. Type <kbd>?</kbd> there for help.</p>
</div>`);
const QUICK = [
  ["Photos this week", () => { realCat("image")?.click(); clickDate(7); }],
  ["Screenshots", () => typeQ("screenshot")],
  ["PDFs", () => setExts(["pdf"])],
  ["Downloads", () => setScope(HOME_DIR() + "\\Downloads")],
  ["Big files (over 1 GB)", () => $('#sizeChips [data-sz="1073741824-"]').click()],
  ["Changed today", () => clickDate(1)],
];
const HOME_DIR = () => (state.homeDir || "C:\\Users\\" + (state.user || "Public"));
const clickDate = d => { const c = document.querySelector(`#dateChips [data-days="${d}"]`); if (c && c.getAttribute("aria-pressed") !== "true") c.click(); syncChips(); };
const typeQ = q => { $("#q").value = q; $("#q").dispatchEvent(new Event("input")); };
const setExts = list => { $("#extField").value = list.join(", "); $("#extField").dispatchEvent(new Event("input")); };
const setScope = p => { $("#scopeField").value = p; $("#scopeField").dispatchEvent(new Event("input")); };
$("#hQuick").innerHTML = QUICK.map(([l], i) => `<button class="chip" data-q="${i}">${l}</button>`).join("");
$("#hQuick").onclick = e => { const b = e.target.closest("[data-q]"); if (b) QUICK[+b.dataset.q][1](); };
$("#hSpace").onclick = e => { const b = e.target.closest("[data-hv]"); if (b) setView(b.dataset.hv); };
$("#hAllRecent").onclick = () => { app.classList.add("allrecent"); render(); };
$("#q").addEventListener("input", () => { if (!$("#q").value) app.classList.remove("allrecent"); });
let histAll = false;
$("#hHistory").onclick = () => { histAll = !histAll; $("#hHistory").textContent = histAll ? "Fewer" : "Open history →"; paintPicks(); };
const openPath = p => fetch("/api/act", { method:"POST", headers:{ "Content-Type":"application/json" }, body:JSON.stringify({ action:"open", paths:[p], q:"" }) });
const iconFor2 = r => iconURL(r);              // same Windows icons as the result list
async function paintRecent(){
  if (!SET || SET.hideSystem === undefined) await loadSettings();     // filters() needs "hide system files"
  const d = await fetch("/api/search", { method:"POST", headers:{ "Content-Type":"application/json" },
    body:JSON.stringify({ q:"", mode:"substring", filters:{ ...filters(), kind:"files" },   // keeps "hide system files"
                          sort:"modified", desc:true, offset:0, limit:5 }) })
    .then(r => r.json()).catch(() => ({ rows:[] }));
  if (!state.homeDir && d.rows && d.rows[0]){ const m = /^([A-Za-z]:\\Users\\[^\\]+)/.exec(d.rows[0].dir); if (m) state.homeDir = m[1]; }
  $("#hRecent").innerHTML = (d.rows || []).map(r => `<li data-p="${esc((r.dir.endsWith("\\") ? r.dir : r.dir + "\\") + r.name)}" title="${esc(r.dir)}">
      <img src="${iconFor2(r)}" alt=""><span class="nm">${esc(r.name)}</span><span class="wh">${esc(friendly(r.dir))}</span><span class="tm">${rel(r.mtime)}</span></li>`).join("")
    || `<li class="hempty">Nothing yet. Riffle is still reading this PC.</li>`;
}
async function paintPicks(){
  const d = await fetch("/api/picks" + (histAll ? "?all=1" : "")).then(r => r.json()).catch(() => ({ items:[] }));
  const items = d.items || [];
  const box = $("#hPicks");
  if (!items.length){ box.innerHTML = `<div class="hempty">Files you open from Riffle show up here, so you can get back to them in one click.</div>`; return; }
  if (histAll){
    box.innerHTML = `<ul class="hlist">${items.map(r => `<li data-p="${esc(r.path)}" title="${esc(r.path)}"><img src="${iconFor2(r)}" alt="">
      <span class="nm">${esc(r.name)}</span><span class="wh">${esc(friendly(r.dir))}</span><span class="tm">Opened ${rel(r.opened)}</span></li>`).join("")}</ul>`;
    return;
  }
  const pic = r => r.cat === "image" || r.cat === "video";
  box.innerHTML = `<div class="hgrid">${items.slice(0, 8).map(r => `<button class="hcard" data-p="${esc(r.path)}" title="${esc(r.path)}">
    <span class="th">${pic(r) ? `<img src="${fileURL("thumb", r.path, 256)}" alt="" onerror="this.className='ico2';this.src='${fileURL("icon", r.path, 48)}'">`
      : `<img class="ico2" src="${iconFor2(r)}" alt="">`}</span>
    <b>${esc(r.name)}</b><small>Opened ${rel(r.opened)}</small></button>`).join("")}</div>`;
}
$("#home").addEventListener("click", e => { const el = e.target.closest("[data-p]"); if (el) openPath(el.dataset.p); });
let homeShown = false;
new MutationObserver(() => {
  const on = app.classList.contains("home") && !app.classList.contains("allrecent");
  if (on && !homeShown){ paintRecent(); paintPicks(); paintSpace(); }
  homeShown = on;
}).observe(app, { attributes:true, attributeFilter:["class"] });
function paintSpace(){
  const c = $("#navClean").textContent.trim(), d = $("#navDupes").textContent.trim();
  $("#hClean").textContent = c || "Check";
  $("#hDupes").textContent = d || "Find";
}

/* ── 6. modes: Search · Image · Voice (← → when the box is empty) ────────── */
$(".main").insertAdjacentHTML("beforeend", `
  <div class="mpanel" id="imgPanel"><div class="drop">${SVG.image}
    <h2>Find the original of a picture</h2>
    <p>Drop a picture or video here, paste one with <b>Ctrl+V</b>, or choose a file. Riffle finds where the original lives,
      other copies, and pictures that look alike, even after crops, filters or resizing.</p>
    <div class="row3"><button class="btn ink big" id="imgChoose">Choose a file… <kbd class="kb">↓</kbd></button></div></div></div>
  <div class="mpanel" id="voicePanel"><div class="drop">${SVG.mic}<h2 id="vpTitle">Voice search</h2><p id="vpText"></p>
    <div class="row3" id="vpBtns"></div></div></div>`);
$("#imgChoose").onclick = () => $("#visFile").click();
const MODES = ["search", "image", "voice"];
let qmode = "search";
function setQMode(m){
  if (qmode === "voice" && m !== "voice") voiceStop(true);
  qmode = m;
  MODES.forEach(x => app.classList.toggle("qm-" + x, x === m));
  $$(".qmodes [data-qm]").forEach(b => b.setAttribute("aria-pressed", b.dataset.qm === m));
  $("#voicePanel").classList.remove("on"); app.classList.remove("vp-on");
  if (m === "image"){ if (vis.on) showVisual(false); $("#q").placeholder = "Drop, paste or choose a picture below"; }
  else if (m === "voice"){ voiceStart(); }
  else { $("#q").placeholder = "Search files and words inside documents"; }
  $("#q").focus();
}
$(".qmodes").addEventListener("click", e => { const b = e.target.closest("[data-qm]"); if (b) setQMode(b.dataset.qm); });
$("#q").addEventListener("keydown", e => {
  if (qmode === "image" && (e.key === "ArrowDown" || e.key === "Enter")){   // quickest way to pick a picture
    e.preventDefault(); e.stopPropagation(); $("#visFile").click(); return;
  }
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  if ($("#q").value && qmode === "search") return;            // normal cursor movement
  e.preventDefault();
  setQMode(MODES[(MODES.indexOf(qmode) + (e.key === "ArrowRight" ? 1 : -1) + 3) % 3]);
});
$("#q").addEventListener("input", () => { if (qmode === "image" && $("#q").value) setQMode("search"); });
const visualSearch0 = visualSearch;
visualSearch = async function(f){ if (qmode !== "search") setQMode("search"); return visualSearch0(f); };

/* voice: the offline engine in Riffle; this page polls the words and the level */
let vPoll = 0, vLast = "", vLevels = [];
async function voiceStart(){
  const r = await fetch("/api/voice/start", { method:"POST", headers:{ "Content-Type":"application/json" }, body:"{}" })
    .then(x => x.json()).catch(() => ({ error:"Riffle didn't answer" }));
  if (!r.installed){
    $("#vpTitle").textContent = "Voice search isn't set up yet";
    $("#vpText").textContent = "Speak instead of typing. It works offline: your voice is turned into text on this PC and nothing is recorded or sent. One free download, 65 MB.";
    $("#vpBtns").innerHTML = `<button class="btn ink big" id="vpInstall">Set up voice search</button>`;
    $("#vpInstall").onclick = () => openSettings("general");
    $("#voicePanel").classList.add("on"); app.classList.add("vp-on");
    return;
  }
  vLast = ""; vLevels = [];
  $("#q").value = ""; state.q = "";
  $("#q").placeholder = "Listening… speak now";
  $(".qwrap").classList.add("listening");
  clearInterval(vPoll);
  vPoll = setInterval(voiceTick, 90);
}
async function voiceTick(){
  const s = await fetch("/api/voice/state").then(x => x.json()).catch(() => null);
  if (!s) return;
  vLevels = s.levels || [];
  drawViz();
  if (s.text !== vLast){
    vLast = s.text;
    $("#q").value = s.text; state.q = s.text;
    setView("search"); scheduleSearch(120);
  }
  if (!s.listening){
    voiceStop(false);
    if (s.error) toast("The microphone didn't start: " + s.error);
    else if (!s.text) toast("Didn't catch that. Press → to try again");
    qmode = "voice"; setQMode("search");
  }
}
function voiceStop(tell){
  clearInterval(vPoll); vPoll = 0;
  $(".qwrap").classList.remove("listening");
  $(".qwrap").style.removeProperty("--lvl");
  if (tell) fetch("/api/voice/stop", { method:"POST", headers:{ "Content-Type":"application/json" }, body:"{}" });
}
function drawViz(){
  const c = $("#qviz"), g = c.getContext("2d"), W = c.width, H = c.height;
  g.clearRect(0, 0, W, H);
  const css = getComputedStyle(document.documentElement);
  const mark = css.getPropertyValue("--mark").trim() || "#FFE24D", rule = css.getPropertyValue("--rule-2").trim() || "#CFC7B6";
  const n = 40, bw = 5, gap = (W - n * bw) / (n - 1), lv = vLevels.slice(-n);
  while (lv.length < n) lv.unshift(0);
  const t = performance.now() / 260;
  lv.forEach((v, i) => {
    const wob = 0.07 * (1 + Math.sin(t + i * 0.55));
    const h = Math.max(4, Math.pow(Math.max(v, wob), 0.8) * (H - 6));
    g.globalAlpha = 0.3 + 0.7 * (i / (n - 1));
    g.fillStyle = i > n - 12 ? mark : rule;
    const x = i * (bw + gap);
    g.beginPath();
    g.roundRect ? g.roundRect(x, (H - h) / 2, bw, h, 2.5) : g.rect(x, (H - h) / 2, bw, h);
    g.fill();
  });
  g.globalAlpha = 1;
  $(".qwrap").style.setProperty("--lvl", (lv[lv.length - 1] || 0).toFixed(2));
}
document.addEventListener("keydown", e => {
  if (!vPoll) return;
  if (e.key === "Escape" || e.key === "Enter"){ e.preventDefault(); e.stopPropagation(); fetch("/api/voice/stop", { method:"POST", headers:{ "Content-Type":"application/json" }, body:"{}" }); }
}, true);

/* ── 7. preview: icon by the name, folder trail, one main action, details folded ── */
paintPreview = (orig => async function(force){
  await orig(force);
  const info = $("#pvInfo"), r = currentRow();
  if (!r || !info.querySelector(".pv-name") || info.querySelector(".pv-nmrow")) return;   // already polished
  const name = info.querySelector(".pv-name"), path = info.querySelector(".pv-path");
  name.insertAdjacentHTML("beforebegin", `<div class="pv-nmrow"><img src="${fileURL("icon", joinPath(r), 32)}" alt=""></div>`);
  info.querySelector(".pv-nmrow").append(name);
  if (path){
    let acc = "";
    const parts = r.dir.split("\\").filter(Boolean);
    const crumbs = parts.map((p, i) => { acc += (i ? "\\" : "") + p + (i ? "" : "\\"); return [p, acc]; });
    const shown = crumbs.slice(-3);
    path.outerHTML = `<nav class="pv-trail" aria-label="Folder">${crumbs.length > 3 ? "<i>…</i><i>›</i>" : ""}${shown.map(([p, full], i) =>
      `<button data-trail="${esc(full)}" title="Open ${esc(full)}">${esc(p)}</button>${i < shown.length - 1 ? "<i>›</i>" : ""}`).join("")}</nav>`;
  }
  const meta = info.querySelector(".pv-meta");
  if (meta){
    meta.querySelectorAll("dd span").forEach(s => s.remove());           // "(148,397 bytes)" moves to details
    const dd = [...meta.querySelectorAll("dt")].find(d => d.textContent === "Modified");
    if (dd && dd.nextElementSibling){ dd.nextElementSibling.title = dd.nextElementSibling.textContent; dd.nextElementSibling.textContent = rel(r.mtime); }
    meta.insertAdjacentHTML("afterend", `<details class="pv-more2"><summary>More details</summary>
      <div class="pv-full">${esc(joinPath(r))}</div>${r.size ? `<div class="pv-full">${r.size.toLocaleString()} bytes</div>` : ""}</details>`);
    const fold = () => {                                                 // "Created" can arrive later from /api/peek
      const c = [...meta.querySelectorAll("dt")].find(d => d.textContent === "Created");
      const det = info.querySelector(".pv-more2");
      if (c && det && c.nextElementSibling){
        det.insertAdjacentHTML("beforeend", `<div class="pv-full">Created ${esc(c.nextElementSibling.textContent)}</div>`);
        c.nextElementSibling.remove(); c.remove();
      }
    };
    fold();
    new MutationObserver(fold).observe(meta, { childList:true });
  }
  const acts = info.querySelector(".pv-acts");
  if (acts){
    const rv = acts.querySelector('[data-pa="reveal"]'); if (rv) rv.textContent = "Show in folder";
    const cp = acts.querySelector('[data-pa="copypath"]');
    if (cp) cp.outerHTML = `<button class="btn dots" data-v2dots aria-label="More actions" title="More actions">⋯</button>`;
  }
})(paintPreview);
$("#pvInfo").addEventListener("click", e => {
  const t = e.target.closest("[data-trail]");
  if (t) return openPath(t.dataset.trail);
  const d = e.target.closest("[data-v2dots]");
  if (d){ const r = d.getBoundingClientRect(); openMenu(r.left - 160, r.bottom + 4); }
});

/* ── 8. Settings › Guide: how everything works, in one place ─────────────── */
const GUIDE_ICO = '<svg viewBox="0 0 24 24"><path d="M4 5a2 2 0 012-2h13v16H6a2 2 0 00-2 2zM4 5v16M8 7h7M8 11h5"/></svg>';
const k = s => `<kbd class="kb">${s}</kbd>`;
TABS.splice(TABS.findIndex(t => t[0] === "general") + 1, 0, ["guide", "Guide"]);    // the dialog's title comes from TABS
ICO.guide = GUIDE_ICO;
document.querySelector('#set [data-tab="general"]').insertAdjacentHTML("afterend",
  `<button role="tab" data-tab="guide" aria-selected="false">${GUIDE_ICO}Guide</button>`);
document.querySelector("#sp-general").insertAdjacentHTML("afterend", `<div class="set-pane guide" id="sp-guide" role="tabpanel" hidden>
  <h2>How to use Riffle</h2>
  <p class="lede2">Everything below works offline. Nothing leaves your PC unless you choose a web search.</p>

  <h3>Search from anywhere</h3>
  <ul>
    <li>Press ${k("Ctrl")} ${k("Shift")} ${k("Space")} in any app. The quick bar opens over whatever you are doing.</li>
    <li>Type part of a name. The best match comes first as <b>Top hit</b>, then <b>Apps</b>, <b>Settings</b>, <b>Files</b>,
        <b>Websites</b> from your browser history, and documents with your words <b>inside</b> them.</li>
    <li>${k("↵")} opens · ${k("Ctrl")} ${k("↵")} shows it in its folder · ${k("Ctrl")} ${k("C")} copies the path ·
        ${k("Tab")} continues in this window · ${k("Esc")} closes. Type ${k("?")} for examples.</li>
    <li>Riffle learns what you open, so your usual picks rise to the top.</li>
  </ul>

  <h3>Three ways to search: Search · Image · Voice</h3>
  <ul>
    <li>When the box is empty, press ${k("←")} ${k("→")} (or click the icons in the box) to switch.</li>
    <li><b>Image</b> finds the original of a picture, other copies and look-alikes. Press ${k("↓")} to choose a file,
        or ${k("Ctrl")} ${k("V")} to paste a copied picture or screenshot. Set its folders under <b>Visual search</b>.</li>
    <li><b>Voice</b> types as you speak and searches as you go. Pause when you are done; ${k("Esc")} stops.
        Add it once under <b>General › Voice search</b> (65 MB, works offline).</li>
  </ul>

  <h3>What you can type</h3>
  <ul>
    <li>A setting: <i>bluetooth</i>, <i>wifi</i>, <i>dark mode</i>, <i>uninstall</i>.</li>
    <li>A sum: <i>12*(3+4)</i> or <i>18% of 4,500</i>. ${k("↵")} copies the answer.</li>
    <li>A website: <i>github.com</i> opens it. Shortcuts: <i>yt lofi</i>, <i>g weather</i>, <i>maps cafe</i>, <i>wiki goa</i>.</li>
  </ul>

  <h3>This window</h3>
  <ul>
    <li>The chips under the box narrow by type. The sidebar narrows by <b>Modified</b> date, <b>Where</b>, <b>Size</b> and
        more types; drag across a graph to pick a range.</li>
    <li>${k("Aa")} changes how names match (close enough, patterns) and shows hidden and system files.</li>
    <li><b>List</b> is easy to read; <b>Details</b> is the full table with exact sizes and paths.</li>
    <li>${k("Space")} shows or hides the preview · ${k("Ctrl")} ${k("D")} switches dark mode · ${k("Ctrl")} ${k("K")} jumps to the box.</li>
  </ul>

  <h3>Free up space</h3>
  <ul>
    <li><b>Duplicates</b> finds exact copies, even renamed ones. One of each is always kept.</li>
    <li><b>Clean up</b> lists temp files, caches and old installers, each with the reason it is safe. Your files go to the Recycle Bin.</li>
  </ul>
</div>`);

/* ── start ───────────────────────────────────────────────────────────────── */
setListView(view0);
syncChips();
if (app.classList.contains("home")){ homeShown = true; paintRecent(); paintPicks(); paintSpace(); }
})();
