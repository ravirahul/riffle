/* Riffle 1.1: views, preview, icons, duplicates, cleanup, space map,
   summon animation, welcome and update notice. Loaded after ui.html's script. */
"use strict";
const TOK = (document.querySelector('meta[name="riffle-token"]') || {}).content || "";
const VERSION = (document.querySelector('meta[name="riffle-version"]') || {}).content || "";
const app = $("#app");
const enc = encodeURIComponent;
const api = (path, body) => fetch(path, body === undefined ? {} : {
  method:"POST", headers:{ "Content-Type":"application/json" }, body:JSON.stringify(body)
}).then(r => r.json());
const fileURL = (kind, p, s) => `/api/${kind}?t=${TOK}${s ? "&s=" + s : ""}&p=${enc(p)}`;
const extOf = p => { const m = /\.([^.\\/]{1,12})$/.exec(p); return m ? m[1].toLowerCase() : ""; };
const IMGX = new Set("png jpg jpeg gif webp avif bmp heic heif tif tiff psd svg ico".split(" "));
const VIDX = new Set("mp4 mov m4v mkv webm avi wmv 3gp".split(" "));
const SVG = {
  search:'<svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="7"/><path d="M21 21l-5.6-5.6"/></svg>',
  dupes:'<svg viewBox="0 0 24 24"><rect x="8" y="8" width="13" height="13" rx="2"/><path d="M4 16V5a2 2 0 012-2h11"/></svg>',
  clean:'<svg viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>',
  eye:'<svg viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  reveal:'<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg>',
  lock:'<svg viewBox="0 0 24 24"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/></svg>',
  key:'<svg viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></svg>',
  spark:'<svg viewBox="0 0 24 24"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8"/></svg>'
};

/* ── in-page confirm ───────────────────────────────────────── */
function ask(msg, okLabel = "OK", sub = "", danger = true){
  return new Promise(res => {
    const d = $("#dlg");
    d.querySelector("p").textContent = msg;
    d.querySelector(".sub").textContent = sub;
    const ok = d.querySelector("[data-ok]"), no = d.querySelector("[data-no]");
    ok.textContent = okLabel;
    ok.className = "btn big " + (danger ? "danger fill" : "ink");
    const done = v => { d.classList.remove("on"); d.removeEventListener("keydown", key); res(v); };
    const key = e => { if (e.key === "Escape"){ e.stopPropagation(); done(false); } };
    ok.onclick = () => done(true); no.onclick = () => done(false);
    d.onclick = e => { if (e.target === d) done(false); };
    d.addEventListener("keydown", key);
    d.classList.add("on"); no.focus();
  });
}
window.confirm = null;  // everything goes through ask()

/* ── views ────────────────────────────────────────────────── */
function setView(v, push = true){
  if (app.dataset.view === v) return;
  app.dataset.view = v;
  document.querySelectorAll(".nav [data-view]").forEach(b =>
    b.setAttribute("aria-current", b.dataset.view === v ? "page" : "false"));
  app.classList.remove("vchange"); void app.offsetWidth; app.classList.add("vchange");
  setTimeout(() => app.classList.remove("vchange"), 260);
  if (v === "dupes") dupes.open();
  if (v === "clean") clean.open();
  if (v === "search") render();
  if (push) try{ localStorage.setItem("riffle.view", v); }catch(e){}
}
$(".nav").addEventListener("click", e => {
  const b = e.target.closest("[data-view]");
  if (b) setView(b.dataset.view);
});
// typing in the search box always means search
$("#q").addEventListener("input", () => setView("search"));
$("#q").addEventListener("focus", () => { if (app.dataset.view !== "search" && $("#q").value) setView("search"); });

/* ── real Windows icons in the result list ─────────────────── */
const PER_FILE = new Set(["exe", "lnk", "ico", "url", "appref-ms", "msc", "cur", "ani"]);
const iconSrc = new Map();   // ext -> url (first path seen of that type stands in for all)
function iconURL(r){
  const p = joinPath(r);
  if (r.cat === "folder"){
    if (!iconSrc.has("<dir>")) iconSrc.set("<dir>", fileURL("icon", p, 32));
    return iconSrc.get("<dir>");
  }
  const e = (r.ext || "").toLowerCase();
  if (!e || PER_FILE.has(e)) return fileURL("icon", p, 32);
  if (!iconSrc.has(e)) iconSrc.set(e, fileURL("icon", p, 32));
  return iconSrc.get(e);
}
const badIcon = new Set();
iconFor = function(r){
  const u = iconURL(r);
  if (!TOK || badIcon.has(u))
    return `<span class="ic ic-${r.cat}">${r.cat === "folder" ? "▸" : (ICON[r.cat] || "")}</span>`;
  return `<img class="fi" src="${u}" alt="" loading="lazy" onerror="badIcon.add(this.getAttribute('src'));this.replaceWith(Object.assign(document.createElement('span'),{className:'ic ic-${r.cat}'}))">`;
};
window.badIcon = badIcon;

