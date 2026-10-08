"""Stage 1c: fetch sightings of wild berries and nuts from iNaturalist.

Writes data/raw/inat_berries.json. Only the species under [[wild]] in
edibles.toml are requested, and only observations that are

  - research grade: at least two people agree on the species
  - at their true position: iNaturalist blurs some, by the observer's choice
    or to protect a species, and a blurred point cannot mark a patch
  - licensed by the observer for reuse (CC0, CC BY or CC BY-NC)

It also fetches, for each species, how many observations within 40 km were
recorded as in fruit in each month, and prints that beside the ripening window
in edibles.toml so the two can be compared.

    uv run pipeline/fetch_berries.py
    uv run pipeline/fetch_berries.py --refresh
"""

import argparse
import datetime
import time
import tomllib
from pathlib import Path

from common import BBOX, RAW_DIR, fetch_json, write_json_safely

API = "https://api.inaturalist.org"
LICENSES = "cc0,cc-by,cc-by-nc"
PAGE_SIZE = 200  # the most the API returns at once
PAUSE = 1.0  # seconds; iNaturalist asks for about one request a second
# The annotation "Flowers and Fruits: Fruits or Seeds". It does not say whether
# the fruit was ripe.
FRUITING = {"term_id": 12, "term_value_id": 14}
PHENOLOGY_AREA = {"lat": 45.52, "lng": -122.68, "radius": 40}  # km; the lowlands around the city
# Version 2 of the API returns only the fields asked for, which keeps a page small.
FIELDS = (
    "(id:!t,location:!t,observed_on_details:(month:!t,year:!t),positional_accuracy:!t,"
    "user:(id:!t),taxon:(ancestor_ids:!t),"
    "annotations:(controlled_attribute_id:!t,controlled_value_id:!t))"
)


def wanted_species():
    with open(Path(__file__).parent / "edibles.toml", "rb") as f:
        return tomllib.load(f)["wild"]


def check_taxa(species):
    """Stop if an id in edibles.toml no longer points at the species named beside it."""
    ids = ",".join(str(s["taxon_id"]) for s in species)
    found = {t["id"]: t for t in fetch_json(f"{API}/v1/taxa/{ids}")["results"]}
    for s in species:
        taxon = found.get(s["taxon_id"])
        assert taxon and taxon["is_active"], f"{s['label']}: taxon {s['taxon_id']} is not active"
        assert taxon["name"] == s["latin"], (
            f"{s['label']}: taxon {s['taxon_id']} is {taxon['name']}, not {s['latin']}"
        )


def tidy(obs, wanted_ids):
    """Keep only what the build needs from one observation."""
    # An observation of a subspecies carries the subspecies' id; its ancestors include ours.
    taxon_id = next(i for i in reversed(obs["taxon"]["ancestor_ids"]) if i in wanted_ids)
    lat, lon = (float(part) for part in obs["location"].split(","))
    return {
        "id": obs["id"],
        "taxon_id": taxon_id,
        "lat": lat,
        "lon": lon,
        "year": obs["observed_on_details"]["year"],
        "month": obs["observed_on_details"]["month"],
        "accuracy_m": obs["positional_accuracy"],
        "user": obs["user"]["id"],
        "fruiting": any(
            a["controlled_attribute_id"] == FRUITING["term_id"]
            and a["controlled_value_id"] == FRUITING["term_value_id"]
            for a in obs.get("annotations", [])
        ),
    }


def fetch_observations(species, limit=None):
    wanted_ids = {s["taxon_id"] for s in species}
    south, west, north, east = BBOX
    query = {
        "taxon_id": ",".join(str(i) for i in sorted(wanted_ids)),
        "swlat": south,
        "swlng": west,
        "nelat": north,
        "nelng": east,
        "quality_grade": "research",
        "geoprivacy": "open",
        "taxon_geoprivacy": "open",
        "license": LICENSES,
        "per_page": PAGE_SIZE,
        "order_by": "id",  # with id_above, pages never skip or repeat
        "order": "asc",
        "fields": FIELDS,
    }
    observations, expected, last_id = [], None, 0
    while True:
        page = fetch_json(f"{API}/v2/observations", params={**query, "id_above": last_id})
        if expected is None:
            expected = page["total_results"]
            print(f"fetch_berries: iNaturalist reports {expected} usable observations")
        if not page["results"]:
            break
        # A few observations are undated or lack coordinates despite the filters.
        observations.extend(
            tidy(obs, wanted_ids)
            for obs in page["results"]
            if obs.get("location") and obs.get("observed_on_details")
        )
        last_id = page["results"][-1]["id"]
        print(f"  {len(observations)}/{expected}")
        if limit and len(observations) >= limit:
            return observations[:limit], expected
        time.sleep(PAUSE)
    return observations, expected


def fetch_fruiting_months(species):
    """Per species: observations recorded as in fruit near the city, by month of the year."""
    months = {}
    for s in species:
        result = fetch_json(
            f"{API}/v1/observations/histogram",
            params={
                "taxon_id": s["taxon_id"],
                "quality_grade": "research",
                "date_field": "observed",
                "interval": "month_of_year",
                **FRUITING,
                **PHENOLOGY_AREA,
            },
        )["results"]["month_of_year"]
        months[str(s["taxon_id"])] = [result[str(m)] for m in range(1, 13)]
        time.sleep(PAUSE)
    return months


def print_windows(species, fruiting_months):
    print("\nIn fruit by month (unripe included); [ ] marks the window in edibles.toml")
    print(" " * 24 + "".join(f"{m:>6}" for m in "JFMAMJJASOND"))
    for s in species:
        cells = [
            f"[{n}]" if m in s["months"] else str(n)
            for m, n in enumerate(fruiting_months[str(s["taxon_id"])], start=1)
        ]
        print(f"{s['label']:<24}" + "".join(f"{cell:>6}" for cell in cells))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--refresh", action="store_true", help="refetch even if a raw file exists")
    parser.add_argument("--limit", type=int, help="stop after this many observations (for testing)")
    parser.add_argument("--out", default=str(RAW_DIR / "inat_berries.json"))
    args = parser.parse_args()

    out = Path(args.out)
    if out.exists() and not args.refresh:
        print(f"{out} exists; pass --refresh to refetch")
        return

    species = wanted_species()
    check_taxa(species)
    time.sleep(PAUSE)
    observations, expected = fetch_observations(species, args.limit)

    unique = len({obs["id"] for obs in observations})
    assert unique == len(observations), f"paging repeated {len(observations) - unique} observations"
    if not args.limit:
        # The count can move by a few while paging, as observations are added or regraded.
        missing = expected - len(observations)
        assert abs(missing) <= max(10, expected // 100), (
            f"expected {expected} observations, kept {len(observations)}"
        )
        print(f"fetch_berries: {expected} reported → {len(observations)} kept ({missing} short)")

    fruiting_months = fetch_fruiting_months(species)
    print_windows(species, fruiting_months)

    payload = {
        "fetched_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "source": f"{API}/v2/observations",
        "licenses": LICENSES,
        "fruiting_months": fruiting_months,
        "observations": observations,
    }
    write_json_safely(payload, out, items_key="observations")
    print(f"\nwrote {out} ({len(observations)} observations)")


if __name__ == "__main__":
    main()
