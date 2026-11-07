/* Riffle 1.2: settings dialog, system-file hiding, extension chips with counts,
   recent-files home view. Loaded after ui-extra.js. */
"use strict";
let SET = {}, SV = null;            // saved settings, full settings view from the server
const sysDark = () => window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
const ICO = {
  general:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"/></svg>',
  search:'<svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="7"/><path d="M21 21l-5.6-5.6"/></svg>',
  index:'<svg viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>',
  clean:'<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>',
  visual:'<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5-8 8"/></svg>',
  about:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/></svg>',
  x:'<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  gear:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/></svg>'
};
const keyLabel = k => k.split("+").map(p => p === "`" ? "`" : p.length === 1 ? p.toUpperCase()
  : p.replace(/^f(\d+)$/, "F$1").replace(/^\w/, c => c.toUpperCase())).join(" + ");
const sw = (id, on, dis) => `<label class="sw"><input type="checkbox" id="${id}" ${on ? "checked" : ""} ${dis ? "disabled" : ""}><i></i></label>`;
const row = (title, desc, ctrl) => `<div class="srow"><div class="t"><b>${title}</b>${desc ? `<p>${desc}</p>` : ""}</div>${ctrl}</div>`;

/* ── load / save ───────────────────────────────────────────── */
async function loadSettings(){
  try{ SV = await api("/api/settings"); SET = SV.settings; }catch(e){ SET = {}; }
  applySettings(true);
}
async function saveSetting(patch, extra = {}, quiet = false){
  const r = await api("/api/settings", { settings:patch, ...extra }).catch(() => null);
  if (!r || r.error){ toast(r && r.error ? r.error : "Couldn't save that"); return; }
  const filterChange = ["hideSystem", "hideHidden", "excludeDirs", "homeRecent"].some(k => k in patch);
  SV = r; SET = r.settings;
  applySettings(false);
  if ($("#set").classList.contains("on")) paintSettings();
  if (filterChange) scheduleSearch(0);
  if (!quiet) toast("Saved");
}
function applySettings(boot){
  if (SET.theme) setTheme(SET.theme === "system" ? (sysDark() ? "dark" : "light") : SET.theme);
  app.classList.toggle("noanim", SET.animation === false);
  const qs = $("#qsSys");
  if (qs) qs.checked = !SET.hideSystem;
  if (boot){
    if (SET.previewOnStart && !pv.on) togglePreview(true);
    const o = $(`#dMin option[value="${SET.dupesMin}"]`);
    if (o) $("#dMin").value = SET.dupesMin;
  }
}
if (window.matchMedia) matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (SET.theme === "system") setTheme(sysDark() ? "dark" : "light");
});
// the theme button now remembers its choice as a setting
const themeClick = $("#themeBtn").onclick;
$("#themeBtn").onclick = () => { themeClick(); saveSetting({ theme:document.documentElement.dataset.theme }, {}, true); };

/* ── search: settings-driven filters, home view, extension counts ── */
filters = (orig => function(){
  const f = orig();
  if (SET.hideSystem) f.hideSystem = true;
  if (SET.hideHidden) f.hideHidden = true;
  if (SET.excludeDirs && SET.excludeDirs.length) f.excludeDirs = SET.excludeDirs;
  return f;
})(filters);

function isHome(){
  return SET.homeRecent !== false && !state.q && state.kind === "all" && !state.cats.size && !state.exts.length
    && !state.scope && !state.exclude && state.timeMin == null && state.timeMax == null
    && state.sizeMin == null && state.sizeMax == null;
}
fetchPage = async function(p, token){
  const home = isHome();
  app.classList.toggle("home", home);
  const f = filters();
  if (home) f.kind = "files";
  const r = await fetch("/api/search", {
    method:"POST", headers:{ "Content-Type":"application/json" },
    body:JSON.stringify({ q:state.q, mode:state.mode, filters:f,
      sort:home ? "modified" : (state.q.trim() && !state.sortPicked ? "best" : state.sort),
      desc:home ? true : (state.q.trim() && !state.sortPicked ? false : state.desc), offset:p * PAGE, limit:PAGE })
  });
  const d = await r.json();
  if (token !== state.token) return null;
  if (p === 0) state.extCounts = d.exts || [];
  return d;
};