/* ── preview pane ──────────────────────────────────────────── */
const pv = { on:false, big:false, path:null, timer:0, seq:0 };
function togglePreview(on = !pv.on){
  pv.on = on;
  app.classList.toggle("pv-on", on);
  $("#pvBtn").classList.toggle("on", on);
  $("#pvBtn").setAttribute("aria-pressed", on);
  try{ localStorage.setItem("riffle.preview", on ? "1" : ""); }catch(e){}
  if (on) showPreview(true); else stopMedia();
}
function stopMedia(){ $("#pvStage").querySelectorAll("video,audio").forEach(m => m.pause()); }
function showPreview(force){
  if (!pv.on) return;
  clearTimeout(pv.timer);
  pv.timer = setTimeout(() => paintPreview(force), 70);
}
async function paintPreview(force){
  const r = currentRow();
  const stage = $("#pvStage"), info = $("#pvInfo");
  if (!r){
    pv.path = null;
    stage.innerHTML = "";
    info.innerHTML = `<div class="pv-empty">Select a file to preview it here.<br><kbd>Space</kbd> shows or hides this panel.</div>`;
    return;
  }
  const p = joinPath(r);
  if (p === pv.path && !force) return;
  pv.path = p;
  const seq = ++pv.seq;
  stopMedia();
  const dir = r.cat === "folder";
  info.innerHTML = `<div class="pv-name">${esc(r.name)}</div><div class="pv-path">${esc(r.dir)}</div>
    <dl class="pv-meta">
      ${dir ? "" : `<dt>Size</dt><dd>${fmtSize(r.size)} <span style="color:var(--muted)">(${r.size.toLocaleString()} bytes)</span></dd>`}
      <dt>Modified</dt><dd>${new Date(r.mtime * 1000).toLocaleString()}</dd>
      <dt>Type</dt><dd id="pvKind">${dir ? "Folder" : esc((r.ext || "file").toUpperCase())}</dd>
    </dl>
    <div class="pv-acts">
      <button class="btn ink" data-pa="open">Open</button>
      <button class="btn" data-pa="reveal">Show in Explorer</button>
      <button class="btn" data-pa="copypath">Copy path</button>
    </div>`;
  stage.innerHTML = `<img class="ico" src="${fileURL("icon", p, 256)}" alt="">`;
  if (dir) return;
  const d = await api("/api/peek", { path:p }).catch(() => ({}));
  if (seq !== pv.seq) return;
  if (d.error && !d.size) return;
  if (d.ctime) $("#pvInfo .pv-meta").insertAdjacentHTML("beforeend",
    `<dt>Created</dt><dd>${new Date(d.ctime * 1000).toLocaleString()}</dd>`);
  const src = fileURL("file", p);
  if (d.inline === "image")
    stage.innerHTML = `<img src="${src}" alt="${esc(r.name)}" onerror="this.src='${fileURL("thumb", p, 512)}'">`;
  else if (d.inline === "video")
    stage.innerHTML = `<video src="${src}" controls muted preload="metadata" poster="${fileURL("thumb", p, 512)}"></video>`;
  else if (d.inline === "audio")
    stage.innerHTML = `<audio src="${src}" controls preload="metadata"></audio>`;
  else if (d.inline === "pdf")
    stage.innerHTML = `<iframe src="${src}#toolbar=0&view=FitH" title="${esc(r.name)}"></iframe>`;
  else if (d.inline === "text"){
    stage.innerHTML = `<pre></pre>`;
    stage.firstChild.textContent = d.text;
    if (d.more) stage.insertAdjacentHTML("afterend", "");
  } else {
    // Office, PSD, anything with a Windows thumbnail handler
    const img = new Image();
    img.onload = () => { if (seq === pv.seq){ stage.innerHTML = ""; stage.appendChild(img); } };
    img.src = fileURL("thumb", p, 512);
  }
  pv.big = ["pdf", "text"].includes(d.inline);
  $("#preview").classList.toggle("big", pv.big);
}
$("#pvInfo").addEventListener("click", e => {
  const b = e.target.closest("[data-pa]");
  const r = currentRow();
  if (b && r) act(b.dataset.pa, [r]);
});
$("#pvBtn").onclick = () => togglePreview();
// Every selection change goes through render(); hook it to refresh the preview.
render = (orig => function(){ orig(); if (pv.on) showPreview(); })(render);
document.addEventListener("keydown", e => {
  if (e.key !== " " || e.ctrlKey || e.altKey || app.dataset.view !== "search") return;
  if (/INPUT|TEXTAREA|SELECT|BUTTON/.test(document.activeElement.tagName)) return;
  if (vis.on) return;
  e.preventDefault(); togglePreview();
});

