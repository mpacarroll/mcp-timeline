// History Studio front end. Vanilla JS, no dependencies, no network calls
// beyond this local server. All data is rendered with textContent, never
// innerHTML, because labels and captions are user text.
"use strict";

const TOKEN = document.querySelector('meta[name="studio-token"]').content;
const state = { trips: [], tripId: null, data: null, tab: "timeline", lastGrant: null };

// ------------------------------------------------------------ helpers
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
const svgEl = (tag, attrs) => {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  return n;
};

async function api(method, path, body) {
  const res = await fetch("/api" + path, {
    method,
    headers: { "X-Studio-Token": TOKEN, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const ctype = res.headers.get("Content-Type") || "";
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).error || msg; } catch (_) { /* not JSON */ }
    throw new Error(msg);
  }
  if (res.headers.get("Content-Disposition")) return res;
  return ctype.includes("json") ? res.json() : res.text();
}

function toast(msg, isError) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show" + (isError ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.className = ""), isError ? 6000 : 2500);
}

async function act(fn, okMsg) {
  try {
    const out = await fn();
    if (okMsg) toast(okMsg);
    return out;
  } catch (e) {
    toast(e.message, true);
    return undefined;
  }
}

const hhmm = (iso) => (iso ? iso.slice(11, 16) : "");
function dayOffset(a, b) {
  if (!a || !b) return 0;
  return Math.round((Date.parse(b.slice(0, 10)) - Date.parse(a.slice(0, 10))) / 86400000);
}
function whenText(it) {
  if (!it.start_local) return "";
  let s = hhmm(it.start_local);
  if (it.end_local) {
    s += "-" + hhmm(it.end_local);
    const off = dayOffset(it.start_local, it.end_local);
    if (off) s += ` (+${off}d)`;
  }
  return s;
}
const displayLabel = (it) => it.label || it.source_label;

// ------------------------------------------------------------ trips
async function loadTrips() {
  const out = await api("GET", "/trips");
  state.trips = out.trips;
  const list = document.getElementById("trip-list");
  list.replaceChildren(...state.trips.map((t) =>
    el("li", null, el("button", {
      "aria-current": t.trip_id === state.tripId ? "true" : "false",
      onclick: () => openTrip(t.trip_id),
    }, t.title, el("small", null, `${t.start_date} to ${t.end_date} · ${t.items} items`)))));
  if (!state.trips.length) list.append(el("li", { class: "hint" }, "No trips yet."));
  const h = await api("GET", "/history");
  document.getElementById("history-range").textContent = h.first_date
    ? `History on file: ${h.first_date} to ${h.last_date} (${h.visits} visits).`
    : "No history ingested yet. Run ingest.py first.";
}

async function openTrip(id) {
  state.tripId = id;
  state.data = await api("GET", "/trips/" + id);
  document.getElementById("empty").hidden = true;
  document.getElementById("trip").hidden = false;
  const t = state.data.trip;
  document.getElementById("trip-title").textContent = t.title;
  document.getElementById("trip-dates").textContent =
    `${t.start_date} to ${t.end_date} · last edited ${t.updated_utc.replace("T", " ").replace("Z", " UTC")}`;
  const badge = document.getElementById("proposal-badge");
  badge.hidden = !state.data.proposals.length;
  badge.textContent = state.data.proposals.length;
  render();
  loadTrips();
}
const refresh = () => openTrip(state.tripId);

function setTab(tab) {
  state.tab = tab;
  for (const b of document.querySelectorAll(".tabs button"))
    b.setAttribute("aria-selected", b.dataset.tab === tab ? "true" : "false");
  for (const p of document.querySelectorAll(".panel")) p.hidden = p.dataset.panel !== tab;
  render();
}

function render() {
  if (!state.data) return;
  const panel = document.querySelector(`.panel[data-panel="${state.tab}"]`);
  const fn = { timeline: renderTimeline, story: renderStory, privacy: renderPrivacy,
    share: renderShare, agents: renderAgents, danger: renderDanger }[state.tab];
  panel.replaceChildren(...fn());
}

