/* Riffle Insights: overview, types, extension bubbles, locations, age,
   activity calendar, folder map, biggest files, busiest folders.
   Everything comes from /api/insights (one numpy pass over the index). */
"use strict";
const CATN = { document:"Documents", image:"Images", video:"Video", audio:"Audio", code:"Code", archive:"Archives", other:"Other" };
const ccol = c => `var(--c-${c})`;
// Label ink inside a coloured fill: white only where it measures >= 4.5:1, else near-black.
const WHITE_ON = { light:new Set(["document", "archive"]), dark:new Set(["archive"]) };
const darkLabel = c => !WHITE_ON[document.documentElement.dataset.theme === "dark" ? "dark" : "light"].has(c);
const ins = { data:null, all:false, root:null, mode:"bytes", loading:false, trail:[] };
const pct = (a, b) => b ? (100 * a / b) : 0;
const fmtN = n => n.toLocaleString();

/* ── shell: nav item, pane, logo tag ──────────────────────── */
$(".nav").insertAdjacentHTML("beforeend",
  `<button data-view="insights" aria-current="false"><svg viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg><span>Insights</span></button>`);
$(".logo").insertAdjacentHTML("beforeend", `<span class="by">by <b>bliink</b></span>`);
document.querySelector(".body").insertAdjacentHTML("beforeend", `
<section class="pane" id="insPane" aria-label="Insights">
  <div class="pane-in">
    <div class="ins-head">
      <div class="t"><h1>What's on <span class="mk">this PC</span></h1>
        <p class="lede">Click anything to see those files.</p></div>
      <label class="qs"><label class="sw"><input type="checkbox" id="insAll"><i></i></label>
        <span>Include system files<small>Only for Insights</small></span></label>
    </div>
    <div class="ins-meta" id="insMeta"></div>
    <div id="insOut"><div class="ins-load">Counting…</div></div>
  </div>
</section>`);
document.body.insertAdjacentHTML("beforeend", `<div class="vtip" id="vtip" role="tooltip"></div>`);

// views: Insights loads on open
setView = (orig => function(v, push){ orig(v, push); if (v === "insights") loadInsights(); })(setView);
$("#insAll").addEventListener("change", e => { ins.all = e.target.checked; ins.root = null; ins.trail = []; loadInsights(true); });

async function loadInsights(force){
  if (ins.loading) return;
  if (ins.data && !force && Date.now() - ins.at < 60000) return;
  ins.loading = true;
  if (!ins.data) $("#insOut").innerHTML = `<div class="ins-load">Counting…</div>`;
  try{
    ins.data = await api("/api/insights", { all:ins.all, root:ins.root });
    ins.at = Date.now();
    paintInsights();
  }catch(e){ $("#insOut").innerHTML = `<div class="ins-load">Couldn't read the index. Try again in a moment.</div>`; }
  ins.loading = false;
}

/* ── tooltip ─────────────────────────────────────────────── */
const tip = $("#vtip");
function showTip(html, e){
  tip.innerHTML = html; tip.classList.add("on");
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(e.clientX + 14, innerWidth - r.width - 8) + "px";
  tip.style.top = Math.min(e.clientY + 14, innerHeight - r.height - 8) + "px";
}
const hideTip = () => tip.classList.remove("on");
$("#insOut").addEventListener("mousemove", e => {
  const t = e.target.closest("[data-tip]");
  t ? showTip(t.dataset.tip, e) : hideTip();
});
$("#insOut").addEventListener("mouseleave", hideTip);

/* ── jump to Search with filters ─────────────────────────── */
function goSearch({ cats = [], exts = [], scope = "", timeMin = null, timeMax = null } = {}){
  hideTip();
  state.q = ""; $("#q").value = "";
  state.forceAll = ins.all;         // keep Insights' "include system files" for this jump
  state.kind = "files"; state.cats = new Set(cats); state.exts = exts;
  state.scope = scope; state.exclude = "";
  state.timeMin = timeMin; state.timeMax = timeMax; state.sizeMin = state.sizeMax = null;
  state.sort = "size"; state.desc = true;
  for (const c of $("#kindChips").children) c.setAttribute("aria-pressed", c.dataset.kind === "files");
  for (const c of $("#catChips").children) c.setAttribute("aria-pressed", state.cats.has(c.dataset.cat));
  $("#extField").value = exts.join(", "); $("#scopeField").value = scope; $("#excludeField").value = "";
  document.querySelectorAll("[data-sort]").forEach(b => { b.classList.toggle("act", b.dataset.sort === "size"); b.classList.toggle("dsc", b.dataset.sort === "size"); });
  setView("search");
  scheduleSearch(0);
}