/* ── duplicates ───────────────────────────────────────────── */
const dupes = {
  groups:[], folders:[], shown:60, picked:new Set(), timer:0, loaded:false,
  async open(){
    if (this.loaded) return;
    this.loaded = true;
    const s = await api("/api/dupes/status");
    if (s.running) this.poll();
    else if (s.phase === "Done") this.load();
    else this.paint();
  },
  async start(){
    const roots = $("#dScope").value.trim();
    const body = { minSize:+$("#dMin").value, roots: roots ? roots.split(/\s*;\s*/) : null };
    await api("/api/dupes/scan", body);
    this.poll();
  },
  stop(){ api("/api/dupes/scan", { stop:true }); },
  async poll(){
    clearTimeout(this.timer);
    const s = await api("/api/dupes/status").catch(() => null);
    if (!s) return;
    const pr = $("#dProg");
    pr.classList.toggle("on", s.running);
    $("#dScan").hidden = s.running; $("#dStop").hidden = !s.running;
    if (s.running){
      const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
      pr.querySelector(".bar").classList.toggle("indet", !s.total);
      pr.querySelector(".bar i").style.width = pct + "%";
      pr.querySelector(".lbl").innerHTML = `<span>${esc(s.phase)}…</span><span>${s.total ? `${s.done.toLocaleString()} / ${s.total.toLocaleString()} files · ` : ""}${fmtSize(s.bytes)} read</span>`;
      this.timer = setTimeout(() => this.poll(), document.hidden ? 4000 : 700);
    } else if (s.phase === "Done" || s.phase === "Stopped") this.load();
    else if (s.phase === "Error"){ toast("Duplicate scan failed: " + (s.error || "")); this.paint(); }
  },
  async load(){
    const d = await api("/api/dupes");
    this.groups = d.groups || []; this.folders = d.folders || [];
    this.picked = new Set(this.groups.flatMap(g => g.files.filter(f => !f.keep).map(f => f.path)));
    this.shown = 60; this.finished = d.finished;
    $("#navDupes").textContent = this.groups.length ? fmtSize(this.groups.reduce((a, g) => a + g.waste, 0)) : "";
    this.paint();
  },
  sizeOf(path){
    for (const g of this.groups) if (g.files.some(f => f.path === path)) return g.size;
    return 0;
  },
  paint(){
    const out = $("#dOut");
    if (!this.groups.length){
      out.innerHTML = this.finished
        ? `<div class="blank"><b>No duplicates found</b>Nothing over the size limit has an identical copy. Try a smaller minimum size.</div>`
        : `<div class="blank"><b>Find files that are stored twice</b>Riffle compares contents, not names, so renamed copies are caught too.<br>It reads only files that share an exact size, at low priority, and remembers what it has checked.</div>`;
      return;
    }
    let waste = 0;
    for (const g of this.groups) waste += g.waste;
    const pickedBytes = [...this.picked].reduce((a, p) => a + this.sizeOf(p), 0);
    let html = `<div class="summary">
      <div class="num">${fmtSize(waste)}<small>in ${this.groups.length.toLocaleString()} sets of duplicates</small></div>
      <div class="sp"></div>
      <button class="btn" data-d="auto">Pick extras for me</button>
      <button class="btn" data-d="none">Clear picks</button>
      <button class="btn danger fill big" data-d="recycle" ${this.picked.size ? "" : "disabled"}>Recycle ${this.picked.size.toLocaleString()} file${this.picked.size === 1 ? "" : "s"} · ${fmtSize(pickedBytes)}</button>
    </div>`;
    if (this.folders.length){
      html += `<div class="section-h">Whole folders that are copies</div>`;
      for (const f of this.folders.slice(0, 20)){
        html += `<div class="dgroup"><div class="gh"><div class="th"><img class="ico" src="${fileURL("icon", f.folders[0], 64)}" alt=""></div>
          <div class="gt"><b>${f.files} identical files</b><span>${fmtSize(f.size)} each copy</span></div>
          <div class="waste">${fmtSize(f.waste)} extra</div></div>
          ${f.folders.map((d, i) => `<div class="dfile">${i ? `<span class="pill lbl-why">copy</span>` : `<span class="pill lbl-keep">keep</span>`}
            <span class="p"><bdi>${esc(d)}</bdi></span><button class="go" title="Open folder" data-reveal="${esc(d)}">${SVG.reveal}</button></div>`).join("")}</div>`;
      }
      html += `<div class="section-h">Duplicate files</div>`;
    }
    for (const g of this.groups.slice(0, this.shown)){
      const first = g.files[0].path, e = extOf(first);
      const thumb = IMGX.has(e) || VIDX.has(e)
        ? `<img src="${fileURL("thumb", first, 96)}" alt="" loading="lazy">`
        : `<img class="ico" src="${fileURL("icon", first, 64)}" alt="" loading="lazy">`;
      const name = first.split("\\").pop();
      html += `<div class="dgroup" data-h="${g.hash}">
        <div class="gh"><div class="th" data-peek="${esc(first)}" title="Preview">${thumb}</div>
          <div class="gt"><b>${esc(name)}</b><span>${fmtSize(g.size)} × ${g.files.length} copies</span></div>
          <div class="waste">${fmtSize(g.waste)} extra</div></div>
        ${g.files.map(f => `<div class="dfile">
          <input type="checkbox" data-pick="${esc(f.path)}" ${this.picked.has(f.path) ? "checked" : ""} aria-label="Recycle this copy">
          <span class="p" title="${esc(f.path)}"><bdi>${esc(f.path)}</bdi></span>
          ${f.keep ? `<span class="pill lbl-keep">keep</span>` : `<span class="pill lbl-why">${esc(f.why)}</span>`}
          <span class="d">${fmtDate(f.mtime)}</span>
          <button class="go" title="Show in Explorer" data-reveal="${esc(f.path)}">${SVG.reveal}</button></div>`).join("")}
      </div>`;
    }
    if (this.groups.length > this.shown)
      html += `<button class="btn more" data-d="more">Show ${Math.min(100, this.groups.length - this.shown)} more of ${(this.groups.length - this.shown).toLocaleString()}</button>`;
    out.innerHTML = html;
  },
  async recycle(){
    const paths = [...this.picked];
    const bytes = paths.reduce((a, p) => a + this.sizeOf(p), 0);
    if (!await ask(`Send ${paths.length.toLocaleString()} duplicate files (${fmtSize(bytes)}) to the Recycle Bin?`,
      "Recycle", "One copy of every file is always kept. You can restore them from the Recycle Bin.")) return;
    const r = await api("/api/dupes/remove", { paths });
    toast(r.removed ? `Recycled ${r.removed.toLocaleString()} files · ${fmtSize(r.freed)} freed` : "Nothing was removed");
    this.load();
  }
};
$("#dScan").onclick = () => dupes.start();
$("#dStop").onclick = () => dupes.stop();
$("#dOut").addEventListener("change", e => {
  const c = e.target.closest("[data-pick]");
  if (!c) return;
  const g = dupes.groups.find(g => g.files.some(f => f.path === c.dataset.pick));
  if (c.checked){
    // never let every copy of a file be picked
    const others = g.files.filter(f => f.path !== c.dataset.pick && !dupes.picked.has(f.path));
    if (!others.length){ c.checked = false; return toast("Keep at least one copy"); }
    dupes.picked.add(c.dataset.pick);
  } else dupes.picked.delete(c.dataset.pick);
  const btn = $("#dOut [data-d=recycle]");
  const bytes = [...dupes.picked].reduce((a, p) => a + dupes.sizeOf(p), 0);
  btn.disabled = !dupes.picked.size;
  btn.textContent = `Recycle ${dupes.picked.size.toLocaleString()} file${dupes.picked.size === 1 ? "" : "s"} · ${fmtSize(bytes)}`;
});
$("#dOut").addEventListener("click", e => {
  const b = e.target.closest("[data-d],[data-reveal],[data-peek]");
  if (!b) return;
  if (b.dataset.reveal) return act("reveal", [pathRow(b.dataset.reveal)]);
  if (b.dataset.peek) return act("open", [pathRow(b.dataset.peek)]);
  const d = b.dataset.d;
  if (d === "more"){ dupes.shown += 100; dupes.paint(); }
  if (d === "none"){ dupes.picked.clear(); dupes.paint(); }
  if (d === "auto"){ dupes.picked = new Set(dupes.groups.flatMap(g => g.files.filter(f => !f.keep).map(f => f.path))); dupes.paint(); }
  if (d === "recycle") dupes.recycle();
});
function pathRow(p){ const i = p.lastIndexOf("\\"); return { dir:p.slice(0, i) || p, name:p.slice(i + 1) }; }

