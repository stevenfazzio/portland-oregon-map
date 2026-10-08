"""Stage 1d: gather what the web says about each place and write its record.

Three steps, each cached per place so a rerun only does what is missing:

  1. fetch the place's own website text
  2. search for other pages that mention it
  3. have a model write the record from both (one batch request per place)

    uv run pipeline/enrich.py --limit 5     # try a few places first
    uv run pipeline/enrich.py               # everything; safe to stop and rerun
    uv run pipeline/enrich.py --pairs private/pairs.json   # just the places being compared
    uv run pipeline/enrich.py --ids more.txt               # just the places listed, one id per line

Reads   data/raw/osm_places.json
Writes  data/raw/enrich/{site,search,record}/<place id>.json

Steps 1 and 2 cost money at Exa and step 3 at Anthropic; each checks the spend
ledger before starting. A submitted batch is remembered in batch.json, so
stopping the script while it waits does not resubmit or lose the work.
"""

import argparse
import concurrent.futures
import contextlib
import io
import json
import urllib.parse
from pathlib import Path

import anthropic

import batching
import budget
import exa
import extract
from build_features import build_places, drop_duplicates, load_toml
from common import RAW_DIR, write_json_safely

CACHE = RAW_DIR / "enrich"
BATCH_FILE = CACHE / "batch.json"
SOURCES = ("site", "search")
MODEL = "claude-sonnet-5-5"

JUDGED_FAMILIES = {"food", "drink", "music_screen", "shops"}
MIN_WEIGHT = 0.4
PAGE_CHARS = 6000
SUBPAGES = 2
MENTIONS = 5
SEARCH_KIND = "auto"
# Listings, ordering and social sites say little about what a place is like.
SKIP_DOMAINS = [
    "yelp.com",
    "tripadvisor.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "doordash.com",
    "ubereats.com",
    "grubhub.com",
    "toasttab.com",
    "opentable.com",
    "mapquest.com",
    "yellowpages.com",
    "foursquare.com",
    "restaurantji.com",
    "atly.com",
    "exa.ai",
]
# Measured on a 20-place sample; used only to check the budget before starting.
EST_SITE = 0.002
EST_SEARCH = 0.0075
EST_RECORD = 0.0050  # batch price


def load_pool(extra_ids=frozenset(), include_chains=False):
    """The places worth enriching: independent, and in a category the map favours.

    Places named in extra_ids are included whatever their category. Chain
    locations are left out unless include_chains is set.
    """
    config = load_toml("categories.toml")
    raw = json.loads((RAW_DIR / "osm_places.json").read_text())
    tags_by_id = {f"{el['type'][0]}{el['id']}": el.get("tags", {}) for el in raw["elements"]}
    with contextlib.redirect_stdout(io.StringIO()):
        places = drop_duplicates(build_places(raw, config))
    weight = {c["id"]: c["weight"] for c in config["category"]}
    pool = []
    for place in places:
        in_scope = (
            place["fam"] in JUDGED_FAMILIES
            and (include_chains or not place["chain"])
            and weight[place["cat"]] >= MIN_WEIGHT
        )
        if not in_scope and place["id"] not in extra_ids:
            continue
        tags = tags_by_id[place["id"]]
        details = []
        if "cuisine" in tags:
            details.append(tags["cuisine"].replace(";", ", ").replace("_", " "))
        street = " ".join(tags[k] for k in ("addr:housenumber", "addr:street") if k in tags)
        if street:
            details.append(street)
        pool.append({**place, "label": place["sub"], "details": details})
    return sorted(pool, key=lambda p: p["id"])


def cached(path, make, keep=lambda value: True):
    """Return the JSON at path, creating it with make() the first time only.

    A value that fails keep() is returned but not saved, so it is retried next run.
    """
    if path.exists():
        return json.loads(path.read_text())
    value = make()
    if keep(value):
        write_json_safely(value, path)
    return value


