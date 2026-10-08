"""Stage 2: turn the raw fetches into the one feature file the map reads.

Every feature, whether a place, a tree or a berry patch, gets a prominence
score and the lowest zoom at which it may appear. The map draws them all in a single layer,
so one ordering decides which label wins wherever two would overlap.

    uv run pipeline/build_features.py

Reads   data/raw/osm_places.json, data/raw/street_trees.json,
        data/raw/inat_berries.json, the place records in data/raw/enrich/record/
        where enrich.py has written them, the fit scores in private/fit/ where
        rank.py has written them, and private/corrections.toml if there is one
Writes  site/data/features.geojson, site/data/meta.json
"""

import collections
import datetime
import json
import math
import re
import tomllib
from pathlib import Path

from common import RAW_DIR, ROOT, SITE_DATA_DIR, mercator, write_json_safely

HERE = Path(__file__).parent
FIRST_ZOOM = 10
TILE_PX = 512  # MapLibre's world is 512 px wide at zoom 0
PLACE_KEYS = ("amenity", "shop", "craft", "tourism", "leisure")
RECORD_DIR = RAW_DIR / "enrich" / "record"
CLOSED_FLOOR = 17
FIT_DIR = ROOT / "private" / "fit"
CORRECTIONS = ROOT / "private" / "corrections.toml"
PATCH_RADIUS_M = 75  # sightings of one species this close to a centre are one patch
PATCH_MAX_ACCURACY_M = 100  # a sighting placed more loosely than this cannot mark a patch
PATCH_FULL_STRENGTH = 12  # observers; more than this adds nothing to a patch's score


def tree_floor(count):
    """Rarer species may appear at lower zooms."""
    if count <= 100:
        return 14
    if count <= 600:
        return 15
    if count <= 1500:
        return 16
    return 17


def patch_floor(observers):
    """Patches more people have recorded may appear at lower zooms."""
    if observers >= 8:
        return 14
    if observers >= 4:
        return 15
    if observers >= 2:
        return 16
    return 17


DAY = "(?:Mo|Tu|We|Th|Fr|Sa|Su|PH)"
SPAN = r"\d\d?:\d\d\s*-\s*\d\d?:\d\d\+?"
HOURS_RULE = re.compile(
    rf"^(?P<days>{DAY}(?:\s*[-,]\s*{DAY})*)?\s*"
    rf"(?P<times>off|closed|{SPAN}(?:\s*,\s*{SPAN})*|\d\d?:\d\d\+)$"
)
DAY_NAMES = {
    "Mo": "Mon",
    "Tu": "Tue",
    "We": "Wed",
    "Th": "Thu",
    "Fr": "Fri",
    "Sa": "Sat",
    "Su": "Sun",
    "PH": "holidays",
}


def clock(hhmm):
    """'14:30' as '2:30pm'. OpenStreetMap writes 2 am the next day as 26:00."""
    hours, minutes = (int(part) for part in hhmm.split(":"))
    hours %= 24
    if minutes == 0 and hours in (0, 12):
        return "midnight" if hours == 0 else "noon"
    suffix = "am" if hours < 12 else "pm"
    return (
        f"{hours % 12 or 12}{suffix}"
        if minutes == 0
        else f"{hours % 12 or 12}:{minutes:02d}{suffix}"
    )