/* ── render ──────────────────────────────────────────────── */
function paintInsights(){
  const d = ins.data, T = d.totals;
  $("#insMeta").textContent = `${d.all ? "Everything indexed" : "Your files, without system and hidden ones"}`;
  const types = d.types.filter(t => t.files);
  const tot = ins.mode === "bytes" ? T.bytes : T.files;
  const val = t => ins.mode === "bytes" ? t.bytes : t.files;
  const show = v => ins.mode === "bytes" ? fmtSize(v) : fmtN(v);
  const old = d.age.slice(4).reduce((a, b) => a + b.bytes, 0);
  const year = d.age.slice(3).reduce((a, b) => a + b.bytes, 0);

  let h = `<div class="tiles">
    <div class="tile-s"><div class="v">${fmtN(T.files)}</div><div class="l">files</div><div class="s">in ${fmtN(T.folders)} folders</div></div>
    <div class="tile-s"><div class="v">${fmtSize(T.bytes)}</div><div class="l">space used</div><div class="s">${T.files ? fmtSize(T.bytes / T.files) : "—"} on average</div></div>
    <div class="tile-s"><div class="v">${fmtN(T.weekFiles)}</div><div class="l">changed this week</div><div class="s">${fmtSize(T.weekBytes)}</div></div>
    <div class="tile-s"><div class="v">${d.biggest.length ? fmtSize(d.biggest[0].size) : "—"}</div><div class="l">biggest file</div>
      <div class="s" title="${d.biggest.length ? esc(d.biggest[0].path) : ""}">${d.biggest.length ? esc(d.biggest[0].path.split("\\").pop()) : ""}</div></div>
  </div><div class="igrid">`;

  // file types
  h += `<div class="icard"><h3>File types<span class="sp"></span>
      <div class="seg2" id="insMode"><button data-m="bytes" aria-pressed="${ins.mode === "bytes"}">Space</button><button data-m="files" aria-pressed="${ins.mode === "files"}">Count</button></div></h3>
    <p class="sub">${biggestType(types, tot, val)}</p>
    <div class="tbar" role="img" aria-label="Share of ${ins.mode === "bytes" ? "space" : "files"} by type">${types.map(t =>
      `<i style="flex:${Math.max(val(t), tot * 0.004)};background:${ccol(t.cat)}" data-cat="${t.cat}" data-tip="<b>${CATN[t.cat]}</b><br><span>${fmtN(t.files)} files · ${fmtSize(t.bytes)} · ${pct(val(t), tot).toFixed(1)}%</span>"></i>`).join("")}</div>
    <div class="legend">${types.map(t => `<button data-cat="${t.cat}"><span class="sw8" style="background:${ccol(t.cat)}"></span>
      <span class="n">${CATN[t.cat]}</span><span class="v">${show(val(t))}</span><span class="p">${pct(val(t), tot).toFixed(0)}%</span></button>`).join("")}</div></div>`;

  // extension bubbles
  h += `<div class="icard"><h3>Extensions</h3><p class="sub">Bigger bubble, more space.</p>
    <div class="bubbles" id="insBub"></div></div>`;

  // locations
  const lmax = Math.max(...d.locations.map(l => l.bytes), 1);
  h += `<div class="icard"><h3>Where your files live</h3><p class="sub">Space by place${d.locations.length ? `: ${esc(d.locations[0].label)} holds ${pct(d.locations[0].bytes, T.bytes).toFixed(0)}%` : ""}.</p>
    <div class="hbars">${d.locations.map(l => `<button class="hbar${l.path ? "" : " static"}" ${l.path ? `data-scope="${esc(l.path)}"` : ""}
      data-tip="<b>${esc(l.label)}</b><br><span>${fmtN(l.files)} files · ${fmtSize(l.bytes)}</span>">
      <span class="lab">${esc(l.label)}</span><span class="track"><i style="width:${pct(l.bytes, lmax)}%"></i></span><span class="val">${fmtSize(l.bytes)}</span></button>`).join("")}</div></div>`;

  // age
  const amax = Math.max(...d.age.map(a => a.bytes), 1);
  const ageDays = [[0, 7], [7, 30], [30, 182], [182, 365], [365, 730], [730, 36500]];
  h += `<div class="icard"><h3>Age of your data</h3><p class="sub">By last change.</p>
    <div class="hbars">${d.age.map((a, i) => `<button class="hbar" data-age="${i}"
      data-tip="<b>${a.label}</b><br><span>${fmtN(a.files)} files · ${fmtSize(a.bytes)}</span>">
      <span class="lab">${a.label}</span><span class="track"><i style="width:${pct(a.bytes, amax)}%;background:var(--heat-${Math.min(5, 5 - i + 0) || 1})"></i></span><span class="val">${fmtSize(a.bytes)}</span></button>`).join("")}</div>
    ${year ? `<div class="callout"><span><b>${fmtSize(year)}</b> hasn't changed in a year${old ? `, ${fmtSize(old)} of it in over two` : ""}.</span><button class="btn" data-goclean>Clean up</button></div>` : ""}</div>`;

  // activity calendar
  h += `<div class="icard wide"><h3>Activity</h3><p class="sub">Files changed per day, last year.</p>
    <div class="cal" id="insCal"></div>
    <div class="heat-key">Less ${[0, 1, 2, 3, 4, 5].map(i => `<i style="background:var(--heat-${i})"></i>`).join("")} More</div></div>`;

  // folder map
  const m = d.map;
  h += `<div class="icard wide"><h3>Folder map<span class="sp"></span><span class="crumbs" id="insCrumbs"></span></h3>
    <p class="sub">Click a folder to look inside.</p>
    <div class="fmap" id="insMap"></div></div>`;

  // top lists
  h += `<div class="icard"><h3>Biggest files</h3><div class="toplist">${d.biggest.slice(0, 10).map(b => {
      const nm = b.path.split("\\").pop(), dir = b.path.slice(0, b.path.length - nm.length - 1);
      return `<button data-reveal="${esc(b.path)}"><span class="dot" style="background:${ccol(b.cat)}"></span>
        <span class="nm">${esc(nm)}<small><bdi>${esc(dir)}</bdi></small></span><span class="val">${fmtSize(b.size)}</span></button>`; }).join("")}</div></div>`;
  h += `<div class="icard"><h3>Busiest folders</h3><p class="sub">Most files directly inside.</p><div class="toplist">${d.folders.slice(0, 10).map(f =>
      `<button data-scope="${esc(f.path)}"><span class="dot" style="background:var(--rule-2)"></span>
        <span class="nm">${esc(f.path.split("\\").filter(Boolean).pop() || f.path)}<small><bdi>${esc(f.path)}</bdi></small></span><span class="val">${fmtN(f.files)}</span></button>`).join("")}</div></div>`;

  $("#insOut").innerHTML = h + `</div>`;
  paintBubbles(d.exts);
  paintCalendar(d.activity);
  paintMap(m);
}
function biggestType(types, tot, val){
  if (!types.length) return "";
  const t = [...types].sort((a, b) => val(b) - val(a))[0];
  const share = pct(val(t), tot), cnt = pct(t.files, types.reduce((a, b) => a + b.files, 0));
  return ins.mode === "bytes"
    ? `${CATN[t.cat]}: ${share.toFixed(0)}% of the space, ${cnt.toFixed(0)}% of the files.`
    : `${CATN[t.cat]}: ${share.toFixed(0)}% of all files.`;
}