def fetch_site(place):
    if not place["url"]:
        return {"pages": [], "status": "no website listed"}
    try:
        response = exa.contents(
            [place["url"]],
            max_chars=PAGE_CHARS,
            subpages=SUBPAGES,
            subpage_target=["about", "menu"],
            what="website text",
        )
    except Exception as e:  # one bad site must not stop the run
        return {"pages": [], "status": f"request failed: {e}"}
    results = response.get("results") or []
    if not results:
        statuses = response.get("statuses") or [{}]
        tag = (statuses[0].get("error") or {}).get("tag", "no content returned")
        return {"pages": [], "status": f"fetch failed: {tag}"}
    main = results[0]
    pages = [{"url": main.get("url", place["url"]), "text": main.get("text") or ""}]
    for sub in main.get("subpages") or []:
        if sub.get("text"):
            pages.append({"url": sub.get("url", ""), "text": sub["text"][: PAGE_CHARS // 2]})
    return {"pages": [p for p in pages if p["text"].strip()], "status": "ok"}


def find_mentions(place, kind=SEARCH_KIND, what="search for mentions"):
    own = urllib.parse.urlparse(place["url"]).hostname or ""
    skip = SKIP_DOMAINS + ([own.removeprefix("www.")] if own else [])
    try:
        response = exa.search(
            f"Articles and write-ups about {place['name']}, a {place['label'].lower()} "
            "in Portland, Oregon",
            kind=kind,
            num_results=MENTIONS,
            highlight_query=f"What is {place['name']} like, and what is it known for?",
            exclude_domains=skip,
            what=what,
        )
    except Exception as e:
        return {"mentions": [], "status": f"request failed: {e}"}
    mentions = []
    for result in response.get("results") or []:
        highlights = [h.strip() for h in result.get("highlights") or [] if h.strip()]
        if not highlights:
            continue
        host = urllib.parse.urlparse(result.get("url", "")).hostname or ""
        mentions.append(
            {
                "title": (result.get("title") or "").strip(),
                "url": result.get("url", ""),
                "domain": host.removeprefix("www."),
                "date": result.get("publishedDate") or "",
                "highlights": highlights,
            }
        )
    return {"mentions": mentions, "status": "ok"}


def not_a_failed_request(value):
    return not value["status"].startswith("request failed")


def gather_sources(places):
    """Steps 1 and 2 for every place that lacks them."""
    need_site = [p for p in places if not (CACHE / "site" / f"{p['id']}.json").exists()]
    need_search = [p for p in places if not (CACHE / "search" / f"{p['id']}.json").exists()]
    estimate = sum(EST_SITE for p in need_site if p["url"]) + EST_SEARCH * len(need_search)
    print(f"sources: {len(need_site)} sites and {len(need_search)} searches to fetch")
    if not (need_site or need_search):
        return
    budget.require(estimate, "fetching websites and mentions")

    done = 0

    def one(place):
        cached(
            CACHE / "site" / f"{place['id']}.json", lambda: fetch_site(place), not_a_failed_request
        )
        cached(
            CACHE / "search" / f"{place['id']}.json",
            lambda: find_mentions(place),
            not_a_failed_request,
        )

    todo = {p["id"]: p for p in need_site + need_search}.values()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        for _ in executor.map(one, todo):
            done += 1
            if done % 100 == 0:
                print(f"  sources: {done}/{len(todo)}")
    missing = sum(
        not (CACHE / kind / f"{p['id']}.json").exists()
        for p in places
        for kind in ("site", "search")
    )
    print(f"sources: done, {missing} still missing after failed requests (rerun to retry)")


def load_sources(place):
    site = json.loads((CACHE / "site" / f"{place['id']}.json").read_text())
    search = json.loads((CACHE / "search" / f"{place['id']}.json").read_text())
    return site["pages"], search["mentions"]


def write_records_now(places, model):
    """Step 3 without the batch: one call per place, at full price, for a handful of places."""
    client = anthropic.Anthropic()
    todo = [p for p in places if not (CACHE / "record" / f"{p['id']}.json").exists()]
    ready = [p for p in todo if all((CACHE / k / f"{p['id']}.json").exists() for k in SOURCES)]
    if len(ready) < len(todo):
        print(f"records: {len(todo) - len(ready)} places skipped, sources missing (rerun)")
    todo = ready
    print(f"records: {len(todo)} to write one by one")
    if not todo:
        return
    budget.require(2 * EST_RECORD * len(todo), f"writing {len(todo)} records with {model}")
    for place in todo:
        record, usage, _ = extract.extract(client, model, place, *load_sources(place))
        if "error" in record:
            print(f"  {place['name']}: {record['error']}")
            continue
        path = CACHE / "record" / f"{place['id']}.json"
        write_json_safely({"record": record, "model": model, "usage": usage}, path)


def write_records(places, model, poll_seconds):
    """Step 3: one batch for every place without a record, resumable while it runs."""
    client = anthropic.Anthropic()
    params = {}
    if not BATCH_FILE.exists():
        todo = [p for p in places if not (CACHE / "record" / f"{p['id']}.json").exists()]
        todo = [p for p in todo if all((CACHE / k / f"{p['id']}.json").exists() for k in SOURCES)]
        print(f"records: {len(todo)} to write")
        if not todo:
            return
        budget.require(EST_RECORD * len(todo), f"writing {len(todo)} records with {model}")
        params = {
            place["id"]: extract.request_params(
                model, extract.build_prompt(place, *load_sources(place))
            )
            for place in todo
        }
    messages, failed, batch_id = batching.run(client, params, BATCH_FILE, poll_seconds)

    written, usd = 0, 0.0
    for place_id, message in messages.items():
        usage = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        }
        usd += extract.cost_usd(model, usage, batch=True)
        record = extract.parse(message)
        if record is None:
            failed += 1
            continue
        path = CACHE / "record" / f"{place_id}.json"
        write_json_safely({"record": record, "model": model, "usage": usage}, path)
        written += 1
    budget.record(
        "anthropic", f"extract {model} (batch)", usd, batch=batch_id, ok=written, failed=failed
    )
    print(f"records: {written} written, {failed} failed, ${usd:.2f}")
    BATCH_FILE.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, help="only the first N places (for testing)")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--sources-only", action="store_true", help="skip writing records")
    parser.add_argument("--poll", type=int, default=60, help="seconds between batch checks")
    parser.add_argument("--pairs", help="cover only the places in this pairs file")
    parser.add_argument(
        "--ids", help="cover only the places listed in this file, whatever their category"
    )
    parser.add_argument(
        "--now", action="store_true", help="write records one by one, not as a batch"
    )
    args = parser.parse_args()

    if args.pairs:
        pairs = json.loads(Path(args.pairs).read_text())["pairs"]
        wanted = {pair[side]["id"] for pair in pairs for side in ("a", "b")}
        places = [p for p in load_pool(wanted) if p["id"] in wanted]
        print(f"enrich: {len(places)} places from {args.pairs}")
        gather_sources(places)
        write_records_now(places, args.model)
        budget.main()
        return

    if args.ids:
        wanted = set(Path(args.ids).read_text().split())
        places = [p for p in load_pool(wanted) if p["id"] in wanted]
        if len(places) < len(wanted):
            print(f"enrich: {len(wanted) - len(places)} ids in {args.ids} match no place")
    else:
        places = load_pool()
    with_site = sum(bool(p["url"]) for p in places)
    print(f"enrich: {len(places)} places in scope, {with_site} with a website")
    if args.limit:
        places = places[: args.limit]
        print(f"enrich: limited to the first {len(places)}")

    gather_sources(places)
    if args.now:
        write_records_now(places, args.model)
    elif not args.sources_only:
        write_records(places, args.model, args.poll)

    have = sum((CACHE / "record" / f"{p['id']}.json").exists() for p in places)
    print(f"enrich: {have} of {len(places)} places have a record")
    budget.main()


if __name__ == "__main__":
    main()