// ------------------------------------------------------------ timeline
function sketch(items) {
  const pts = [];
  for (const it of items) {
    if (it.lat != null) pts.push([it.lat, it.lng]);
    if (it.end_lat != null) pts.push([it.end_lat, it.end_lng]);
  }
  if (pts.length < 2) return el("p", { class: "hint" }, "No coordinates to sketch for this day.");
  const lats = pts.map((p) => p[0]), lngs = pts.map((p) => p[1]);
  const [minLat, maxLat, minLng, maxLng] = [Math.min(...lats), Math.max(...lats), Math.min(...lngs), Math.max(...lngs)];
  const W = 220, H = 180, pad = 14;
  const kx = Math.cos(((minLat + maxLat) / 2) * Math.PI / 180);
  const spanX = Math.max((maxLng - minLng) * kx, 1e-6), spanY = Math.max(maxLat - minLat, 1e-6);
  const s = Math.min((W - 2 * pad) / spanX, (H - 2 * pad) / spanY);
  const X = (lng) => pad + (lng - minLng) * kx * s + ((W - 2 * pad) - spanX * s) / 2;
  const Y = (lat) => H - pad - (lat - minLat) * s - ((H - 2 * pad) - spanY * s) / 2;
  const svg = svgEl("svg", { class: "sketch", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "Sketch of the day's stops and routes, no map tiles" });
  for (const it of items) {
    if (it.kind !== "movement" || it.lat == null || it.end_lat == null) continue;
    if (it.mode === "flying") continue;
    svg.append(svgEl("line", { class: "leg", x1: X(it.lng), y1: Y(it.lat), x2: X(it.end_lng), y2: Y(it.end_lat) }));
  }
  for (const it of items) {
    if (it.kind !== "visit" || it.lat == null) continue;
    const c = svgEl("circle", { class: "stop" + (it.redaction ? " masked" : ""), cx: X(it.lng), cy: Y(it.lat), r: 4 });
    const title = svgEl("title", {});
    title.textContent = displayLabel(it) + (it.redaction ? " (redacted in exports)" : "");
    c.append(title);
    svg.append(c);
  }
  return svg;
}

function itemRow(it) {
  const tags = [];
  if (it.redaction) tags.push(el("span", { class: "tag redacted" },
    it.redaction.action === "remove" ? "removed from exports" : `shown as "${it.redaction.replacement}"`));
  if (it.label || it.caption) tags.push(el("span", { class: "tag edited" }, "edited"));
  if (it.hidden) tags.push(el("span", { class: "tag" }, "hidden"));
  if (it.source === "raw fixes") tags.push(el("span", { class: "tag" }, "reconstructed"));
  let what;
  if (it.kind === "movement") {
    what = el("div", { class: "what" }, `${it.mode}${it.km ? `, ${it.km} km` : ""}`,
      it.route_points ? el("span", { class: "meta" }, ` · ${it.route_points} route points`) : null, tags);
  } else if (it.kind === "note") {
    what = el("div", { class: "what" }, el("strong", null, "Note: "), it.caption, tags);
  } else {
    what = el("div", { class: "what" }, el("strong", null, displayLabel(it)), tags,
      it.label ? el("div", { class: "meta" }, `source said: ${it.source_label}`) : null);
  }
  if (it.caption && it.kind !== "note") what.append(el("div", { class: "meta" }, it.caption));
  const actions = el("div", { class: "actions" },
    it.kind === "visit" ? el("button", { class: "ghost", onclick: () => editLabel(it) }, "Rename") : null,
    it.kind !== "note" ? el("button", { class: "ghost", onclick: () => editCaption(it) }, "Caption") : null,
    el("button", { class: "ghost", onclick: () => toggleHidden(it),
      "aria-label": (it.hidden ? "Show " : "Hide ") + displayLabel(it) }, it.hidden ? "Show" : "Hide"),
    el("button", { class: "ghost", onclick: () => inspect(it.item_id) }, "Source"),
    el("button", { class: "danger", onclick: () => deleteItem(it),
      "aria-label": "Delete " + displayLabel(it) }, "Delete"));
  return el("li", { class: `item ${it.kind}${it.hidden ? " hidden-item" : ""}` },
    el("div", { class: "when" }, whenText(it)), what, actions);
}

function renderTimeline() {
  const days = state.data.days;
  if (!days.length) return [el("p", { class: "hint" }, "This trip has no items. Pull new history, or add a note.")];
  return days.map((d) => el("section", { class: "day" },
    el("div", { class: "day-head" }, el("h3", null, d.date),
      el("button", { class: "ghost", onclick: () => addNote(d.date) }, "Add note")),
    el("div", { class: "day-grid" },
      el("ul", { class: "items" }, d.items.map(itemRow)),
      sketch(d.items))));
}

