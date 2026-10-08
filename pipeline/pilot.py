"""Try the enrichment steps on a small sample before paying for a full run.

For each sampled place this fetches its website, searches for pages that
mention it, and has several models write the record. It then writes a
side-by-side report with what every step cost.

    uv run pipeline/pilot.py --n 20
    uv run pipeline/pilot.py --n 2 --include "Some Place" "Another Place"

Everything fetched is cached under data/raw/pilot/, so a rerun spends nothing
on work already done. The report goes to private/pilot/report.html.
"""

import argparse
import concurrent.futures
import contextlib
import html
import json
import random
import statistics

import anthropic

import budget
import exa
import extract
from common import RAW_DIR, ROOT
from enrich import PAGE_CHARS, SUBPAGES, cached, find_mentions, load_pool

CACHE = RAW_DIR / "pilot"
REPORT = ROOT / "private" / "pilot" / "report.html"

MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
SEARCH_KINDS = ["instant", "auto"]  # the cheaper and the default search; compared in the report
EXTRACT_FROM = "auto"  # which search's mentions the models are given

EST_EXA_PER_PLACE = 0.03
EST_CLAUDE_PER_PLACE = 0.06
FULL_RUN_PLACES = 2029

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        key: extract.SCHEMA["properties"][key]
        for key in ("summary", "kind", "known_for", "features", "status")
    },
    "required": ["summary", "kind", "known_for", "features", "status"],
}
SUMMARY_QUERY = (
    "Describe this business for a city map, using only what the page says: one factual "
    "sentence of at most 25 words, the specific kind of place, up to three things it "
    "offers, and which of the listed features it explicitly has."
)