let extOpen = false;
function paintExts(){
  const box = $("#extChips"), list = state.extCounts || [];
  const sel = new Set(state.exts.map(e => e.toLowerCase().replace(/^\./, "")));
  const shown = list.slice(0, extOpen ? 60 : 12);
  for (const e of sel) if (!shown.some(([x]) => x === e)) shown.push([e, (list.find(([x]) => x === e) || [e, 0])[1]]);
  box.innerHTML = shown.map(([e, n]) =>
    `<button class="chip" data-ext="${esc(e)}" aria-pressed="${sel.has(e)}">.${esc(e)} <span class="n">${n.toLocaleString()}</span></button>`).join("")
    + (list.length > 12 ? `<button class="chip more" data-more>${extOpen ? "fewer" : `+${list.length - 12} more`}</button>` : "");
}
paintFacets = (orig => function(c){ orig(c); paintExts(); })(paintFacets);

/* ── rail additions: extension chips, quick system-files switch, home strip ── */
$("#extField").insertAdjacentHTML("afterend", `<div class="extchips" id="extChips" aria-label="Extensions in these results"></div>`);
$("#extChips").addEventListener("click", e => {
  const b = e.target.closest(".chip"); if (!b) return;
  if (b.dataset.more !== undefined){ extOpen = !extOpen; return paintExts(); }
  const x = b.dataset.ext, cur = state.exts.map(e => e.toLowerCase().replace(/^\./, ""));
  state.exts = cur.includes(x) ? cur.filter(e => e !== x) : [...cur, x];
  $("#extField").value = state.exts.join(", ");
  savePrefs(); scheduleSearch(0);
});
$("#kindChips").insertAdjacentHTML("afterend", `<label class="qs">${sw("qsSys", false)}
  <span>Show system &amp; hidden files<small>Windows, Program Files, AppData, dot-folders</small></span></label>`);
$("#qsSys").addEventListener("change", e =>
  saveSetting({ hideSystem:!e.target.checked, hideHidden:!e.target.checked }, {}, true));
$(".main .head").insertAdjacentHTML("beforebegin", `<div class="homebar">${ICO.search.replace("<svg", '<svg width="14" height="14" style="stroke:currentColor;fill:none;stroke-width:2"')}
  <b>Recently changed</b><span>Your newest files. Start typing to search everything.</span><span class="kb">Ctrl K</span></div>`);

/* ── settings dialog ───────────────────────────────────────── */
const TABS = [["pro", "Riffle Pro"], ["general", "General"], ["search", "Search"], ["index", "Index"], ["clean", "Duplicates & clean up"],
              ["visual", "Visual search"], ["about", "About"]];
ICO.pro = '<svg viewBox="0 0 24 24"><path d="M12 3l2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.6 6.6 19.5l1.2-6L3.3 9.3l6.1-.7z"/></svg>';
document.body.insertAdjacentHTML("beforeend", `
<div class="dlg set" id="set" role="dialog" aria-modal="true" aria-label="Settings">
  <div class="box">
    <nav class="set-tabs" role="tablist" aria-label="Settings sections">
      <h2>Settings</h2>
      ${TABS.map(([k, l], i) => `<button role="tab" data-tab="${k}" aria-selected="${i === 0}">${ICO[k]}${l}</button>`).join("")}
      <div class="ver" id="setVer"></div>
    </nav>
    <div class="set-body">
      <div class="set-head"><h3 id="setTitle">General</h3><button class="x" id="setClose" aria-label="Close settings">${ICO.x}</button></div>
      ${TABS.map(([k], i) => `<div class="set-pane" id="sp-${k}" role="tabpanel" ${i ? "hidden" : ""}></div>`).join("")}
    </div>
  </div>
</div>`);
$("#themeBtn").insertAdjacentHTML("beforebegin",
  `<button class="icobtn" id="setBtn" title="Settings (Ctrl+,)" aria-label="Settings">${ICO.general}</button>`);