async function editLabel(it) {
  const v = prompt(`Name shown for this stop (leave empty to use "${it.source_label}")`, it.label || "");
  if (v === null) return;
  if (await act(() => api("PATCH", "/items/" + it.item_id, { label: v }), "Saved")) {
    if (v && it.place_id && confirm(`Also name every visit to this place "${v}" in future trips?`))
      await act(() => api("PUT", `/places/${it.place_id}/label`, { label: v }), "Place named");
    refresh();
  }
}
async function editCaption(it) {
  const v = prompt("Caption", it.caption || "");
  if (v === null) return;
  if (await act(() => api("PATCH", "/items/" + it.item_id, { caption: v }), "Saved")) refresh();
}
async function toggleHidden(it) {
  if (await act(() => api("PATCH", "/items/" + it.item_id, { hidden: !it.hidden }),
    it.hidden ? "Shown again" : "Hidden from every export")) refresh();
}
async function deleteItem(it) {
  if (!confirm(`Delete "${displayLabel(it)}" from this trip? Its name is also removed from your story and captions. Your history is not changed; use Delete > Purge for that.`)) return;
  if (await act(() => api("DELETE", "/items/" + it.item_id), "Deleted")) refresh();
}
async function addNote(date) {
  const text = prompt(`Note for ${date}`);
  if (!text) return;
  if (await act(() => api("POST", `/trips/${state.tripId}/notes`, { date, text }), "Note added")) refresh();
}

async function inspect(itemId) {
  const out = await act(() => api("GET", `/items/${itemId}/source`));
  if (!out) return;
  const body = document.getElementById("inspector-body");
  const kv = (obj) => el("dl", { class: "kv" }, Object.entries(obj).flatMap(([k, v]) =>
    [el("dt", null, k.replaceAll("_", " ")), el("dd", null, v === null || v === "" ? "none" : String(v))]));
  body.replaceChildren(
    el("p", { class: "hint" }, `Source: ${out.source}${out.source_table ? ` (${out.source_table} table)` : ""}. Captured ${out.captured_utc}.`),
    el("h3", null, "Source row status"),
    el("p", null, out.source_status),
    el("h3", null, "As the source recorded it"), kv(out.as_captured),
    el("p", { class: "hint" }, `${out.route_points} route points captured.`),
    out.place_record ? el("h3", null, "The place in your history") : null,
    out.place_record ? kv({ "source category": out.place_record.semantic_type, "resolved name": out.place_record.resolved_name,
      visits: out.place_record.visit_count, "first seen": out.place_record.first_seen, "last seen": out.place_record.last_seen }) : null,
    el("h3", null, "Your changes"), kv(out.your_changes),
    el("h3", null, "Edit history"),
    out.edit_history.length
      ? el("table", null, el("tr", null, el("th", null, "When"), el("th", null, "Field"), el("th", null, "Was"), el("th", null, "Now")),
        out.edit_history.map((e) => el("tr", null, el("td", null, e.at_utc), el("td", null, e.field), el("td", null, e.old ?? ""), el("td", null, e.new ?? ""))))
      : el("p", { class: "hint" }, "No edits."),
    el("h3", null, "Redactions that apply"),
    out.redactions.length
      ? el("ul", null, out.redactions.map((r) => el("li", null, `${r.kind} redaction: ${r.action} as "${r.replacement}"`)))
      : el("p", { class: "hint" }, "None. This item appears in exports, subject to each share's settings."),
    out.source_rows.length ? el("details", null, el("summary", null, "Raw source rows"),
      el("pre", null, JSON.stringify(out.source_rows, null, 2))) : null);
  document.getElementById("inspector").showModal();
}