def choose(pool, n, seed, includes):
    by_name = {}
    for place in pool:
        by_name.setdefault(place["name"].casefold(), place)
    chosen = []
    for name in includes:
        if name.casefold() not in by_name:
            raise SystemExit(f"--include {name!r} is not in the pool")
        chosen.append(by_name[name.casefold()])
    rng = random.Random(seed)
    rest = [p for p in pool if p not in chosen]
    rng.shuffle(rest)
    # Mostly places with a website, plus a few without, to see what mentions alone give.
    without = [p for p in rest if not p["url"]][: max(1, n // 7)]
    with_site = [p for p in rest if p["url"]]
    return (chosen + without + with_site)[:n]


def fetch_site(place):
    if not place["url"]:
        return {"pages": [], "status": "no website listed", "exa_summary": None}
    try:
        response = exa.contents(
            [place["url"]],
            max_chars=PAGE_CHARS,
            subpages=SUBPAGES,
            subpage_target=["about", "menu"],
            summary={"query": SUMMARY_QUERY, "schema": SUMMARY_SCHEMA},
            what="pilot website text and summary",
        )
    except Exception as e:  # one bad site must not stop the sample
        return {"pages": [], "status": f"request failed: {e}", "exa_summary": None}
    results = response.get("results") or []
    if not results:
        statuses = response.get("statuses") or [{}]
        tag = (statuses[0].get("error") or {}).get("tag", "no content returned")
        return {"pages": [], "status": f"fetch failed: {tag}", "exa_summary": None}
    main = results[0]
    pages = [{"url": main.get("url", place["url"]), "text": main.get("text") or ""}]
    for sub in main.get("subpages") or []:
        if sub.get("text"):
            pages.append({"url": sub.get("url", ""), "text": sub["text"][: PAGE_CHARS // 2]})
    summary = main.get("summary")
    with contextlib.suppress(TypeError, ValueError):
        summary = json.loads(summary)
    return {
        "pages": [p for p in pages if p["text"].strip()],
        "status": "ok",
        "exa_summary": summary if isinstance(summary, dict) else None,
    }


def run_place(client, place, models):
    site = cached(
        CACHE / "site" / f"{place['id']}.json",
        lambda: fetch_site(place),
        keep=lambda site: not site["status"].startswith("request failed"),
    )
    mentions = {
        kind: cached(
            CACHE / f"search-{kind}" / f"{place['id']}.json",
            lambda kind=kind: find_mentions(place, kind, f"pilot search ({kind})")["mentions"],
        )
        for kind in SEARCH_KINDS
    }
    records = {}
    for model in models:

        def call(model=model):
            record, usage, usd = extract.extract(
                client, model, place, site["pages"], mentions[EXTRACT_FROM]
            )
            return {"record": record, "usage": usage, "usd": usd}

        records[model] = cached(CACHE / "extract" / model / f"{place['id']}.json", call)
    print(f"  done: {place['name']}")
    return {"place": place, "site": site, "mentions": mentions, "records": records}


# --- report -----------------------------------------------------------------


def esc(value):
    return html.escape(str(value))


def cell(record, key):
    if record is None:
        return "<td class='none'>no result</td>"
    if "error" in record:
        return f"<td class='none'>{esc(record['error'])}</td>"
    value = record.get(key)
    if isinstance(value, list):
        value = ", ".join(v.replace("_", " ") for v in value)
    if value in ("", None):
        return "<td class='none'>blank</td>"
    return f"<td>{esc(value)}</td>"


def place_section(row, models):
    place, site, mentions = row["place"], row["site"], row["mentions"]
    arms = [(m.removeprefix("claude-"), row["records"][m]["record"]) for m in models]
    arms.append(("exa summary (website only)", site["exa_summary"]))
    links = []
    if place["url"]:
        links.append(f"<a href='{esc(place['url'])}'>website</a>")
    chars = sum(len(p["text"]) for p in site["pages"])
    out = [
        "<section>",
        f"<h2>{esc(place['name'])}</h2>",
        f"<p class='meta'>{esc(place['label'])}"
        + "".join(f", {esc(d)}" for d in place["details"])
        + ("; " + " ".join(links) if links else "")
        + "</p>",
        f"<p class='meta'>Website: {esc(site['status'])}, {len(site['pages'])} pages, "
        f"{chars:,} characters. Mentions found: "
        + "; ".join(
            f"{kind} search {len(found)} (" + ", ".join(esc(m["domain"]) for m in found) + ")"
            for kind, found in mentions.items()
        )
        + "</p>",
        "<table><thead><tr><th></th>" + "".join(f"<th>{esc(name)}</th>" for name, _ in arms),
        "</tr></thead><tbody>",
    ]
    for key, label in [
        ("summary", "Summary"),
        ("kind", "Kind"),
        ("known_for", "Known for"),
        ("features", "Features"),
        ("status", "Status"),
        ("matches_place", "Website matches"),
        ("press_note", "Press note (private)"),
    ]:
        out.append(f"<tr><th>{label}</th>" + "".join(cell(rec, key) for _, rec in arms) + "</tr>")
    usage_cells = "".join(
        f"<td>{row['records'][m]['usage']['input_tokens']:,} in, "
        f"{row['records'][m]['usage']['output_tokens']:,} out, "
        f"${row['records'][m]['usd']:.4f}</td>"
        for m in models
    )
    out.append(f"<tr><th>Tokens and cost</th>{usage_cells}<td class='none'>see totals</td></tr>")
    out.append("</tbody></table>")
    out.append("<details><summary>Source text the models were given</summary>")
    for page in site["pages"]:
        out.append(f"<h3>{esc(page['url'])}</h3><pre>{esc(page['text'][:2500])}</pre>")
    for kind, found in mentions.items():
        out.append(f"<h3>Mentions from the {kind} search</h3>")
        for m in found:
            out.append(
                f"<p><a href='{esc(m['url'])}'>{esc(m['title'] or m['url'])}</a> "
                f"({esc(m['domain'])})<br>{esc(' … '.join(m['highlights']))}</p>"
            )
    out.append("</details></section>")
    return "\n".join(out)


def totals_table(rows, models):
    n = len(rows)
    lines = [
        "<table><thead><tr><th>Step</th><th>Spent on this sample</th><th>Per place</th>"
        f"<th>Projected for {FULL_RUN_PLACES:,} places</th></tr></thead><tbody>"
    ]
    spend = {}
    for entry in budget.entries():
        if entry["what"].startswith("pilot"):
            spend[entry["what"]] = spend.get(entry["what"], 0) + entry["usd"]
    for what, usd in sorted(spend.items()):
        lines.append(
            f"<tr><th>Exa: {esc(what.removeprefix('pilot '))}</th><td>${usd:.3f}</td>"
            f"<td>${usd / n:.4f}</td><td>${usd / n * FULL_RUN_PLACES:.2f}</td></tr>"
        )
    for model in models:
        usd = sum(r["records"][model]["usd"] for r in rows)
        tin = statistics.mean(r["records"][model]["usage"]["input_tokens"] for r in rows)
        tout = statistics.mean(r["records"][model]["usage"]["output_tokens"] for r in rows)
        full = usd / n * FULL_RUN_PLACES
        lines.append(
            f"<tr><th>{esc(model.removeprefix('claude-'))}: {tin:,.0f} tokens in, "
            f"{tout:,.0f} out per place</th><td>${usd:.3f}</td><td>${usd / n:.4f}</td>"
            f"<td>${full:.2f}, or ${full / 2:.2f} as a batch</td></tr>"
        )
    lines.append("</tbody></table>")
    return "\n".join(lines)


def write_report(rows, models):
    body = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Enrichment pilot</title><style>",
        "body{font:14px/1.45 system-ui,sans-serif;color:#22262a;background:#f2f3f0;"
        "margin:0;padding:24px 16px 60px}main{max-width:1180px;margin:0 auto}",
        "h1{font-size:22px;margin:0 0 6px}h2{font-size:17px;margin:0 0 2px}"
        "h3{font-size:13px;margin:14px 0 4px}",
        "section{background:#fff;border:1px solid #d9dbd6;border-radius:8px;"
        "padding:14px 16px;margin:16px 0;overflow-x:auto}",
        ".meta{margin:0 0 4px;color:#5c6166}table{border-collapse:collapse;width:100%;"
        "margin-top:8px}",
        "th,td{border-top:1px solid #d9dbd6;padding:6px 8px;text-align:left;"
        "vertical-align:top}thead th{border-top:0;font-weight:600}",
        "tbody th{font-weight:600;white-space:nowrap;width:1%}.none{color:#8a8f94}",
        "pre{white-space:pre-wrap;background:#f2f3f0;padding:10px;border-radius:6px;"
        "max-height:260px;overflow:auto}a{color:#22262a}details{margin-top:10px}",
        "</style></head><body><main>",
        f"<h1>Enrichment pilot: {len(rows)} places</h1>",
        "<p class='meta'>Each place's website text and the mentions found by the "
        f"{EXTRACT_FROM} search were given to three models with the same instructions. "
        "The last column is Exa's own summary of the homepage alone.</p>",
        "<section><h2>Cost</h2>",
        totals_table(rows, models),
        "</section>",
    ]
    body += [place_section(row, models) for row in rows]
    body.append("</main></body></html>")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORT.with_suffix(".tmp")
    tmp.write_text("\n".join(body))
    tmp.replace(REPORT)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--n", type=int, default=20, help="places to sample")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--include", nargs="*", default=[], help="place names to include")
    parser.add_argument("--models", nargs="+", default=MODELS)
    args = parser.parse_args()

    pool = load_pool()
    chosen = choose(pool, args.n, args.seed, args.include)
    with_site = sum(bool(p["url"]) for p in chosen)
    print(f"pilot: {len(chosen)} of {len(pool)} places ({with_site} with a website)")
    todo = [
        p for p in chosen if not (CACHE / "extract" / args.models[-1] / f"{p['id']}.json").exists()
    ]
    budget.require(
        len(todo) * (EST_EXA_PER_PLACE + EST_CLAUDE_PER_PLACE), f"pilot on {len(todo)} new places"
    )

    client = anthropic.Anthropic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool_:
        rows = list(pool_.map(lambda place: run_place(client, place, args.models), chosen))

    write_report(rows, args.models)
    print(f"pilot: wrote {REPORT}")
    budget.main()


if __name__ == "__main__":
    main()