$("#sp-visual").appendChild($("#visGrp"));     // visual search setup lives in Settings now

function openSettings(tab){
  $("#set").classList.add("on");
  api("/api/settings").then(r => { SV = r; SET = r.settings; paintSettings(); });
  paintSettings();
  showTab(tab || $("#set [aria-selected=true]").dataset.tab);
}
function closeSettings(){ $("#set").classList.remove("on"); $("#q").focus(); }
function showTab(k){
  document.querySelectorAll("#set [data-tab]").forEach(b => b.setAttribute("aria-selected", b.dataset.tab === k));
  document.querySelectorAll("#set .set-pane").forEach(p => p.hidden = p.id !== "sp-" + k);
  $("#setTitle").textContent = TABS.find(t => t[0] === k)[1];
  if (k === "visual") pollVisual();
}
$("#setBtn").onclick = () => openSettings();
$("#setClose").onclick = closeSettings;
$("#set").addEventListener("click", e => {
  if (e.target.id === "set") return closeSettings();
  const t = e.target.closest("[data-tab]"); if (t) showTab(t.dataset.tab);
});
$("#set").addEventListener("keydown", e => {
  if (e.key === "Escape"){ e.stopPropagation(); closeSettings(); }
  if ((e.key === "ArrowDown" || e.key === "ArrowUp") && e.target.closest(".set-tabs")){
    const tabs = [...document.querySelectorAll("#set [data-tab]")];
    const i = tabs.indexOf(e.target) + (e.key === "ArrowDown" ? 1 : -1);
    if (tabs[i]){ e.preventDefault(); tabs[i].focus(); showTab(tabs[i].dataset.tab); }
  }
});
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key === ","){ e.preventDefault(); $("#set").classList.contains("on") ? closeSettings() : openSettings(); }
}, true);