// ------------------------------------------------------------ story
function renderStory() {
  const out = [el("p", { class: "hint" },
    "Each day starts with a plain draft built from what that day's shared view would show. Write over it in your own words. Names of anything you redact are replaced in your text automatically when it is exported.")];
  for (const d of state.data.days) {
    const ta = el("textarea", { "aria-label": `Story for ${d.date}` });
    ta.value = d.story || d.story_draft || "";
    out.push(el("section", { class: "day" },
      el("div", { class: "day-head" }, el("h3", null, d.date),
        el("span", { class: "tag" + (d.story ? " edited" : "") }, d.story ? "written by you" : "auto-draft")),
      ta,
      el("div", { class: "row" },
        el("button", { onclick: async () => {
          if (await act(() => api("PUT", `/trips/${state.tripId}/story/${d.date}`, { body: ta.value }), "Story saved")) refresh();
        } }, "Save"),
        d.story ? el("button", { class: "ghost", onclick: async () => {
          if (await act(() => api("PUT", `/trips/${state.tripId}/story/${d.date}`, { body: "" }), "Back to the draft")) refresh();
        } }, "Revert to draft") : null)));
  }
  return out;
}

// ------------------------------------------------------------ privacy
function renderPrivacy() {
  const d = state.data;
  const placeName = (pid) => (d.places.find((p) => p.place_id === pid) || {}).source_label || pid;
  const allItems = d.days.flatMap((x) => x.items);
  const itemName = (iid) => { const it = allItems.find((i) => i.item_id === iid); return it ? `${it.local_date} ${displayLabel(it)}` : iid; };
  const describe = (r) => ({
    place: `Place: ${placeName(r.target)}`,
    item: `One item: ${itemName(r.target)}`,
    date: `Whole day: ${r.target}`,
    area: `Area: ${r.radius_m} m around ${Number(r.lat).toFixed(4)}, ${Number(r.lng).toFixed(4)}`,
  })[r.kind];
  const list = el("ul", { class: "list" }, d.redactions.length ? d.redactions.map((r) => el("li", null,
    el("div", null, el("strong", null, describe(r)),
      el("div", { class: "meta" }, `${r.action === "remove" ? "Removed" : `Shown as "${r.replacement}"`} · ${r.trip_id ? "this trip" : "every trip"}${r.note ? ` · note: ${r.note}` : ""}`)),
    el("button", { class: "ghost", onclick: async () => {
      if (confirm("Remove this redaction? The item will appear in exports again.") &&
        await act(() => api("DELETE", "/redactions/" + r.redaction_id), "Redaction removed")) refresh();
    } }, "Remove"))) : [el("li", { class: "hint" }, "No redactions yet. Everything in this trip can appear in exports, subject to each share's settings.")]);

  const kind = el("select", { name: "kind" },
    ["place", "area", "item", "date"].map((k) => el("option", { value: k }, { place: "A place (every visit)", area: "An area (radius around a point)", item: "One item", date: "A whole day" }[k])));
  const placeSel = el("select", { name: "target_place" }, d.places.map((p) => el("option", { value: p.place_id }, p.source_label)));
  const itemSel = el("select", { name: "target_item" }, allItems.filter((i) => i.kind !== "movement").map((i) => el("option", { value: i.item_id }, itemName(i.item_id))));
  const dateSel = el("select", { name: "target_date" }, d.days.map((x) => el("option", { value: x.date }, x.date)));
  const areaFrom = el("select", { name: "area_from", "aria-label": "Center the area on a stop" },
    el("option", { value: "" }, "Center on a stop..."),
    allItems.filter((i) => i.kind === "visit" && i.lat != null).map((i) => el("option", { value: `${i.lat},${i.lng}` }, itemName(i.item_id))));
  const lat = el("input", { name: "lat", inputmode: "decimal", placeholder: "38.7361" });
  const lng = el("input", { name: "lng", inputmode: "decimal", placeholder: "-9.1543" });
  const radius = el("input", { name: "radius_m", type: "number", min: 25, max: 50000, value: 250 });
  areaFrom.addEventListener("change", () => { if (areaFrom.value) { const [a, b] = areaFrom.value.split(","); lat.value = a; lng.value = b; } });
  const fields = {
    place: el("label", null, "Place", placeSel),
    item: el("label", null, "Item", itemSel),
    date: el("label", null, "Day", dateSel),
    area: el("div", null, areaFrom, el("label", null, "Latitude", lat), el("label", null, "Longitude", lng), el("label", null, "Radius in meters", radius)),
  };
  const holder = el("div", null, fields.place);
  kind.addEventListener("change", () => holder.replaceChildren(fields[kind.value]));
  const action = el("select", { name: "action" }, el("option", { value: "mask" }, "Mask: keep that something happened, hide what and where"), el("option", { value: "remove" }, "Remove: leave no trace in exports"));
  const replacement = el("input", { name: "replacement", maxlength: 60, placeholder: "Private place" });
  const scope = el("select", { name: "scope" }, el("option", { value: "trip" }, "This trip only"), el("option", { value: "all" }, "Every trip, now and future"));
  const note = el("input", { name: "note", maxlength: 200, placeholder: "Only you see this" });
  const form = el("form", { class: "card", onsubmit: async (ev) => {
    ev.preventDefault();
    const k = kind.value;
    const body = { kind: k, action: action.value, replacement: replacement.value, note: note.value,
      trip_id: scope.value === "trip" ? state.tripId : null };
    if (k === "place") body.target = placeSel.value;
    if (k === "item") body.target = itemSel.value;
    if (k === "date") body.target = dateSel.value;
    if (k === "area") Object.assign(body, { lat: lat.value, lng: lng.value, radius_m: radius.value });
    if (await act(() => api("POST", "/redactions", body), "Redaction added. Earlier exports are now marked stale.")) refresh();
  } }, el("h3", null, "Add a redaction"), el("label", null, "What", kind), holder,
    el("label", null, "How", action), el("label", null, "Replacement label", replacement),
    el("label", null, "Applies to", scope), el("label", null, "Private note", note),
    el("button", { type: "submit" }, "Add redaction"));
  return [el("p", { class: "hint" }, "Redactions apply to every export, share link and agent grant, including your own archive. Each share's settings can show less, never more."),
    el("div", { class: "split" }, form, el("div", null, el("h3", null, "Active redactions"), list))];
}

