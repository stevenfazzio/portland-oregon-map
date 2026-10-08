import {
  GeolocateControl,
  Map as MapLibreMap,
  NavigationControl,
  Popup,
  ScaleControl,
} from "https://unpkg.com/maplibre-gl@6.11.2/dist/maplibre-gl.mjs";

const BASE_STYLE = "https://tiles.openfreemap.org/styles/positron";
const INK = "#22262a";

// Colour marks the family a feature belongs to. A tree or berry patch is a filled
// dot while it is in season and a ring when it is not.
const FAMILIES = [
  { id: "food", label: "Food", color: "#eb6834" },
  { id: "drink", label: "Drink", color: "#eda100" },
  { id: "music_screen", label: "Music and film", color: "#2a78d6" },
  { id: "shops", label: "Shops and markets", color: "#e87ba4" },
  { id: "growing", label: "Fruit, nuts and berries", color: "#1baf7a", seasonal: true },
  { id: "landmark", label: "Parks and landmarks", color: "#5c6166" },
  { id: "other", label: "Everything else", color: "#a3a6a1" },
];

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

let month = new Date().getMonth() + 1;
const hidden = new Set();

// Expressions shared by the filter, the label ordering and the paint.
// A feature with no `months` is always in season; one with no `mz_off` appears
// at the same zoom all year.
const MONTH = ["global-state", "month"];
const IN_SEASON = [
  "any",
  ["!", ["has", "months"]],
  ["in", ["concat", ",", ["to-string", MONTH], ","], ["coalesce", ["get", "months"], ""]],
];
const MIN_ZOOM = ["case", IN_SEASON, ["get", "mz"], ["coalesce", ["get", "mz_off"], ["get", "mz"]]];

function featureFilter() {
  const shown = FAMILIES.map((f) => f.id).filter((id) => !hidden.has(id));
  return ["all", [">=", ["zoom"], MIN_ZOOM], ["in", ["get", "fam"], ["literal", shown]]];
}

// The basemap is a neutral public style. Tint land cover so parks and water
// read at a glance, and draw unpaved paths the way a topographic sheet does.
function restyle(style) {
  const paint = (id, props) => {
    const layer = style.layers.find((l) => l.id === id);
    if (layer) Object.assign((layer.paint ??= {}), props);
  };
  paint("park", { "fill-color": "#dde8d3" });
  paint("landcover_wood", { "fill-color": "#d2e2c7" });
  paint("water", { "fill-color": "#bcd3e1" });
  paint("waterway", { "line-color": "#bcd3e1" });
  paint("water_name_point_label", { "text-color": "#3f6184" });
  paint("water_name_line_label", { "text-color": "#3f6184" });
  paint("waterway_line_label", { "text-color": "#3f6184" });

  const trail = {
    id: "trail",
    type: "line",
    source: "openmaptiles",
    "source-layer": "transportation",
    minzoom: 12,
    filter: [
      "all",
      ["==", ["get", "class"], "path"],
      ["any", ["==", ["get", "subclass"], "path"], ["==", ["get", "surface"], "unpaved"]],
    ],
    paint: {
      "line-color": "#9a5b2e",
      "line-width": ["interpolate", ["linear"], ["zoom"], 12, 0.7, 16, 1.6],
      "line-dasharray": [3, 1.5],
    },
  };
  const after = style.layers.findIndex((l) => l.id === "highway_path");
  style.layers.splice(after + 1, 0, trail);

  style.state = { month: { default: month } };
  return style;
}

function dotImage(color, ring) {
  const size = 28; // drawn at 2x for sharpness; 14 px on screen
  const canvas = Object.assign(document.createElement("canvas"), { width: size, height: size });
  const ctx = canvas.getContext("2d");
  const c = size / 2;
  ctx.beginPath();
  ctx.arc(c, c, 11, 0, Math.PI * 2);
  ctx.fillStyle = "#ffffff";
  ctx.fill();
  ctx.beginPath();
  ctx.arc(c, c, ring ? 7 : 8.5, 0, Math.PI * 2);
  if (ring) {
    ctx.lineWidth = 4;
    ctx.strokeStyle = color;
    ctx.stroke();
  } else {
    ctx.fillStyle = color;
    ctx.fill();
  }
  return ctx.getImageData(0, 0, size, size);
}