/* ── clean up ─────────────────────────────────────────────── */
const clean = {
  data:null, open_:new Set(), items:{}, picked:{}, timer:0, loaded:false,
  async open(){
    if (!this.loaded){ this.loaded = true; const s = await api("/api/cleanup"); this.data = s; s.running ? this.poll() : this.paint(); }
    if (!this.map) smap.go("C:\\");
  },
  async scan(){ await api("/api/cleanup/scan", {}); this.poll(); },
  async poll(){
    clearTimeout(this.timer);
    const s = await api("/api/cleanup").catch(() => null);
    if (!s) return;
    this.data = s;
    $("#cProg").classList.toggle("on", s.running);
    $("#cProg .lbl").innerHTML = `<span>${esc(s.phase || "Starting")}…</span><span></span>`;
    $("#cScan").disabled = s.running;
    if (s.running) this.timer = setTimeout(() => this.poll(), 800);
    else { this.items = {}; this.picked = {}; this.paint(); }
  },
  paint(){
    const out = $("#cOut"), s = this.data;
    if (!s || !s.cards || !s.cards.length){
      out.innerHTML = `<div class="blank"><b>See what's safe to clear</b>Riffle looks at temp files, caches, old installers, forgotten downloads, rebuildable project folders and more. Nothing is removed until you choose.</div>`;
      return;
    }
    const total = s.cards.filter(c => !c.done).reduce((a, c) => a + c.size, 0);
    $("#navClean").textContent = total ? fmtSize(total) : "";
    let html = `<div class="summary"><div class="num">${fmtSize(total)}<small>you could free · checked ${new Date(s.time * 1000).toLocaleTimeString([], { hour:"2-digit", minute:"2-digit" })}</small></div></div><div class="cards">`;
    for (const c of s.cards){
      const open = this.open_.has(c.key);
      const how = c.action === "recycle" ? "Recycle Bin" : c.action === "empty_bin" ? "Empty bin" : "Delete";
      html += `<div class="ccard${open ? " open" : ""}${c.done ? " done" : ""}" data-k="${c.key}">
        <div class="row1"><h3>${esc(c.title)}</h3><span class="pill lbl-${c.level}">${c.level === "safe" ? "Safe" : "Review"}</span></div>
        <div class="row1"><span class="sz">${fmtSize(c.size)}</span><span class="ct">${c.count.toLocaleString()} item${c.count === 1 ? "" : "s"}</span></div>
        <p>${esc(c.why)}</p>${c.note ? `<div class="note">${esc(c.note)}</div>` : ""}
        ${open ? this.list(c) : ""}
        <div class="acts">
          ${c.key === "dupes" && !c.count ? `<button class="btn" data-c="godupes">Find duplicates</button>` :
            c.action === "empty_bin" ? `<button class="btn danger" data-c="bin" ${c.count ? "" : "disabled"}>Empty Recycle Bin</button>` :
            `<button class="btn" data-c="toggle">${open ? "Hide files" : "Review files"}</button>
             ${c.level === "safe" || open ? `<button class="btn danger" data-c="clean" ${c.count && !c.done ? "" : "disabled"}>${open ? "Remove picked" : how === "Delete" ? "Clear all" : "Recycle all"}</button>` : ""}`}
        </div></div>`;
    }
    out.innerHTML = html + "</div>";
  },
  list(c){
    const items = this.items[c.key];
    if (!items) return `<div class="citems"><div class="blank">Loading…</div></div>`;
    const pk = this.picked[c.key];
    return `<div class="cbar"><label><input type="checkbox" data-all ${pk.size === items.length ? "checked" : ""}> Pick all ${items.length.toLocaleString()}</label>${items.length < c.count ? `<span>(largest ${items.length.toLocaleString()} shown)</span>` : ""}</div>
      <div class="citems">${items.map(i => `<div class="dfile"><input type="checkbox" data-ci="${esc(i.path)}" ${pk.has(i.path) ? "checked" : ""}>
        <span class="p" title="${esc(i.path)}"><bdi>${esc(i.path)}</bdi></span><span class="d">${fmtSize(i.size)}</span>
        <span class="d">${fmtDate(i.mtime)}</span><button class="go" title="Show in Explorer" data-reveal="${esc(i.path)}">${SVG.reveal}</button></div>`).join("")}</div>`;
  },
  async toggle(key){
    if (this.open_.has(key)) this.open_.delete(key);
    else {
      this.open_.add(key);
      if (!this.items[key]){
        this.paint();
        const d = await api("/api/cleanup/items", { key });
        this.items[key] = d.items;
        const c = this.data.cards.find(c => c.key === key);
        this.picked[key] = new Set(c.level === "safe" ? d.items.map(i => i.path) : []);
      }
    }
    this.paint();
  },
  async clean(key){
    const c = this.data.cards.find(c => c.key === key);
    let paths;
    if (this.open_.has(key)) paths = [...this.picked[key]];
    else paths = (this.items[key] || (this.items[key] = (await api("/api/cleanup/items", { key })).items)).map(i => i.path);
    if (!paths.length) return toast("Nothing picked");
    const sel = new Set(paths);
    const bytes = this.items[key].filter(i => sel.has(i.path)).reduce((a, i) => a + i.size, 0);
    const perm = c.action === "delete";
    if (!await ask(`${perm ? "Delete" : "Recycle"} ${paths.length.toLocaleString()} item${paths.length === 1 ? "" : "s"} (${fmtSize(bytes)})?`,
      perm ? "Delete" : "Recycle", perm ? "These are caches or rebuildable files, so they skip the Recycle Bin. Files in use are left alone." : "They go to the Recycle Bin, so you can restore them.")) return;
    const r = await api("/api/cleanup/remove", { paths });
    toast(`Freed ${fmtSize(r.freed)}${r.refused ? ` · ${r.refused} skipped` : ""}`);
    const gone = new Set(paths);
    this.items[key] = this.items[key].filter(i => !gone.has(i.path) || !r.removed);
    c.size = Math.max(0, c.size - r.freed); c.count = Math.max(0, c.count - r.removed);
    if (!c.count) c.done = true;
    this.picked[key] = new Set();
    this.paint();
  }
};
$("#cScan").onclick = () => clean.scan();
$("#cOut").addEventListener("click", async e => {
  const b = e.target.closest("[data-c],[data-reveal]");
  if (!b) return;
  if (b.dataset.reveal) return act("reveal", [pathRow(b.dataset.reveal)]);
  const key = b.closest("[data-k]").dataset.k;
  const c = b.dataset.c;
  if (c === "toggle") clean.toggle(key);
  if (c === "clean") clean.clean(key);
  if (c === "godupes") setView("dupes");
  if (c === "bin" && await ask("Empty the Recycle Bin?", "Empty", "This can't be undone.")){
    const r = await api("/api/cleanup/remove", { emptyBin:true });
    toast(`Freed ${fmtSize(r.freed)}`);
    const card = clean.data.cards.find(c => c.key === "bin"); card.size = 0; card.count = 0; card.done = true;
    clean.paint();
  }
});
$("#cOut").addEventListener("change", e => {
  const card = e.target.closest("[data-k]");
  if (!card) return;
  const key = card.dataset.k, pk = clean.picked[key];
  if (e.target.matches("[data-all]")){
    clean.picked[key] = new Set(e.target.checked ? clean.items[key].map(i => i.path) : []);
    clean.paint();
  } else if (e.target.dataset.ci){
    e.target.checked ? pk.add(e.target.dataset.ci) : pk.delete(e.target.dataset.ci);
  }
});