// ------------------------------------------------------------ share
function profileEditor(onChange) {
  const presets = state.data.profiles;
  const base = el("select", { name: "preset", "aria-label": "Start from a preset" },
    Object.keys(presets).map((k) => el("option", { value: k, selected: k === "family" }, k)));
  const times = el("select", { name: "times" }, ["exact", "part_of_day", "none"].map((v) => el("option", { value: v }, v.replaceAll("_", " "))));
  const coords = el("select", { name: "coordinates" }, ["exact", "neighborhood", "city", "none"].map((v) => el("option", { value: v }, v)));
  const flags = ["routes", "captions", "story", "movement", "sources"].map((f) => {
    const cb = el("input", { type: "checkbox", name: f });
    return [f, cb, el("label", null, cb, f === "sources" ? "provenance" : f)];
  });
  const dayBoxes = state.data.days.map((d) => { const cb = el("input", { type: "checkbox", value: d.date, checked: true }); return [cb, el("label", null, cb, d.date)]; });
  const load = () => {
    const p = presets[base.value];
    times.value = p.times; coords.value = p.coordinates;
    for (const [f, cb] of flags) cb.checked = !!p[f];
    for (const [cb] of dayBoxes) cb.checked = true;
    onChange();
  };
  base.addEventListener("change", load);
  for (const n of [times, coords, ...flags.map((f) => f[1]), ...dayBoxes.map((d) => d[0])]) n.addEventListener("change", onChange);
  const value = () => {
    const days = dayBoxes.filter(([cb]) => cb.checked).map(([cb]) => cb.value);
    const p = { base: base.value, name: base.value, times: times.value, coordinates: coords.value,
      days: days.length === dayBoxes.length ? null : days };
    for (const [f, cb] of flags) p[f] = cb.checked;
    const preset = presets[base.value];
    const same = ["times", "coordinates", "routes", "captions", "story", "movement", "sources"].every((k) => p[k] === preset[k]) && p.days === null;
    if (!same) p.name = base.value + " (custom)";
    return p;
  };
  const node = el("div", { class: "card" }, el("h3", null, "What this audience sees"),
    el("label", null, "Start from", base), el("label", null, "Times", times), el("label", null, "Coordinates", coords),
    el("div", { class: "checks" }, flags.map((f) => f[2])),
    el("h3", null, "Days"), el("div", { class: "checks" }, dayBoxes.map((d) => d[1])));
  setTimeout(load, 0);
  return { node, value };
}