def readable_hours(raw):
    """OpenStreetMap's opening_hours as a line a person can read.

    Covers plain weekly schedules, which is nearly all of them. Anything else
    (seasons, sunrise, "by appointment") is returned as written.
    """
    if raw.strip() == "24/7":
        return "Daily, 24 hours"
    lines = []
    for piece in raw.split(";"):
        # "Mo-Fr 09:00-18:00, Sa 09:00-13:00": a comma after a time starts a new rule.
        for rule in re.split(rf"(?<=[\d+]),\s*(?={DAY}\b)", piece.strip()):
            if not rule:
                continue
            match = HOURS_RULE.match(rule.strip())
            if not match:
                return raw.strip().strip('"')
            days = re.sub(r"\s+", "", match["days"] or "Mo-Su")
            if days == "Mo-Su":
                days = "Daily"
            else:
                days = re.sub(DAY, lambda m: DAY_NAMES[m[0]], days)
                days = days.replace("-", "–").replace(",", ", ")
            times = re.sub(r"\s+", "", match["times"])
            if times in ("off", "closed"):
                times = "closed"
            elif times in ("00:00-24:00", "00:00-00:00"):
                times = "24 hours"
            else:
                spans = []
                for span in times.split(","):
                    if span.endswith("+") and "-" not in span:
                        spans.append(f"from {clock(span[:-1])}")
                    else:
                        start, end = span.rstrip("+").split("-")
                        spans.append(f"{clock(start)}–{clock(end)}")
                times = ", ".join(spans)
            lines.append(f"{days} {times}")
    return "; ".join(lines) if lines else raw


def load_toml(name):
    with open(HERE / name, "rb") as f:
        return tomllib.load(f)


def tag_values(tags, key):
    return {v.strip().lower() for v in tags.get(key, "").split(";") if v.strip()}


def classify(tags, categories):
    for category in categories:
        for rule in category["match"]:
            if all(
                tag_values(tags, key) & set([want] if isinstance(want, str) else want)
                for key, want in rule.items()
            ):
                return category
    raise AssertionError("the last category must match everything")


def build_places(raw, config):
    scoring, categories = config["scoring"], config["category"]
    places, no_position = [], 0
    for el in raw["elements"]:
        tags = el.get("tags", {})
        position = el if "lat" in el else el.get("center")
        if not position:
            no_position += 1
            continue
        category = classify(tags, categories)
        notable = "wikidata" in tags or "wikipedia" in tags
        chain = "brand" in tags or "brand:wikidata" in tags
        bonus = (scoring["notable_bonus"] if notable else 0) + scoring["detail_bonus"] * min(
            len(tags), 25
        ) / 25
        score = category["weight"] + bonus
        floor = category["floor"]
        if chain:
            score *= scoring["chain_factor"]
            floor = max(floor, scoring["chain_floor"])
        if category["id"] == "other":
            kind_tag = next(tags[key] for key in PLACE_KEYS if key in tags)
            sub = kind_tag.replace("_", " ").capitalize()
        else:
            sub = category["label"]
        places.append(
            {
                "id": f"{el['type'][0]}{el['id']}",
                "kind": "place",
                "name": tags["name"],
                "cat": category["id"],
                "fam": category["family"],
                "sub": sub,
                "url": tags.get("website") or tags.get("contact:website") or "",
                "hours": readable_hours(tags["opening_hours"]) if "opening_hours" in tags else "",
                # The month a mapper last confirmed the hours, where one was recorded.
                "hours_at": (tags.get("check_date:opening_hours") or tags.get("check_date", ""))[:7]
                if "opening_hours" in tags
                else "",
                "months": "",
                "lon": position["lon"],
                "lat": position["lat"],
                "score": score,
                "floor": floor,
                "chain": chain,
                "bonus": bonus,
            }
        )
    print(
        f"places: {len(raw['elements'])} in → {len(places)} out "
        f"({no_position} dropped: no coordinates)"
    )
    return places


def drop_duplicates(places, radius_m=60):
    """OSM often maps one business twice, as a point and as its building."""
    cell_lat = radius_m / 111_320
    cell_lon = cell_lat / math.cos(math.radians(45.5))
    seen, kept = set(), []
    for place in sorted(places, key=lambda p: (-p["score"], p["id"])):
        key = (place["name"].casefold(), place["cat"])
        cy, cx = round(place["lat"] / cell_lat), round(place["lon"] / cell_lon)
        if any((key, cy + dy, cx + dx) in seen for dy in (-1, 0, 1) for dx in (-1, 0, 1)):
            continue
        seen.add((key, cy, cx))
        kept.append(place)
    print(
        f"dedupe: {len(places)} in → {len(kept)} out ({len(places) - len(kept)} dropped: "
        f"same name and category within ~{radius_m} m of a higher-scored copy)"
    )
    return kept