/* ── space map: squarified treemap of folder sizes ─────────── */
const smap = {
  trail:[],
  async go(root){
    clean.map = true;
    $("#smap").innerHTML = `<div class="blank">Measuring ${esc(root)}…</div>`;
    const t = await api("/api/spacemap", { root, depth:2 });
    if (!this.trail.length || this.trail[this.trail.length - 1] !== root){
      const i = this.trail.indexOf(root);
      this.trail = i >= 0 ? this.trail.slice(0, i + 1) : [...this.trail, root];
    }
    this.root = root; this.tree = t; this.paint();
  },
  paint(){
    $("#crumbs").innerHTML = this.trail.map((p, i) =>
      `${i ? "<span>›</span>" : ""}<button data-go="${esc(p)}">${esc(i ? p.split("\\").filter(Boolean).pop() : p)}</button>`).join("");
    $("#smapTotal").textContent = fmtSize(this.tree.size);
    const box = $("#smap"), W = box.clientWidth, H = box.clientHeight;
    const kids = this.tree.kids.filter(k => k.size > 0);
    if (!kids.length){ box.innerHTML = `<div class="blank">Nothing indexed here</div>`; return; }
    const rects = squarify(kids, 0, 0, W, H);
    const max = kids[0].size;
    box.innerHTML = rects.map(({ n, x, y, w, h }, i) => {
      const t = n.size / max;
      const leaf = !n.kids.length || n.name === "(files)" || / more$/.test(n.name);
      const pct = i === 0 ? 100 : Math.round(15 + 55 * t);
      const bg = pct === 100 ? "var(--mark)" : `color-mix(in srgb, var(--mark) ${pct}%, var(--card))`;
      const onMark = pct >= 45 || !document.documentElement.dataset.theme || document.documentElement.dataset.theme === "light";
      const path = this.root.replace(/\\?$/, "\\") + n.name;
      return `<div class="tile${leaf ? " leaf" : ""}${onMark ? " on-mark" : ""}" ${leaf ? "" : `data-go="${esc(path)}" tabindex="0" role="button"`}
        style="left:${x}px;top:${y}px;width:${w}px;height:${h}px;background:${bg};color:var(--ink)" title="${esc(n.name)} · ${fmtSize(n.size)}">
        ${w > 60 && h > 30 ? `<b>${esc(n.name)}</b><span>${fmtSize(n.size)}</span>` : ""}</div>`;
    }).join("");
  }
};
function squarify(items, x, y, w, h){
  const total = items.reduce((a, n) => a + n.size, 0), out = [];
  let rest = items.slice(), area = w * h;
  const scale = area / total;
  const worst = (row, len) => {
    const s = row.reduce((a, n) => a + n.size * scale, 0);
    let mx = 0;
    for (const n of row){ const a = n.size * scale; mx = Math.max(mx, (len * len * a) / (s * s), (s * s) / (len * len * a)); }
    return mx;
  };
  while (rest.length){
    const len = Math.min(w, h);
    let row = [rest[0]], i = 1;
    while (i < rest.length && worst([...row, rest[i]], len) <= worst(row, len)) row.push(rest[i++]);
    rest = rest.slice(row.length);
    const s = row.reduce((a, n) => a + n.size * scale, 0);
    if (w >= h){
      const cw = s / h; let cy = y;
      for (const n of row){ const ch = n.size * scale / cw; out.push({ n, x, y:cy, w:cw, h:ch }); cy += ch; }
      x += cw; w -= cw;
    } else {
      const ch = s / w; let cx = x;
      for (const n of row){ const cw = n.size * scale / ch; out.push({ n, x:cx, y, w:cw, h:ch }); cx += cw; }
      y += ch; h -= ch;
    }
  }
  return out.map(r => ({ ...r, x:Math.round(r.x), y:Math.round(r.y), w:Math.max(0, Math.round(r.w)), h:Math.max(0, Math.round(r.h)) }));
}
$("#cleanPane").addEventListener("click", e => {
  const t = e.target.closest("[data-go]");
  if (t) smap.go(t.dataset.go);
});
$("#smap").addEventListener("keydown", e => {
  const t = e.target.closest("[data-go]");
  if (t && (e.key === "Enter" || e.key === " ")){ e.preventDefault(); smap.go(t.dataset.go); }
});
$("#smapDrive").addEventListener("change", e => { smap.trail = []; smap.go(e.target.value); });
window.addEventListener("resize", () => { if (smap.tree && app.dataset.view === "clean") smap.paint(); });