function renderPreview(view) {
  const out = [el("p", { class: "hint" },
    `${view.redacted_items} item(s) redacted · times: ${view.disclosure.times.replaceAll("_", " ")} · coordinates: ${view.disclosure.coordinates} · routes ${view.disclosure.routes ? "included" : "left out"}`),
  el("h3", null, view.trip.title)];
  for (const d of view.days) {
    out.push(el("h4", null, d.date));
    if (d.story) out.push(el("p", null, d.story));
    out.push(el("ul", null, d.items.map((e) => {
      const when = e.start ? `${e.start}${e.end ? "-" + e.end : ""}${e.end_day_offset ? ` (+${e.end_day_offset}d)` : ""}` : e.part_of_day || "";
      const what = e.kind === "movement" ? `${e.mode}${e.km ? `, ${e.km} km` : ""}` : e.kind === "note" ? `Note: ${e.caption || ""}` : e.label;
      const where = e.lat != null ? ` · ${e.lat}, ${e.lng}` : "";
      const route = e.route ? ` · route of ${e.route.length} points` : "";
      return el("li", { class: e.redacted ? "redacted" : "" }, `${when} ${what}${e.caption && e.kind !== "note" ? ` (${e.caption})` : ""}${where}${route}`.trim());
    })));
  }
  return out;
}

function renderShare() {
  const preview = el("div", { class: "preview", "aria-live": "polite" }, "Loading preview...");
  let pending = 0;
  const update = async () => {
    const mine = ++pending;
    const view = await act(() => api("GET", `/trips/${state.tripId}/preview?custom=${encodeURIComponent(JSON.stringify(editor.value()))}`));
    if (view && mine === pending) preview.replaceChildren(...renderPreview(view));
  };
  const editor = profileEditor(update);
  const formats = el("div", { class: "row" }, state.data.formats.map((f) => el("button", { class: "ghost", onclick: async () => {
    const res = await act(() => api("GET", `/trips/${state.tripId}/export?format=${f}&custom=${encodeURIComponent(JSON.stringify(editor.value()))}`));
    if (!res) return;
    const blob = await res.blob();
    const name = (res.headers.get("Content-Disposition").match(/filename="([^"]+)"/) || [])[1] || `trip.${f}`;
    const a = el("a", { href: URL.createObjectURL(blob), download: name });
    document.body.append(a); a.click(); a.remove();
    toast(`Downloaded ${name}`);
    refresh();
  } }, f.toUpperCase())));
  const audience = el("input", { maxlength: 80, placeholder: "Mom and Dad" });
  const shareForm = el("form", { class: "card", onsubmit: async (ev) => {
    ev.preventDefault();
    const out = await act(() => api("POST", `/trips/${state.tripId}/shares`, { audience: audience.value, profile: editor.value() }), "Share page created");
    if (out) refresh();
  } }, el("h3", null, "Share page"), el("p", { class: "hint" }, "A read-only page on this machine with exactly the preview's contents. It always reflects your latest redactions and deletions, and you can revoke it."),
    el("label", null, "Who it is for", audience), el("button", { type: "submit" }, "Create share page"));
  const shares = el("ul", { class: "list" }, state.data.shares.length ? state.data.shares.map((s) => el("li", null,
    el("div", null, el("strong", null, s.audience), el("div", { class: "meta" }, `${s.profile.name} · ${s.revoked_utc ? "revoked " + s.revoked_utc : "active"}`)),
    s.revoked_utc ? null : el("div", { class: "row" },
      el("a", { href: `/share/${s.share_id}`, target: "_blank", rel: "noopener" }, "Open"),
      el("button", { class: "ghost", onclick: async () => { if (await act(() => api("DELETE", "/shares/" + s.share_id), "Share revoked")) refresh(); } }, "Revoke")))) : [el("li", { class: "hint" }, "No share pages yet.")]);
  const stale = state.data.exports.filter((x) => x.stale_reason && !x.removed_utc);
  const exportsTable = el("table", null, el("tr", null, el("th", null, "When"), el("th", null, "Format"), el("th", null, "View"), el("th", null, "Where"), el("th", null, "Status")),
    state.data.exports.slice(0, 20).map((x) => el("tr", null, el("td", null, x.created_utc), el("td", null, x.format), el("td", null, x.profile_name),
      el("td", null, x.path || (x.destination === "download" ? "browser download" : x.destination)),
      el("td", { class: x.stale_reason && !x.removed_utc ? "stale" : "" }, x.removed_utc ? "file removed" : x.stale_reason || "current"))));
  return [el("div", { class: "split" }, el("div", null, editor.node, shareForm),
    el("div", null, el("h3", null, "Preview: exactly what leaves"), preview,
      el("h3", null, "Export this view"), formats,
      el("h3", null, "Share pages"), shares,
      el("h3", null, "Export history"),
      stale.length ? el("p", { class: "stale" }, `${stale.length} earlier export(s) still contain things you have since redacted or deleted. Files the studio wrote can be removed; browser downloads you need to delete yourself.`) : null,
      stale.length ? el("button", { class: "ghost", onclick: async () => {
        const out = await act(() => api("POST", "/exports/remove-stale"));
        if (out) { toast(`Removed ${out.removed.length} file(s); ${out.outside_studio.length} download(s) need deleting by hand.`); refresh(); }
      } }, "Remove stale export files") : null,
      state.data.exports.length ? exportsTable : el("p", { class: "hint" }, "Nothing exported yet.")))];
}