def fit_floor(fit):
    """Better fits may appear at lower zooms."""
    if fit >= 80:
        return 11
    if fit >= 65:
        return 12
    if fit >= 50:
        return 13
    if fit >= 35:
        return 14
    return 15


def set_fit(place, fit):
    # A fifth of the tagging bonus is kept, only to order places with equal fits.
    place["score"] = fit / 100 + place["bonus"] / 5
    place["floor"] = fit_floor(fit)
    place["fit"] = fit


def attach_fit(places):
    """Where rank.py has scored a place, that score replaces the category weight.

    The scores live outside the repository. Without them every place keeps its
    category weight, and the map still builds.
    """
    scored = 0
    for place in places:
        path = FIT_DIR / f"{place['id']}.json"
        if not path.exists():
            continue
        set_fit(place, json.loads(path.read_text())["fit"])
        scored += 1
    print(f"fit: {scored} of {len(places)} places ranked by fit score, the rest by category")
    return scored


def attach_records(places):
    """Add the record written by enrich.py to each place that has one."""
    with_record = own_site = other_sites = closed = 0
    for place in places:
        path = RECORD_DIR / f"{place['id']}.json"
        if not path.exists():
            continue
        record = json.loads(path.read_text())["record"]
        with_record += 1
        about = {}
        if record["status"] == "closed":
            # A source says it has shut. Keep it findable at the deepest zoom, with a note,
            # since the report may be stale; it should not be surfaced as somewhere to go.
            about["closed"] = 1
            place["score"] *= 0.3
            place["floor"] = max(place["floor"], CLOSED_FLOOR)
            closed += 1
        # A record is published when the place's own website supports it, or when the
        # model found other sites reliable enough to write a summary from. Where it
        # doubted that they describe this place, it left the summary empty, and the
        # rest of such a record may be about a namesake.
        if record["matches_place"] or (record["summary"] and record["press_note"]):
            own_site += record["matches_place"]
            other_sites += not record["matches_place"]
            about.update(
                what=record["kind"],
                about=record["summary"],
                known=", ".join(record["known_for"]),
                # Wrapped in commas so the map can test for ",darts," as a substring.
                feat="," + ",".join(record["features"]) + "," if record["features"] else "",
            )
        place["about"] = about
    print(
        f"records: {with_record} of {len(places)} places have one; {own_site} described "
        f"from their own website, {other_sites} from other sites, "
        f"{with_record - own_site - other_sites} not published, {closed} reported closed"
    )


def apply_corrections(places, config):
    """Apply hand corrections from private/corrections.toml, if the file exists.

    Each [[place]] names a place exactly as the map does, with `id` as well when
    two places share the name, and may set:

      fit       0 to 100, in place of the model's score
      category  a category id from categories.toml, in place of OpenStreetMap's
      what      the kind of place shown at the top of its popup
    """
    if not CORRECTIONS.exists():
        return
    with open(CORRECTIONS, "rb") as f:
        corrections = tomllib.load(f).get("place", [])
    categories = {category["id"]: category for category in config["category"]}
    for fix in corrections:
        unknown = set(fix) - {"name", "id", "fit", "category", "what"}
        assert not unknown, f"corrections: {fix.get('name')} has unknown keys {unknown}"
        found = [
            place
            for place in places
            if place["name"] == fix["name"] and fix.get("id", place["id"]) == place["id"]
        ]
        if len(found) != 1:
            listing = ", ".join(f"{p['id']} ({p['sub']})" for p in found) or "none"
            raise SystemExit(
                f'corrections: "{fix["name"]}" should match one place but matches '
                f"{len(found)}: {listing}. Add or fix its id."
            )
        place = found[0]
        if "category" in fix:
            category = categories[fix["category"]]
            place.update(cat=category["id"], fam=category["family"], sub=category["label"])
            if "fit" not in place:
                place["score"] = category["weight"] + place["bonus"]
                place["floor"] = category["floor"]
        if "fit" in fix:
            set_fit(place, fix["fit"])
        if "what" in fix:
            place.setdefault("about", {})["what"] = fix["what"]
    print(f"corrections: {len(corrections)} applied from {CORRECTIONS.name}")