/* ── summon: play the riffle when the window comes back ────── */
let blurredAt = 0;
window.addEventListener("blur", () => { blurredAt = Date.now(); });
function summon(){
  if (Date.now() - blurredAt < 900) return;   // quick alt-tab back: stay calm
  app.classList.remove("summon"); void app.offsetWidth;
  // stagger only the first rows that are on screen
  const rows = [...$("#rows").children].filter(r => r.offsetTop + r.offsetHeight > $("#scroll").scrollTop).slice(0, 14);
  rows.forEach((r, i) => { r.classList.add("rf"); r.style.animationDelay = (40 + i * 18) + "ms"; });
  app.classList.add("summon");
  if (app.dataset.view === "search" || !app.dataset.view){ $("#q").focus(); $("#q").select(); }
  setTimeout(() => { app.classList.remove("summon"); rows.forEach(r => { r.classList.remove("rf"); r.style.animationDelay = ""; }); }, 650);
}
window.addEventListener("focus", summon);
document.addEventListener("visibilitychange", () => { if (document.hidden) blurredAt = Date.now(); });

/* ── quieter polling while the window is hidden ────────────── */
const _setTimeout = window.setTimeout.bind(window);
poll = (orig => async function(){
  if (document.hidden){ clearTimeout(pollTimer); pollTimer = _setTimeout(poll, 15000); return; }
  return orig();
})(poll);
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

