"""Stage 1b: fetch fruit and nut street trees from the City of Portland tree inventory.

Writes data/raw/street_trees.json. Only the species listed in edibles.toml are
requested, and only living trees. The address field is deliberately not fetched.

    uv run pipeline/fetch_trees.py
    uv run pipeline/fetch_trees.py --refresh
"""

import argparse
import datetime
import tomllib
from pathlib import Path

from common import RAW_DIR, fetch_json, write_json_safely

LAYER = "https://www.portlandmaps.com/arcgis/rest/services/Public/Parks_UF_Tree_Layers/MapServer/13"
PAGE_SIZE = 2000  # the layer's maxRecordCount
FIELDS = "OBJECTID,species_common,species_latin,condition,diameter"


def wanted_species():
    with open(Path(__file__).parent / "edibles.toml", "rb") as f:
        config = tomllib.load(f)
    names = [name for species in config["species"] for name in species["inventory_names"]]
    assert all("'" not in name for name in names), "quote in a species name would break the query"
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--refresh", action="store_true", help="refetch even if a raw file exists")
    parser.add_argument("--limit", type=int, help="stop after this many trees (for testing)")
    parser.add_argument("--out", default=str(RAW_DIR / "street_trees.json"))
    args = parser.parse_args()

    out = Path(args.out)
    if out.exists() and not args.refresh:
        print(f"{out} exists; pass --refresh to refetch")
        return

    names = wanted_species()
    where = "status = 'Alive' AND species_common IN (" + ",".join(f"'{n}'" for n in names) + ")"
    expected = fetch_json(
        LAYER + "/query", data={"where": where, "returnCountOnly": "true", "f": "json"}
    )["count"]
    print(f"fetch_trees: layer reports {expected} living trees across {len(names)} species names")

    trees, offset = [], 0
    while True:
        page = fetch_json(
            LAYER + "/query",
            data={
                "where": where,
                "outFields": FIELDS,
                "outSR": 4326,
                "orderByFields": "OBJECTID",  # stable order, so offsets never skip or repeat
                "resultOffset": offset,
                "resultRecordCount": PAGE_SIZE,
                "f": "geojson",
            },
        )
        features = page.get("features", [])
        if not features:
            break
        trees.extend(features)
        offset += len(features)
        print(f"  {len(trees)}/{expected}")
        if args.limit and len(trees) >= args.limit:
            trees = trees[: args.limit]
            break

    unique = len({t["properties"]["OBJECTID"] for t in trees})
    assert unique == len(trees), f"paging returned {len(trees) - unique} duplicate trees"
    if not args.limit:
        assert len(trees) == expected, f"expected {expected} trees, fetched {len(trees)}"
    payload = {
        "fetched_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "source": LAYER,
        "features": trees,
    }
    write_json_safely(payload, out, items_key="features")
    print(f"wrote {out} ({len(trees)} trees)")


if __name__ == "__main__":
    main()