def build_trees(raw, config):
    by_inventory_name = {
        name: species for species in config["species"] for name in species["inventory_names"]
    }
    counts = collections.Counter(
        by_inventory_name[f["properties"]["species_common"]]["label"] for f in raw["features"]
    )
    commonest = max(counts.values())
    trees, no_position = [], 0
    for f in raw["features"]:
        props = f["properties"]
        if not f.get("geometry"):
            no_position += 1
            continue
        species = by_inventory_name[props["species_common"]]
        count = counts[species["label"]]
        rarity = 1 - math.log10(count) / math.log10(commonest)
        size = min(props.get("diameter") or 0, 30) / 30
        lon, lat = f["geometry"]["coordinates"]
        trees.append(
            {
                "id": f"t{props['OBJECTID']}",
                "kind": "tree",
                "name": species["label"],
                "cat": "tree",
                "fam": "growing",
                "sub": "Street tree",
                "url": "",
                "months": "," + ",".join(str(m) for m in species["months"]) + ",",
                "lon": lon,
                "lat": lat,
                "score": 0.35 + 0.55 * rarity + 0.10 * size,
                "floor": tree_floor(count),
            }
        )
    print(
        f"trees: {len(raw['features'])} in → {len(trees)} out "
        f"({no_position} dropped: no coordinates)"
    )
    return trees, counts