function addFeatures(map) {
  for (const family of FAMILIES) {
    map.addImage(`dot-${family.id}`, dotImage(family.color, false), { pixelRatio: 2 });
    if (family.seasonal) {
      map.addImage(`ring-${family.id}`, dotImage(family.color, true), { pixelRatio: 2 });
    }
  }
  map.addSource("features", {
    type: "geojson",
    data: "data/features.geojson",
    attribution:
      'Places © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors. ' +
      'Trees: <a href="https://www.portland.gov/trees">City of Portland</a>. ' +
      'Berries: <a href="https://www.inaturalist.org">iNaturalist</a> observers.',
  });
  // One layer for everything, so a single ordering settles every overlap.
  map.addLayer({
    id: "features",
    type: "symbol",
    source: "features",
    filter: featureFilter(),
    layout: {
      // Only what has months can be out of season, so only it has a ring to show.
      "icon-image": ["concat", ["case", IN_SEASON, "dot-", "ring-"], ["get", "fam"]],
      "text-field": ["get", "name"],
      "text-font": ["Noto Sans Regular"],
      "text-size": ["interpolate", ["linear"], ["zoom"], 11, 11, 16, 12.5],
      "text-variable-anchor": ["left", "right", "top", "bottom"],
      "text-radial-offset": 0.75,
      "text-justify": "auto",
      "text-max-width": 8,
      "text-padding": 3,
      // When a name will not fit, the dot stays: labelled, dot only, or hidden.
      "text-optional": true,
      // Lower keys are placed first. Out of season, a tree or berry patch drops behind everything.
      "symbol-sort-key": ["case", IN_SEASON, ["get", "sort"], ["+", ["get", "sort"], 2]],
    },
    paint: {
      "text-color": INK,
      "text-halo-color": "rgba(255, 255, 255, 0.92)",
      "text-halo-width": 1.4,
    },
  });
}

function monthRange(months) {
  const names = months.map((m) => MONTHS[m - 1]);
  return names.length === 1 ? names[0] : `${names[0]} to ${names.at(-1)}`;
}

// How each feature key in the data reads in a sentence.
const FEATURE_WORDS = {
  trivia_night: "trivia night",
  pool_table: "pool",
  pinball_or_arcade: "pinball or arcade games",
  dog_friendly: "dogs welcome",
  late_night: "late hours",
  house_roasted_coffee: "house-roasted coffee",
  baked_on_site: "baking on site",
  vinyl_records: "vinyl",
  secondhand: "second-hand goods",
  repertory_film: "repertory film",
  vegetarian_friendly: "vegetarian options",
  bulk_or_refill: "bulk and refill",
};

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

function linkRow(links) {
  const row = el("p", { className: "links" });
  for (const [text, href] of links) {
    row.append(el("a", { textContent: text, href, target: "_blank", rel: "noopener" }));
  }
  return row;
}

// "Recorded 12 times by 7 people, 2017 to 2026."
function sightings(p) {
  if (p.n === 1) return `Recorded once, in ${p.y1}.`;
  const people = p.who === 1 ? "one person" : `${p.who} people`;
  const years = p.y0 === p.y1 ? `in ${p.y1}` : `${p.y0} to ${p.y1}`;
  return `Recorded ${p.n} times by ${people}, ${years}.`;
}