// ------------------------------------------------------------ agents
function renderAgents() {
  const d = state.data;
  const agent = el("input", { maxlength: 80, placeholder: "Claude Desktop" });
  const profile = el("select", null, Object.keys(d.profiles).map((k) => el("option", { value: k, selected: k === "agent" }, k)));
  const suggest = el("input", { type: "checkbox" });
  const hours = el("select", null, [["1", "1 hour"], ["24", "1 day"], ["168", "1 week"], ["720", "30 days"]].map(([v, t]) => el("option", { value: v, selected: v === "24" }, t)));
  const tokenBox = el("div");
  if (state.lastGrant && state.lastGrant.tripId === state.tripId) {
    const code = el("code", null, state.lastGrant.token);
    tokenBox.append(el("div", { class: "token-box" },
      el("p", null, el("strong", null, "Shown once. "), "Put this in the MCP server's environment as TIMELINE_GRANT. The studio keeps only a hash of it."),
      code, el("div", { class: "row" },
        el("button", { class: "ghost", onclick: async () => { try { await navigator.clipboard.writeText(state.lastGrant.token); toast("Copied"); } catch (_) { toast("Copy blocked by the browser; select the text instead", true); } } }, "Copy"),
        el("button", { class: "ghost", onclick: () => { state.lastGrant = null; render(); } }, "I've stored it"))));
  }
  const form = el("form", { class: "card", onsubmit: async (ev) => {
    ev.preventDefault();
    const out = await act(() => api("POST", `/trips/${state.tripId}/grants`, {
      agent: agent.value, profile: profile.value, hours: Number(hours.value),
      capabilities: suggest.checked ? ["read", "suggest"] : ["read"] }), "Grant created");
    if (out) { state.lastGrant = { tripId: state.tripId, token: out.token }; refresh(); }
  } }, el("h3", null, "Give an agent access to this trip"),
    el("p", { class: "hint" }, "An agent sees only this trip, only through the chosen view, only until the grant expires. Nothing else in your history is reachable through a grant."),
    el("label", null, "Agent", agent), el("label", null, "View", profile), el("label", null, "Lasts", hours),
    el("label", { class: "row" }, suggest, "Allow it to propose captions and story text for your approval"),
    el("button", { type: "submit" }, "Create grant"), tokenBox);
  const grants = el("ul", { class: "list" }, d.grants.length ? d.grants.map((g) => el("li", null,
    el("div", null, el("strong", null, g.agent), el("div", { class: "meta" },
      `${g.capabilities.join(" + ")} · ${g.profile.name} view · ${g.active ? "active until " + g.expires_utc : g.revoked_utc ? "revoked" : "expired"}`)),
    g.active ? el("button", { class: "ghost", onclick: async () => { if (await act(() => api("DELETE", "/grants/" + g.grant_id), "Grant revoked")) refresh(); } }, "Revoke") : null)) : [el("li", { class: "hint" }, "No grants. No agent can read this trip.")]);
  const proposals = el("ul", { class: "list" }, d.proposals.length ? d.proposals.map((p) => el("li", null,
    el("div", null, el("strong", null, p.field === "story" ? `Story for ${p.local_date}` : "Caption"), el("div", null, p.value)),
    el("div", { class: "row" },
      el("button", { onclick: async () => { if (await act(() => api("POST", `/proposals/${p.proposal_id}/accept`), "Accepted")) refresh(); } }, "Accept"),
      el("button", { class: "ghost", onclick: async () => { if (await act(() => api("POST", `/proposals/${p.proposal_id}/reject`), "Rejected")) refresh(); } }, "Reject")))) : [el("li", { class: "hint" }, "No pending proposals.")]);
  const log = d.access_log.length ? el("table", null, el("tr", null, el("th", null, "When"), el("th", null, "Grant"), el("th", null, "Tool"), el("th", null, "Outcome")),
    d.access_log.map((r) => el("tr", null, el("td", null, r.at_utc), el("td", null, r.grant_id || "unknown"), el("td", null, r.tool), el("td", null, r.outcome))))
    : el("p", { class: "hint" }, "No agent has used a grant for this trip.");
  return [el("div", { class: "split" }, form, el("div", null,
    el("h3", null, "Grants"), grants, el("h3", null, "Proposals waiting for you"), proposals,
    el("h3", null, "Access log"), log))];
}

