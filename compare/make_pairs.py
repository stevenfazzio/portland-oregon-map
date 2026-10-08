"""Draw pairs of places to be judged side by side.

The category weights say which kinds of place matter; they cannot say which
place is better. Judging pairs gives the ordering directly. Two kinds are drawn:

  within  two places from the same category: ground truth for ranking inside it
  cross   one place from each of two categories: evidence for the weights

Writes private/pairs.json, which is not committed.

    uv run compare/make_pairs.py
    uv run compare/make_pairs.py --described-only
    uv run compare/make_pairs.py --seed 2 --out private/pairs-2.json
"""

import argparse
import datetime
import json
import random
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from build_features import build_places, drop_duplicates, load_toml  # noqa: E402
from common import RAW_DIR, write_json_safely  # noqa: E402

JUDGED_FAMILIES = {"food", "drink", "music_screen", "shops"}
RECORD_DIR = RAW_DIR / "enrich" / "record"
WITHIN_MIN_WEIGHT = 0.5
WITHIN_MIN_MEMBERS = 8
CROSS_MIN_MEMBERS = 3
CROSS_APPEARANCES = 3  # times each category shows up in a cross pair


def card(place, tags):
    """What the judge sees for one place."""
    details = []
    if "cuisine" in tags:
        details.append(tags["cuisine"].replace(";", ", ").replace("_", " "))
    street = " ".join(tags[k] for k in ("addr:housenumber", "addr:street") if k in tags)
    if street:
        details.append(street)
    query = " ".join([place["name"], street, tags.get("addr:city", "Portland"), "OR"])
    return {
        "id": place["id"],
        "name": place["name"],
        "cat": place["cat"],
        "label": place["sub"],
        "fam": place["fam"],
        "details": details,
        "url": place["url"],
        "lookup": "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(query),
    }


def cross_schedule(category_ids, rng):
    """Pair categories so each appears CROSS_APPEARANCES times, never with itself or twice."""
    slots = category_ids * CROSS_APPEARANCES
    for _ in range(2000):
        rng.shuffle(slots)
        pairs = [tuple(sorted(slots[i : i + 2])) for i in range(0, len(slots) - 1, 2)]
        if all(a != b for a, b in pairs) and len(set(pairs)) == len(pairs):
            return pairs
    raise RuntimeError("could not schedule cross-category pairs; lower CROSS_APPEARANCES")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--within", type=int, default=24, help="number of same-category pairs")
    parser.add_argument("--out", default=str(ROOT / "private" / "pairs.json"))
    parser.add_argument("--force", action="store_true", help="replace an existing pairs file")
    parser.add_argument(
        "--described-only",
        action="store_true",
        help="draw only from places that enrich.py has written a record for",
    )
    args = parser.parse_args()

    out = Path(args.out)
    if out.exists() and not args.force:
        sys.exit(f"{out} exists. Answers refer to its pairs; use --out for a new round.")

    rng = random.Random(args.seed)
    config = load_toml("categories.toml")
    raw = json.loads((RAW_DIR / "osm_places.json").read_text())
    tags_by_id = {f"{el['type'][0]}{el['id']}": el.get("tags", {}) for el in raw["elements"]}
    places = drop_duplicates(build_places(raw, config))

    pool = {}
    for place in places:
        if args.described_only and not (RECORD_DIR / f"{place['id']}.json").exists():
            continue
        if place["fam"] in JUDGED_FAMILIES and not place["chain"]:
            pool.setdefault(place["cat"], []).append(place)
    for members in pool.values():
        members.sort(key=lambda p: p["id"])  # a stable order, so the seed decides the draw
        rng.shuffle(members)
    weight = {c["id"]: c["weight"] for c in config["category"]}

    def draw(category):
        members = pool[category]
        place = members.pop()
        members.insert(0, place)  # reused only once every other member has been shown
        return card(place, tags_by_id[place["id"]])

    within_cats = sorted(
        (
            c
            for c, m in pool.items()
            if weight[c] >= WITHIN_MIN_WEIGHT and len(m) >= WITHIN_MIN_MEMBERS
        ),
        key=lambda c: (-weight[c], -len(pool[c]), c),
    )
    cross_cats = sorted(c for c, m in pool.items() if len(m) >= CROSS_MIN_MEMBERS)

    pairs = []
    for i in range(args.within):
        category = within_cats[i % len(within_cats)]
        pairs.append({"kind": "within", "a": draw(category), "b": draw(category)})
    for cat_a, cat_b in cross_schedule(cross_cats, rng):
        sides = [draw(cat_a), draw(cat_b)]
        rng.shuffle(sides)  # so neither category always sits on the left
        pairs.append({"kind": "cross", "a": sides[0], "b": sides[1]})
    rng.shuffle(pairs)
    for n, pair in enumerate(pairs, start=1):
        pair["id"] = f"s{args.seed}-{n:03d}"

    n_within = sum(p["kind"] == "within" for p in pairs)
    print(
        f"pairs: {len(pairs)} drawn ({n_within} within {len(within_cats)} categories, "
        f"{len(pairs) - n_within} across {len(cross_cats)} categories)"
    )
    payload = {
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "seed": args.seed,
        "pairs": pairs,
    }
    write_json_safely(payload, out, items_key="pairs")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