/* ── welcome (first run) ───────────────────────────────────── */
async function welcome(){
  let seen = false;
  try{ seen = !!localStorage.getItem("riffle.welcomed"); }catch(e){}
  if (seen) return;
  const w = $("#welcome");
  const st = await api("/api/autostart").catch(() => ({ on:true }));
  $("#wAuto").checked = st.on !== false;
  w.classList.add("on");
  $("#wGo").focus();
  $("#wGo").onclick = async () => {
    await api("/api/autostart", { on:$("#wAuto").checked }).catch(() => {});
    try{ localStorage.setItem("riffle.welcomed", "1"); }catch(e){}
    w.classList.remove("on");
    $("#q").focus();
  };
}

/* ── update notice ─────────────────────────────────────────── */
async function checkUpdate(){
  const u = await api("/api/update").catch(() => null);
  if (!u || !u.available) return;
  const b = document.createElement("button");
  b.className = "upd"; b.textContent = `Riffle ${u.latest} is ready · Update`;
  b.title = u.notes || "";
  b.onclick = () => api("/api/update/open", {});
  $(".foot").insertBefore(b, $("#reindex"));
}

/* ── optional visual-search pack ───────────────────────────── */
paintVisual = (orig => function(s){
  if (!s.pack) return orig(s);
  const st = $("#visStat"), p = s.pack;
  $("#visPick").hidden = $("#visPath").hidden = $("#visSuggest").hidden = true;
  $("#visFolders").innerHTML = "";
  if (p.state === "downloading" || p.state === "unpacking"){
    const pct = p.total ? Math.round(100 * p.got / p.total) : 0;
    st.innerHTML = (p.state === "unpacking" ? "Unpacking…" : `Downloading ${fmtSize(p.got)} of ${fmtSize(p.total)}`)
      + `<div class="bar"><i style="width:${pct}%"></i></div>`;
    clearTimeout(vis.timer); vis.timer = setTimeout(pollVisual, 700);
    return;
  }
  st.innerHTML = `Find the original of any image or video, even after crops and filters. It's an optional
    download (about 90 MB) that runs fully on this PC.
    ${p.state === "error" ? `<br><span style="color:var(--warn)">${esc(p.error)}</span>` : ""}
    <button class="btn ink" id="visGet" style="margin-top:8px">${p.state === "error" ? "Try again" : "Get visual search"}</button>`;
  $("#visGet").onclick = async () => { await api("/api/visual/install", {}); pollVisual(); };
})(paintVisual);

/* ── boot ─────────────────────────────────────────────────── */
$("#ver").textContent = VERSION ? "v" + VERSION : "";
try{ if (localStorage.getItem("riffle.preview")) togglePreview(true); }catch(e){}
app.dataset.view = "search";
try{ const v = localStorage.getItem("riffle.view"); if (v && v !== "search") setView(v, false); }catch(e){}
api("/api/status").then(s => {
  const sel = $("#smapDrive");
  sel.innerHTML = (s.drives || ["C:\\"]).map(d => `<option>${esc(d)}</option>`).join("");
  $("#wDrives").textContent = (s.drives || []).join("  ");
}).catch(() => {});
welcome();
setTimeout(checkUpdate, 4000);