// ------------------------------------------------------------ delete
function renderDanger() {
  const t = state.data.trip;
  const confirmTrip = el("input", { placeholder: t.trip_id, "aria-label": "Type the trip id to confirm" });
  const start = el("input", { type: "date", value: t.start_date });
  const end = el("input", { type: "date", value: t.end_date });
  const phrase = el("input", { placeholder: "delete history", "aria-label": "Type delete history to confirm" });
  return [el("div", { class: "cards" },
    el("form", { class: "card", onsubmit: async (ev) => {
      ev.preventDefault();
      if (await act(() => api("DELETE", `/trips/${t.trip_id}?confirm=${encodeURIComponent(confirmTrip.value)}`), "Trip deleted")) {
        state.tripId = null; state.data = null;
        document.getElementById("trip").hidden = true; document.getElementById("empty").hidden = false;
        loadTrips();
      }
    } }, el("h3", null, "Delete this trip"),
      el("p", { class: "hint" }, "Removes the trip, its edits, story, share pages and agent grants. Share pages stop working at once. Your history stays."),
      el("label", null, `Type ${t.trip_id} to confirm`, confirmTrip), el("button", { class: "danger", type: "submit" }, "Delete trip")),
    el("form", { class: "card", onsubmit: async (ev) => {
      ev.preventDefault();
      const out = await act(() => api("POST", "/purge", { start_date: start.value, end_date: end.value, confirm: phrase.value }));
      if (out) { toast(`Purged ${out.visits} visits, ${out.activities} journeys, ${out.raw_fixes} raw fixes, ${out.trip_items} trip items.`); refresh(); }
    } }, el("h3", null, "Purge history for a date range"),
      el("p", { class: "hint" }, "Permanently deletes the underlying location history for these days, from every trip, every share page, every export the studio can reach, and the place-name cache. This cannot be undone."),
      el("label", null, "First day", start), el("label", null, "Last day", end),
      el("label", null, 'Type "delete history" to confirm', phrase), el("button", { class: "danger", type: "submit" }, "Purge history")))];
}

// ------------------------------------------------------------ wiring
document.addEventListener("DOMContentLoaded", () => {
  for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => setTab(b.dataset.tab));
  document.getElementById("inspector-close").addEventListener("click", () => document.getElementById("inspector").close());
  document.getElementById("create-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const out = await act(() => api("POST", "/trips", Object.fromEntries(f)), "Trip captured");
    if (out) { ev.target.reset(); document.getElementById("new-trip").open = false; openTrip(out.trip_id); }
  });
  document.getElementById("rename-trip").addEventListener("click", async () => {
    const v = prompt("Trip title", state.data.trip.title);
    if (v && await act(() => api("PATCH", "/trips/" + state.tripId, { title: v }), "Renamed")) refresh();
  });
  document.getElementById("recapture").addEventListener("click", async () => {
    const out = await act(() => api("POST", `/trips/${state.tripId}/recapture`));
    if (out) { toast(out.added ? `Added ${out.added} new item(s); your edits are kept.` : "Nothing new in your history for these days."); refresh(); }
  });
  loadTrips().then(() => { if (state.trips.length) openTrip(state.trips[0].trip_id); })
    .catch((e) => toast(e.message, true));
});
