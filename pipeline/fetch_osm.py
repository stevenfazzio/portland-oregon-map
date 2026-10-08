"""Stage 1a: fetch named places in the Portland bounding box from OpenStreetMap.

Writes data/raw/osm_places.json. The public Overpass servers are unreliable, so
each tag key is fetched separately and saved as it arrives; a rerun picks up
where the last one stopped. An existing raw file is kept unless --refresh is passed.

    uv run pipeline/fetch_osm.py
    uv run pipeline/fetch_osm.py --refresh
"""

import argparse
import datetime
import json
import shutil
import time
import urllib.error
from pathlib import Path

from common import BBOX, RAW_DIR, fetch_json, write_json_safely

KEYS = ["amenity", "shop", "leisure", "tourism", "craft"]
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
ROUNDS = 4
MAX_AGE_DAYS = 30
PAUSE_BETWEEN_KEYS = 10  # seconds; the main server rate-limits back-to-back queries


def fetch_key(key):
    south, west, north, east = BBOX
    query = (
        f'[out:json][timeout:90];nwr["name"]["{key}"]({south},{west},{north},{east});'
        "out tags center;"
    )
    for round_number in range(ROUNDS):
        for mirror in MIRRORS:
            host = mirror.split("/")[2]
            try:
                # One attempt per mirror: a busy mirror is quicker to skip than to wait out.
                result = fetch_json(mirror, data={"data": query}, timeout=120, max_retries=1)
            except (urllib.error.URLError, TimeoutError) as e:
                print(f"  {key}: {host} failed ({e})")
                continue
            elements = result["elements"]
            osm_base = result.get("osm3s", {}).get("timestamp_osm_base")
            age = datetime.datetime.now(datetime.UTC) - datetime.datetime.fromisoformat(osm_base)
            if age.days > MAX_AGE_DAYS:
                # Some mirrors stop replicating and serve months-old data without saying so.
                print(f"  {key}: {host} is {age.days} days behind, skipping")
                continue
            print(f"  {key}: {len(elements)} elements from {host} (OSM data as of {osm_base})")
            return {
                "source": {"mirror": host, "osm_base": osm_base, "count": len(elements)},
                "elements": elements,
            }
        wait = 30 * (round_number + 1)
        print(f"  {key}: every mirror failed, waiting {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"could not fetch '{key}' from any Overpass mirror; rerun to resume")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--refresh", action="store_true", help="refetch even if a raw file exists")
    parser.add_argument("--keys", nargs="+", default=KEYS, help="tag keys to fetch")
    parser.add_argument("--out", default=str(RAW_DIR / "osm_places.json"))
    args = parser.parse_args()

    out = Path(args.out)
    if out.exists() and not args.refresh:
        print(f"{out} exists; pass --refresh to refetch")
        return

    # One file per key, so a failure on the last key does not throw away the others.
    parts_dir = out.with_suffix(".parts")
    parts = {}
    for key in args.keys:
        part_path = parts_dir / f"{key}.json"
        if part_path.exists():
            parts[key] = json.loads(part_path.read_text())
            print(f"  {key}: {len(parts[key]['elements'])} elements already fetched")
            continue
        if parts:
            time.sleep(PAUSE_BETWEEN_KEYS)
        parts[key] = fetch_key(key)
        write_json_safely(parts[key], part_path, items_key="elements", min_items=0)

    by_id = {}
    for part in parts.values():
        for el in part["elements"]:
            by_id[(el["type"], el["id"])] = el  # a feature can carry several of these keys
    fetched = sum(len(part["elements"]) for part in parts.values())
    print(f"fetch_osm: {fetched} fetched → {len(by_id)} unique ({fetched - len(by_id)} duplicates)")
    payload = {
        "fetched_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "bbox": BBOX,
        "sources": {key: part["source"] for key, part in parts.items()},
        "elements": list(by_id.values()),
    }
    write_json_safely(payload, out, items_key="elements")
    shutil.rmtree(parts_dir)  # a finished run leaves no checkpoint behind
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