def group_patches(sightings, radius_m):
    """Group one species' sightings into patches that reach radius_m from a centre.

    The sighting with the most observers within radius_m becomes a centre and
    takes those neighbours with it; then the same again among what is left. Every
    patch is therefore centred on a place where the plant was actually recorded.
    """
    m_per_lat = 111_320
    m_per_lon = m_per_lat * math.cos(math.radians(45.5))
    points = [(s["lon"] * m_per_lon, s["lat"] * m_per_lat) for s in sightings]
    grid = collections.defaultdict(list)
    for i, (x, y) in enumerate(points):
        grid[(int(x // radius_m), int(y // radius_m))].append(i)

    def neighbours(i):
        x, y = points[i]
        cx, cy = int(x // radius_m), int(y // radius_m)
        return [
            j
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for j in grid[(cx + dx, cy + dy)]
            if math.hypot(points[j][0] - x, points[j][1] - y) <= radius_m
        ]

    near = [neighbours(i) for i in range(len(points))]
    free = set(range(len(points)))
    patches = []
    while free:
        centre = max(
            free,
            key=lambda i: (
                len({sightings[j]["user"] for j in near[i] if j in free}),
                sum(j in free for j in near[i]),
                -sightings[i]["id"],
            ),
        )
        members = [j for j in near[centre] if j in free]
        free.difference_update(members)
        patches.append((sightings[centre], [sightings[j] for j in members]))
    return patches


def build_patches(raw, config):
    by_taxon = {species["taxon_id"]: species for species in config["wild"]}
    sightings = raw["observations"]
    located = [s for s in sightings if s["accuracy_m"] is not None]
    precise = [s for s in located if s["accuracy_m"] <= PATCH_MAX_ACCURACY_M]
    patches, counts = [], collections.Counter()
    for taxon_id, species in by_taxon.items():
        own = [s for s in precise if s["taxon_id"] == taxon_id]
        for centre, members in group_patches(own, PATCH_RADIUS_M):
            observers = len({s["user"] for s in members})
            strength = min(1, math.log2(1 + observers) / math.log2(1 + PATCH_FULL_STRENGTH))
            years = [s["year"] for s in members]
            counts[species["label"]] += 1
            patches.append(
                {
                    "id": f"b{centre['id']}",
                    "kind": "patch",
                    "name": species["label"],
                    "cat": "patch",
                    "fam": "growing",
                    "url": "",
                    "months": "," + ",".join(str(m) for m in species["months"]) + ",",
                    "lon": centre["lon"],
                    "lat": centre["lat"],
                    "score": 0.20 + 0.25 * species["weight"] + 0.55 * strength,
                    "floor": patch_floor(observers),
                    "about": {
                        "tx": taxon_id,
                        "n": len(members),
                        "who": observers,
                        "y0": min(years),
                        "y1": max(years),
                        "fruit": sum(s["fruiting"] for s in members),
                    },
                }
            )
    assert sum(p["about"]["n"] for p in patches) == len(precise)
    print(
        f"berries: {len(sightings)} sightings in → {len(precise)} used "
        f"({len(sightings) - len(located)} dropped: no accuracy recorded; "
        f"{len(located) - len(precise)} dropped: placed more loosely than "
        f"{PATCH_MAX_ACCURACY_M} m) → {len(patches)} patches, "
        f"{sum(p['about']['who'] > 1 for p in patches)} of them recorded by more than one person"
    )
    return patches, counts


def assign_minzoom(items, *, cell_px, last_zoom, per_cell=1, admit_by=()):
    """Give each item the lowest zoom at which it is among the top scorers in its cell.

    At each zoom the map is cut into cells of cell_px screen pixels and the best
    per_cell eligible items in each cell are admitted. This spreads labels evenly
    instead of letting the densest neighbourhood take every early slot.
    """
    ranked = sorted(items, key=lambda it: (-it["score"], it["id"]))
    positions = {it["id"]: mercator(it["lon"], it["lat"]) for it in items}
    for it in items:
        it["mz"] = last_zoom + 1
    for zoom in range(FIRST_ZOOM, last_zoom + 1):
        cells_per_world = TILE_PX * 2**zoom / cell_px
        taken = collections.Counter()
        for it in ranked:
            if it["floor"] > zoom:
                continue
            x, y = positions[it["id"]]
            cell = (int(x * cells_per_world), int(y * cells_per_world))
            if taken[cell] >= per_cell:
                continue
            taken[cell] += 1
            it["mz"] = min(it["mz"], zoom)
    # A high score guarantees a zoom, whatever the competition in the cell. The map's
    # own label collision still decides which of the admitted places gets its name.
    for it in items:
        for threshold, zoom in admit_by:
            if it["score"] >= threshold:
                it["mz"] = min(it["mz"], max(zoom, it["floor"]))
                break


def assign_minzoom_in_season(items, **settings):
    """As assign_minzoom, but an item competes only with what is ripe alongside it.

    Each month is settled on its own, among what is ripe in it, and an item keeps
    the lowest zoom it earns in any of its months. Ranked all together, a salal
    patch (ripe in August) would wait behind the thimbleberries beside it, which
    are over by then.
    """
    earned = {}
    for month in range(1, 13):
        ripe = [it for it in items if f",{month}," in it["months"]]
        assign_minzoom(ripe, **settings)
        for it in ripe:
            earned[it["id"]] = min(earned.get(it["id"], it["mz"]), it["mz"])
    assert len(earned) == len(items), "every item needs at least one month"
    for it in items:
        it["mz"] = earned[it["id"]]


def to_feature(item, mz_off):
    # Properties the map can do without are left out: the file is read on phones.
    properties = {
        "kind": item["kind"],
        "name": item["name"],
        "fam": item["fam"],
        "sort": round(-item["score"], 3),  # MapLibre places lower sort keys first
        "mz": item["mz"],
    }
    if item["kind"] == "place":
        properties["id"] = item["id"]
        properties["sub"] = item["sub"]
        if item["top"]:
            properties["top"] = 1
        if item["hours"]:
            properties["hours"] = item["hours"]
        if item["hours_at"]:
            properties["hours_at"] = item["hours_at"]
    properties.update({key: value for key, value in item.get("about", {}).items() if value})
    if item["url"]:
        properties["url"] = item["url"]
    if item["months"]:
        properties["months"] = item["months"]
    if mz_off != item["mz"]:
        properties["mz_off"] = mz_off
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [round(item["lon"], 6), round(item["lat"], 6)],
        },
        "properties": properties,
    }


def main():
    categories = load_toml("categories.toml")
    edibles = load_toml("edibles.toml")
    scoring = categories["scoring"]
    raw_places = json.loads((RAW_DIR / "osm_places.json").read_text())
    raw_trees = json.loads((RAW_DIR / "street_trees.json").read_text())
    raw_berries = json.loads((RAW_DIR / "inat_berries.json").read_text())

    places = drop_duplicates(build_places(raw_places, categories))
    scored = attach_fit(places)
    attach_records(places)
    apply_corrections(places, categories)
    for place in places:
        closed = place.get("about", {}).get("closed")
        place["top"] = place.get("fit", 0) >= scoring["top_fit"] and not closed
    print(f"top picks: {sum(p['top'] for p in places)} places score {scoring['top_fit']} or more")
    trees, tree_counts = build_trees(raw_trees, edibles)
    patches, patch_counts = build_patches(raw_berries, edibles)

    # Places are ranked apart from what grows, so that each keeps an even spread;
    # the map's label collision then arbitrates between them by score. What grows
    # is ranked month by month, so nothing ripe waits behind something that is not.
    assign_minzoom(
        places,
        cell_px=scoring["cell_px"],
        last_zoom=scoring["last_zoom"],
        per_cell=scoring["per_cell"],
        admit_by=scoring["admit_by"],
    )
    assign_minzoom_in_season(
        trees + patches, cell_px=scoring["plant_cell_px"], last_zoom=scoring["last_zoom"]
    )

    deep_zoom = scoring["last_zoom"]
    features = [to_feature(p, mz_off=p["mz"]) for p in places]
    # Out of season, a tree or a berry patch waits until the deepest zoom.
    features += [to_feature(g, mz_off=max(g["mz"], deep_zoom)) for g in trees + patches]
    assert len(features) == len(places) + len(trees) + len(patches)

    for label, items in (("places", places), ("trees", trees), ("berry patches", patches)):
        by_zoom = collections.Counter(it["mz"] for it in items)
        running, line = 0, []
        for zoom in sorted(by_zoom):
            running += by_zoom[zoom]
            line.append(f"z{zoom}: {running}")
        print(f"{label} visible by zoom (cumulative): " + ", ".join(line))

    write_json_safely(
        {"type": "FeatureCollection", "features": features},
        SITE_DATA_DIR / "features.geojson",
        items_key="features",
    )
    meta = {
        "built_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "counts": {
            "places": len(places),
            "trees": len(trees),
            "patches": len(patches),
            "ranked_by_fit": scored,
        },
        "places_fetched_at": raw_places.get("fetched_at"),
        "trees_fetched_at": raw_trees.get("fetched_at"),
        "berries_fetched_at": raw_berries.get("fetched_at"),
        # `count` is trees for a street tree and patches for a wild plant.
        "species": [
            {"label": s["label"], "months": s["months"], "count": tree_counts[s["label"]]}
            for s in edibles["species"]
        ]
        + [
            {"label": s["label"], "months": s["months"], "count": patch_counts[s["label"]]}
            for s in edibles["wild"]
        ],
    }
    write_json_safely(meta, SITE_DATA_DIR / "meta.json")
    print(f"wrote {len(features)} features to {SITE_DATA_DIR / 'features.geojson'}")


if __name__ == "__main__":
    main()