function openPopup(map, feature) {
  const p = feature.properties;
  const box = el("div", {}, el("h2", { textContent: p.name }));
  const ripe = () => monthRange(p.months.split(",").filter(Boolean).map(Number));
  if (p.kind === "tree") {
    box.append(el("p", { textContent: `Street tree. Usually ripe ${ripe()}.` }));
  } else if (p.kind === "patch") {
    const [lng, lat] = feature.geometry.coordinates;
    box.append(el("p", { textContent: `Growing wild. Usually ripe ${ripe()}.` }));
    box.append(el("p", { textContent: sightings(p) }));
    if (p.fruit) {
      const times = p.fruit === 1 ? "once" : `${p.fruit} times`;
      box.append(el("p", { textContent: `Seen in fruit ${times}.` }));
    }
    // The sightings behind the patch, each credited to whoever made it.
    const search = new URLSearchParams({ taxon_id: p.tx, lat, lng, radius: 0.1 });
    box.append(linkRow([["Sightings on iNaturalist", `https://www.inaturalist.org/observations?${search}`]]));
  } else {
    const what = p.what ?? p.sub;
    box.append(el("p", { textContent: what.charAt(0).toUpperCase() + what.slice(1) }));
    if (p.closed) box.append(el("p", { textContent: "Reported as permanently closed." }));
    if (p.about) box.append(el("p", { className: "about", textContent: p.about }));
    if (p.known) box.append(el("p", { textContent: `Known for ${p.known}.` }));
    if (p.feat) {
      const words = p.feat
        .split(",")
        .filter(Boolean)
        .map((key) => FEATURE_WORDS[key] ?? key.replaceAll("_", " "));
      box.append(el("p", { textContent: `Has ${words.join(", ")}.` }));
    }
    const osmType = { n: "node", w: "way", r: "relation" }[p.id[0]];
    const links = [["OpenStreetMap", `https://www.openstreetmap.org/${osmType}/${p.id.slice(1)}`]];
    if (/^https?:\/\//.test(p.url ?? "")) links.unshift(["Website", p.url]);
    box.append(linkRow(links));
  }
  // Near the map's edge a popup hangs to one side of its point. On a phone it has
  // to be narrow enough to fit there: a popup half as wide again as itself, plus
  // its offset, must not exceed the map.
  const maxWidth = Math.min(300, Math.floor((map.getContainer().clientWidth - 20) / 1.5));
  new Popup({ offset: 10, maxWidth: `${maxWidth}px` })
    .setLngLat(feature.geometry.coordinates)
    .setDOMContent(box)
    .addTo(map);
}

function buildKey(map) {
  const key = document.getElementById("key");
  for (const family of FAMILIES) {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("aria-pressed", "true");
    const swatch = el("span", { className: "swatch" });
    button.style.setProperty("--swatch", family.color);
    button.append(swatch, family.label);
    if (family.seasonal) {
      // The same button explains the ring: one family, two states.
      button.classList.add("two-line");
      button.append(
        el("span", { className: "swatch ring" }),
        el("span", { className: "note", textContent: "when out of season" }),
      );
    }
    button.addEventListener("click", () => {
      const show = hidden.has(family.id);
      if (show) hidden.delete(family.id);
      else hidden.add(family.id);
      button.setAttribute("aria-pressed", String(show));
      map.setFilter("features", featureFilter());
    });
    key.appendChild(document.createElement("li")).appendChild(button);
  }
}

function buildMonths(map, species) {
  const ripeIn = (m) => species.filter((s) => s.months.includes(m));
  const most = Math.max(...MONTHS.map((_, i) => ripeIn(i + 1).length));
  const holder = document.getElementById("months");
  const ripe = document.getElementById("ripe");
  const heading = document.getElementById("month-name");

  const select = (m) => {
    month = m;
    map.setGlobalStateProperty("month", m);
    for (const button of holder.children) {
      button.setAttribute("aria-pressed", String(Number(button.dataset.month) === m));
    }
    // Rarest first: those are the ones worth knowing about.
    const names = ripeIn(m)
      .sort((a, b) => a.count - b.count)
      .map((s) => s.label);
    heading.textContent = MONTHS[m - 1];
    ripe.textContent = names.length ? names.join(", ") : "Nothing yet.";
  };

  MONTHS.forEach((name, i) => {
    const m = i + 1;
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.month = m;
    button.title = `${name}: ${ripeIn(m).length} kinds of fruit, nut and berry in season`;
    const block = document.createElement("span");
    block.className = "block";
    block.style.setProperty("--share", (ripeIn(m).length / most).toFixed(2));
    const long = Object.assign(document.createElement("span"), { className: "long", textContent: name.slice(0, 3) });
    const short = Object.assign(document.createElement("span"), { className: "short", textContent: name[0] });
    button.append(block, long, short);
    button.setAttribute("aria-label", name);
    button.addEventListener("click", () => select(m));
    holder.appendChild(button);
  });
  select(month);
}

async function main() {
  const [style, meta] = await Promise.all([
    fetch(BASE_STYLE).then((r) => r.json()),
    fetch("data/meta.json").then((r) => r.json()),
  ]);

  const map = new MapLibreMap({
    container: "map",
    style: restyle(style),
    center: [-122.63, 45.52],
    zoom: 12,
    minZoom: 9,
    maxZoom: 19,
    hash: true,
    dragRotate: false,
    pitchWithRotate: false,
    attributionControl: { compact: true },
  });
  // The key and the credits start open only where there is room beside the map.
  const roomy = matchMedia("(min-width: 600px)").matches;
  document.getElementById("key-box").open = roomy;
  map.touchZoomRotate.disableRotation();
  map.addControl(new NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new GeolocateControl({ trackUserLocation: true }), "top-right");
  map.addControl(new ScaleControl({ unit: "imperial" }), "bottom-left");

  const zoom = document.getElementById("zoom");
  const showZoom = () => (zoom.textContent = `z${map.getZoom().toFixed(1)}`);
  map.on("zoom", showZoom);
  showZoom();

  map.on("load", () => {
    addFeatures(map);
    buildKey(map);
    buildMonths(map, meta.species);
    if (!roomy) {
      // MapLibre opens the credits once the sources report in; fold them back.
      const credits = document.querySelector(".maplibregl-ctrl-attrib");
      credits?.classList.remove("maplibregl-compact-show");
      credits?.removeAttribute("open");
    }
  });
  map.on("click", "features", (e) => openPopup(map, e.features[0]));
  map.on("mouseenter", "features", () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", "features", () => (map.getCanvas().style.cursor = ""));

  window.map = map; // handy in the console
}

main().catch((error) => {
  console.error(error);
  const notice = document.getElementById("notice");
  notice.textContent = "The map could not load. Check your connection and reload the page.";
  notice.hidden = false;
});