function paintSettings(){
  if (!SV) return;
  const s = SET, ix = SV.index;
  $("#setVer").textContent = "Riffle " + (SV.version || "");

  $("#sp-general").innerHTML =
    row("Summon hotkey", SV.hotkey.ok ? `Press it anywhere to open Riffle.`
          : `<span class="warn">${esc(keyLabel(s.hotkey))} is already used by another app. Pick another.</span>`,
        `<select id="sHotkey">${SV.hotkeys.map(k => `<option value="${k}" ${k === s.hotkey ? "selected" : ""}>${esc(keyLabel(k))}</option>`).join("")}</select>`)
    + row("The hotkey opens", "The quick bar is a small search box that appears instantly over any app.",
        `<div class="seg2" id="sOpens">${[["bar", "Quick bar"], ["window", "Full window"]].map(([v, l]) =>
          `<button data-v="${v}" aria-pressed="${(s.hotkeyOpens || "bar") === v}">${l}</button>`).join("")}</div>`)
    + row("Preview in the quick bar", "A picture or the first lines of the selected file, beside the results.", sw("sQPrev", s.quickPreview !== false))
    + row("Quick bar look", "Glass needs Windows 11.", `<div class="seg2" id="sLook">${[["paper", "Paper"], ["glass", "Glass"]].map(([v, l]) =>
          `<button data-v="${v}" aria-pressed="${(s.quickLook || "paper") === v}">${l}</button>`).join("")}</div>`)
    + row("Apps in the quick bar", "Start menu and Microsoft Store apps.", sw("sQApps", s.quickApps !== false))
    + row("Windows settings in the quick bar", "Pages like Bluetooth, Wi-Fi and Display.", sw("sQSet", s.quickSettings !== false))
    + row("Browser history in the quick bar", "Pages and bookmarks from Chrome, Edge, Brave, Opera, Vivaldi and Firefox. Read on this PC only.", sw("sQHist", s.quickHistory !== false))
    + row("Web search in the quick bar", "Open addresses you type, a search row at the end, and shortcuts: g, yt, maps, wiki.", sw("sQWeb", s.quickWeb !== false))
    + row("Search engine", "", `<select id="sEngine">${[["google", "Google"], ["bing", "Bing"], ["duckduckgo", "DuckDuckGo"]].map(([v, l]) =>
          `<option value="${v}" ${(s.searchEngine || "google") === v ? "selected" : ""}>${l}</option>`).join("")}</select>`)
    + row("Start with Windows", SV.canAutostart ? "Riffle waits quietly so the hotkey always works."
          : "Available in the installed app.", sw("sAuto", SV.autostart, !SV.canAutostart))
    + row("Theme", "", `<div class="seg2" id="sTheme">${["system", "light", "dark"].map(t =>
          `<button data-v="${t}" aria-pressed="${s.theme === t}">${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}</div>`)
    + row("Summon animation", "The highlighter sweep and card riffle when the window opens.", sw("sAnim", s.animation))
    + row("Open the preview panel on start", "Otherwise press Space to show it.", sw("sPrev", s.previewOnStart))
    + row("Check for updates", "Once a day, Riffle asks GitHub whether a newer version exists. Nothing about you is sent.", sw("sUpd", s.updates));

  $("#sp-search").innerHTML =
    row("Hide system and app files", "Windows, Program Files, ProgramData, AppData and the Recycle Bin stay out of results.", sw("sSys", s.hideSystem))
    + row("Hide hidden files", "Dot-folders and dot-files such as .git, .cache and .gitignore.", sw("sHid", s.hideHidden))
    + row("Show recent files when the search box is empty", "Your newest files, ready the moment Riffle opens.", sw("sHome", s.homeRecent))
    + row("Default match", "", `<div class="seg2" id="sMode">${[["substring", "Contains"], ["fuzzy", "Fuzzy"], ["regex", "Regex"], ["content", "Inside"]].map(([v, l]) =>
          `<button data-v="${v}" aria-pressed="${state.mode === v}">${l}</button>`).join("")}</div>`)
    + `<div class="sgroup">Never show these folders</div>
       <ul class="ledit" id="sEx">${(s.excludeDirs || []).map((d, i) =>
         `<li><span title="${esc(d)}">${esc(d)}</span><button data-rm="${i}" aria-label="Remove ${esc(d)}">×</button></li>`).join("")
         || `<li class="none">Nothing excluded yet.</li>`}
         <li class="add"><input id="sExIn" placeholder="C:\\Users\\you\\OldBackups" spellcheck="false" aria-label="Folder to exclude"><button class="btn" id="sExAdd">Add</button></li></ul>`;

  const roots = new Set((ix.roots || []).map(r => r.toUpperCase()));
  $("#sp-index").innerHTML =
    `<div class="stat-grid">
       <div><b>${ix.count.toLocaleString()}</b><span>files and folders</span></div>
       <div><b>${fmtSize(ix.bytes)}</b><span>index on disk</span></div>
       <div><b>${ix.savedAt ? new Date(ix.savedAt * 1000).toLocaleTimeString([], { hour:"2-digit", minute:"2-digit" }) : "—"}</b><span>last saved</span></div>
     </div>`
    + row("Drives to index", "Riffle watches these for changes as they happen.",
          `<div class="drives">${ix.drives.map(d => `<button class="chip" data-drive="${esc(d)}" aria-pressed="${roots.has(d.toUpperCase())}">${esc(d)}</button>`).join("")}</div>`)
    + row("Rebuild the index", `${esc(ix.message || "")}. Takes a minute or two, searching keeps working meanwhile.`,
          `<button class="btn" id="sRebuild">Rebuild now</button>`)
    + `<p class="hint" style="margin-top:12px">Inside <code>node_modules</code>, <code>WinSxS</code>, <code>site-packages</code> and browser caches only the folder itself is listed, not its contents. That keeps the index small and Riffle light.</p>`;

  const mins = [[102400, "100 KB"], [1048576, "1 MB"], [10485760, "10 MB"], [104857600, "100 MB"]];
  $("#sp-clean").innerHTML =
    row("Smallest file for duplicate scans", "Smaller files rarely free much space.",
        `<select id="sDMin">${mins.map(([v, l]) => `<option value="${v}" ${v === s.dupesMin ? "selected" : ""}>${l}</option>`).join("")}</select>`)
    + row("Old downloads", "Suggest files in Downloads not changed for this many days.", `<input type="number" id="sDl" min="1" value="${s.cleanDownloadsDays}">`)
    + row("Old installers", "Suggest setup files in Downloads older than this many days.", `<input type="number" id="sInst" min="0" value="${s.cleanInstallerDays}">`)
    + row("Big files", "Suggest files bigger than this many MB, untouched for a year.", `<input type="number" id="sBig" min="10" value="${s.cleanBigMB}">`)
    + row("Forget what duplicate scans remember", "Riffle keeps fingerprints of checked files so rescans are instant. This clears them.",
          `<button class="btn" id="sClearHash">Clear</button>`);

  $("#sp-about").innerHTML =
    `<div class="about-logo"><span class="logo">Riffle</span><div><b>Version ${esc(SV.version || "")}</b><p class="hint">Search and clean up every file on your PC.</p></div></div>`
    + `<div class="sgroup">Made by</div>
       <div class="mk-card mk-bliink">
         <div class="mk-top"><span class="bl-word">bliink</span><span class="pill lbl-safe">Studio</span></div>
         <p>A small studio of useful apps, for books, people, habits and the web. Each one lives on its own
            bliink.in address and is free to start. Riffle is one of them.</p>
         <p class="mk-steps"><span>Notice</span><span>Build</span><span>Ship</span><span>Listen</span></p>
         <div class="links"><a href="https://bliink.in" target="_blank" rel="noopener">bliink.in</a>
           <span class="hint">Shelfy · Lectern · Relay · Keep · Wall · KickAsh · Flock · Bliink Links</span></div>
         <a class="mk-credit" href="https://ravirahul.bliink.in" target="_blank" rel="noopener">by Ravi Rahul</a>
       </div>`
    + row("Your data", `Index, settings and fingerprints live in <code>${esc(SV.dataDir)}</code>. Nothing leaves this PC.`,
          `<button class="btn" id="sOpenData">Open folder</button>`)
    + row("Updates", "", `<button class="btn" id="sCheck">Check now</button>`)
    + `<div class="links">
         <a href="https://github.com/ravirahul/riffle" target="_blank" rel="noopener">Source on GitHub</a>
         <a href="https://github.com/ravirahul/riffle/issues" target="_blank" rel="noopener">Report a problem</a>
         <a href="https://github.com/ravirahul/riffle/releases" target="_blank" rel="noopener">What's new</a>
       </div>`
    + `<div class="sgroup">Keyboard</div>
       <div class="pv-meta" style="grid-template-columns:auto 1fr;margin-top:10px">
         <dt><kbd class="kb">${esc(keyLabel(s.hotkey))}</kbd></dt><dd>Open Riffle from anywhere</dd>
         <dt><kbd class="kb">Ctrl K</kbd></dt><dd>Jump to the search box</dd>
         <dt><kbd class="kb">Space</kbd></dt><dd>Preview the selected file</dd>
         <dt><kbd class="kb">Enter</kbd></dt><dd>Open · Ctrl+Enter shows it in Explorer</dd>
         <dt><kbd class="kb">Ctrl C</kbd></dt><dd>Copy the full path</dd>
         <dt><kbd class="kb">Ctrl D</kbd></dt><dd>Light or dark</dd>
         <dt><kbd class="kb">Ctrl ,</kbd></dt><dd>Settings</dd>
       </div>`;
}

/* settings controls (event delegation, since panes repaint) */
$("#set").addEventListener("change", e => {
  const id = e.target.id, v = e.target.type === "checkbox" ? e.target.checked : e.target.value;
  const map = { sAnim:"animation", sPrev:"previewOnStart", sUpd:"updates", sSys:"hideSystem", sHid:"hideHidden",
                sHome:"homeRecent", sHotkey:"hotkey", sQPrev:"quickPreview",
                sQApps:"quickApps", sQSet:"quickSettings", sQHist:"quickHistory", sQWeb:"quickWeb", sEngine:"searchEngine" };
  if (map[id]) return saveSetting({ [map[id]]:v });
  if (id === "sAuto") return saveSetting({}, { autostart:v });
  if (id === "sDMin"){ $("#dMin").value = v; return saveSetting({ dupesMin:+v }); }
  const num = { sDl:"cleanDownloadsDays", sInst:"cleanInstallerDays", sBig:"cleanBigMB" }[id];
  if (num && e.target.value !== "") return saveSetting({ [num]:+e.target.value });
});
$("#set").addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.closest("#sTheme")) return saveSetting({ theme:b.dataset.v });
  if (b.closest("#sOpens")) return saveSetting({ hotkeyOpens:b.dataset.v });
  if (b.closest("#sLook")) return saveSetting({ quickLook:b.dataset.v });
  if (b.closest("#sMode")){
    document.querySelector(`.seg [data-mode="${b.dataset.v}"]`).click();
    return paintSettings();
  }
  if (b.dataset.rm !== undefined){
    const list = [...SET.excludeDirs]; list.splice(+b.dataset.rm, 1);
    return saveSetting({ excludeDirs:list });
  }
  if (b.id === "sExAdd"){
    const d = $("#sExIn").value.trim(); if (!d) return $("#sExIn").focus();
    return saveSetting({ excludeDirs:[...(SET.excludeDirs || []), d] });
  }
  if (b.dataset.drive){
    const on = b.getAttribute("aria-pressed") !== "true";
    const roots = [...document.querySelectorAll("#sp-index [data-drive]")].filter(x => x === b ? on : x.getAttribute("aria-pressed") === "true").map(x => x.dataset.drive);
    if (!roots.length) return toast("Keep at least one drive");
    if (!await ask(`Index ${roots.join(", ")}?`, "Rebuild index", "Riffle reads these drives again. Searching keeps working while it runs.", false)) return;
    return saveSetting({}, { roots });
  }
  if (b.id === "sRebuild"){
    await api("/api/reindex", { roots:SV.index.roots });
    toast("Rebuilding the index"); poll();
  }
  if (b.id === "sClearHash" && await ask("Forget all duplicate-scan fingerprints?", "Clear", "The next scan reads files again.", false))
    return saveSetting({}, { clearHashes:true });
  if (b.id === "sOpenData") return act("open", [pathRow(SV.dataDir)]);
  if (b.id === "sCheck"){
    const u = await api("/api/update").catch(() => null);
    toast(!u ? "Couldn't reach GitHub" : u.available ? `Riffle ${u.latest} is available` : "You're on the latest version");
  }
});
$("#set").addEventListener("keydown", e => { if (e.key === "Enter" && e.target.id === "sExIn") $("#sExAdd").click(); });

loadSettings().then(() => scheduleSearch(0));

/* Best match first while typing; clicking a column header switches to that sort. */
document.querySelector(".head").addEventListener("click", e => { if (e.target.closest("[data-sort]")) state.sortPicked = true; }, true);
$("#q").addEventListener("input", () => { if (!$("#q").value) state.sortPicked = false; });
// Faster response to typing (was 90 ms)
scheduleSearch = (orig => function(delay = 35){ return orig(delay); })(scheduleSearch);

/* the quick bar can hand a search over to this window ("Open in Riffle") */
async function checkInbox(){
  const r = await api("/api/inbox").catch(() => null);
  if (r && r.q != null){
    setView("search");
    $("#q").value = r.q; state.q = r.q; scheduleSearch(0);
    $("#q").focus();
  }
}
window.addEventListener("focus", checkInbox);
checkInbox();