/* bubbles: biggest first, each placed touching earlier ones, as close to centre as fits */
function paintBubbles(exts){
  const box = $("#insBub"), W = box.clientWidth, H = box.clientHeight;
  const list = exts.slice(0, 40), maxB = list.length ? list[0].bytes : 1;
  const area = W * H * 0.52, sumB = list.reduce((a, e) => a + e.bytes, 0) || 1;
  const placed = [];
  for (const e of list){
    const r = Math.max(5, Math.sqrt(area * e.bytes / sumB / Math.PI));
    let best = null;
    if (!placed.length) best = { x:W / 2, y:H / 2 };
    else for (const p of placed){
      for (let a = 0; a < 360; a += 12){
        const t = a * Math.PI / 180, x = p.x + (p.r + r + 2) * Math.cos(t), y = p.y + (p.r + r + 2) * Math.sin(t);
        if (x - r < 2 || x + r > W - 2 || y - r < 2 || y + r > H - 2) continue;
        if (placed.some(q => Math.hypot(q.x - x, q.y - y) < q.r + r + 1.5)) continue;
        const dd = Math.hypot(x - W / 2, (y - H / 2) * 1.4);
        if (!best || dd < best.d) best = { x, y, d:dd };
      }
    }
    if (best) placed.push({ ...e, r, x:best.x, y:best.y });
  }
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Extensions by space">${placed.map(p => {
    const lab = p.r > 16 ? `<text x="${p.x}" y="${p.y - (p.r > 26 ? 6 : 0)}" fill="${darkLabel(p.cat) ? "#0b0b0b" : "#fff"}">.${esc(p.ext)}</text>`
      + (p.r > 26 ? `<text x="${p.x}" y="${p.y + 8}" fill="${darkLabel(p.cat) ? "#0b0b0b" : "#fff"}" style="font-size:10px;opacity:.9">${fmtSize(p.bytes)}</text>` : "") : "";
    return `<g data-ext="${esc(p.ext)}" data-tip="<b>.${esc(p.ext)}</b> · ${CATN[p.cat]}<br><span>${fmtN(p.files)} files · ${fmtSize(p.bytes)}</span>">
      <circle cx="${p.x}" cy="${p.y}" r="${p.r}" fill="${ccol(p.cat)}"/>${lab}</g>`;
  }).join("")}</svg>`;
}

/* activity: 53 weeks × 7 days, sequential blue, quantiles */
function paintCalendar(a){
  const counts = a.counts, cell = 12, gap = 3, top = 16, left = 26;
  const nonzero = counts.filter(c => c).sort((x, y) => x - y);
  const q = p => nonzero.length ? nonzero[Math.min(nonzero.length - 1, Math.floor(p * nonzero.length))] : 1;
  const cuts = [q(.2), q(.45), q(.7), q(.9)];
  const level = c => !c ? 0 : 1 + cuts.filter(t => c > t).length;
  const todayMs = a.today * 86400000;
  const firstDay = new Date(todayMs - 370 * 86400000);
  const startDow = (firstDay.getUTCDay() + 6) % 7;            // Monday first
  let s = "", lastMonth = -1;
  for (let i = 0; i < counts.length; i++){
    const k = i + startDow, col = Math.floor(k / 7), row = k % 7;
    const day = new Date(todayMs - (370 - i) * 86400000);
    const x = left + col * (cell + gap), y = top + row * (cell + gap);
    if (row === 0 || i === 0){
      const mth = day.getUTCMonth();
      if (mth !== lastMonth && day.getUTCDate() <= 7){ s += `<text x="${x}" y="10">${day.toLocaleString([], { month:"short", timeZone:"UTC" })}</text>`; lastMonth = mth; }
    }
    const label = day.toLocaleDateString([], { weekday:"short", day:"numeric", month:"short", year:"numeric", timeZone:"UTC" });
    s += `<rect x="${x}" y="${y}" width="${cell}" height="${cell}" fill="var(--heat-${level(counts[i])})" data-day="${a.today - (370 - i)}"
      data-tip="<b>${fmtN(counts[i])} file${counts[i] === 1 ? "" : "s"}</b> changed<br><span>${label}</span>"/>`;
  }
  ["Mon", "Wed", "Fri"].forEach((d, i) => s += `<text x="0" y="${top + (i * 2) * (cell + gap) + 10}">${d}</text>`);
  const W = left + Math.ceil((counts.length + startDow) / 7) * (cell + gap);
  $("#insCal").innerHTML = `<svg width="${W}" height="${top + 7 * (cell + gap)}" role="img" aria-label="Files changed per day over the last year">${s}</svg>`;
}

/* folder map: reuse the space map's squarify; label colour per the palette rule */
function paintMap(m){
  ins.trail = ins.trail.length && ins.trail[ins.trail.length - 1] === m.root ? ins.trail : [...ins.trail.filter(p => p !== m.root), m.root];
  $("#insCrumbs").innerHTML = ins.trail.map((p, i) => `${i ? "<span>›</span>" : ""}<button data-root="${esc(p)}">${esc(i ? p.split("\\").filter(Boolean).pop() : p)}</button>`).join("");
  const box = $("#insMap"), W = box.clientWidth, H = box.clientHeight;
  const kids = m.kids.filter(k => k.bytes > 0).map(k => ({ ...k, size:k.bytes }));
  if (!kids.length){ box.innerHTML = `<div class="ins-load">Nothing here</div>`; return; }
  box.innerHTML = squarify(kids, 0, 0, W, H).map(({ n, x, y, w, h }) => {
    const drill = n.name !== "(files here)" && !n.more;
    return `<div class="t2${darkLabel(n.cat) ? " dk" : ""}" ${drill ? `data-root="${esc(n.path)}" tabindex="0" role="button"` : ""}
      style="left:${x}px;top:${y}px;width:${w}px;height:${h}px;background:${ccol(n.cat)}"
      data-tip="<b>${esc(n.name)}</b> · mostly ${CATN[n.cat].toLowerCase()}<br><span>${fmtSize(n.bytes)} · ${pct(n.bytes, m.bytes).toFixed(1)}%</span>">
      ${w > 70 && h > 34 ? `<b>${esc(n.name)}</b><span>${fmtSize(n.bytes)}</span>` : ""}</div>`;
  }).join("");
}

/* ── interactions ────────────────────────────────────────── */
$("#insOut").addEventListener("click", e => {
  const t = e.target;
  const mode = t.closest("#insMode button");
  if (mode){ ins.mode = mode.dataset.m; return paintInsights(); }
  const c = t.closest("[data-cat]"); if (c) return goSearch({ cats:[c.dataset.cat] });
  const x = t.closest("[data-ext]"); if (x) return goSearch({ exts:[x.dataset.ext === "(none)" ? "" : x.dataset.ext].filter(Boolean) });
  const s = t.closest("[data-scope]"); if (s) return goSearch({ scope:s.dataset.scope });
  const r = t.closest("[data-reveal]"); if (r) return act("reveal", [pathRow(r.dataset.reveal)]);
  const a = t.closest("[data-age]");
  if (a){
    const [lo, hi] = [[0, 7], [7, 30], [30, 182], [182, 365], [365, 730], [730, 36500]][+a.dataset.age];
    const now = Date.now() / 1000;
    return goSearch({ timeMin:now - hi * 86400, timeMax:now - lo * 86400 });
  }
  const day = t.closest("[data-day]");
  if (day){ const d0 = +day.dataset.day * 86400; return goSearch({ timeMin:d0, timeMax:d0 + 86399 }); }
  const root = t.closest("[data-root]");
  if (root){ ins.root = root.dataset.root; ins.trail = ins.trail.slice(0, ins.trail.indexOf(root.dataset.root) + 1); return loadInsights(true); }
  if (t.closest("[data-goclean]")) setView("clean");
});
$("#insOut").addEventListener("keydown", e => {
  const root = e.target.closest("[data-root]");
  if (root && (e.key === "Enter" || e.key === " ")){ e.preventDefault(); root.click(); }
});
window.addEventListener("resize", () => { if (app.dataset.view === "insights" && ins.data) paintInsights(); });
if (app.dataset.view === "insights") loadInsights();

$("#insPane").addEventListener("scroll", hideTip, { passive:true });
// a jump from Insights with system files included keeps them, until the search changes
filters = (orig => function(){
  const f = orig();
  if (state.forceAll){ delete f.hideSystem; delete f.hideHidden; }
  return f;
})(filters);
$("#q").addEventListener("input", () => { state.forceAll = false; });
$("#qsSys").addEventListener("change", () => { state.forceAll = false; });
