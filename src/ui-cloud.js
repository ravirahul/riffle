/* Cloud storage: a badge on results inside sync folders (OneDrive, Dropbox, Google Drive...) and a
   Cloud filter in the rail. Hidden entirely when this PC has no cloud folders. Loaded after ui-settings.js. */
"use strict";
const CLOUD_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 18h10a4 4 0 00.6-8A6 6 0 006 9.5 4.3 4.3 0 007 18z"/></svg>';
state.cloud = "";
state.cloudCounts = [];

filters = (orig => function(){
  const f = orig();
  if (state.cloud) f.cloud = state.cloud;
  return f;
})(filters);
isHome = (orig => function(){ return orig() && !state.cloud; })(isHome);
fetchPage = (orig => async function(p, token){
  const d = await orig(p, token);
  if (d && p === 0) state.cloudCounts = d.clouds || [];
  return d;
})(fetchPage);

/* rail group, before Folder */
$("#scopeField").closest(".grp").insertAdjacentHTML("beforebegin",
  `<div class="grp" id="cloudGrp" hidden><h3>Cloud</h3><div class="chips" id="cloudChips" aria-label="Cloud storage"></div>
   <div class="hint">Files your sync apps keep on this PC. Online-only files aren't downloaded unless you open them.</div></div>`);
function paintClouds(){
  const list = state.cloudCounts;
  $("#cloudGrp").hidden = !list.length && !state.cloud;
  if (!list.length) return;
  const total = list.reduce((a, [, n]) => a + n, 0);
  $("#cloudChips").innerHTML = [["any", "All clouds", total], ...list.map(([p, n]) => [p, p, n])].map(([v, label, n]) =>
    `<button class="chip" data-cloud="${esc(v)}" aria-pressed="${state.cloud === v}">${label === "All clouds" ? CLOUD_SVG : ""}${esc(label)} <span class="n">${n.toLocaleString()}</span></button>`).join("");
}
paintFacets = (orig => function(c){ orig(c); paintClouds(); })(paintFacets);
$("#cloudChips").addEventListener("click", e => {
  const b = e.target.closest("[data-cloud]"); if (!b) return;
  state.cloud = state.cloud === b.dataset.cloud ? "" : b.dataset.cloud;
  scheduleSearch(0);
});
$("#clearAll").addEventListener("click", () => { if (state.cloud){ state.cloud = ""; scheduleSearch(0); } });

/* row badge: after each render, tag visible rows that live in a cloud folder */
render = (orig => function(){
  orig();
  for (const el of $("#rows").children){
    const i = +el.dataset.i, rows = state.pages.get(Math.floor(i / PAGE));
    const r = rows && rows[i % PAGE];
    if (!r || !r.cloud || el.querySelector(".cb")) continue;
    const label = r.online ? `${r.cloud}, online only` : `${r.cloud}, on this PC`;
    el.querySelector(".c-name").insertAdjacentHTML("beforeend",
      `<span class="cb${r.online ? " off" : ""}" title="${esc(label)}">${CLOUD_SVG}<span class="sr">${esc(label)}</span></span>`);
  }
})(render);
