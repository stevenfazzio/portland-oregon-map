"""Stage 1e: score how well each described place fits the person the map is for.

Category weights can put record shops above banks; they cannot tell a great
record shop from a dull one. Here a model reads each place's record and gives
it a fit score from 0 to 100, which build_features.py then uses in place of
the category weight.

The description of the person is private. It is read from private/profile.md,
which is not committed, and only the resulting scores reach the map.

    uv run pipeline/rank.py --pairs private/pairs.json    # a few places, one by one
    uv run pipeline/rank.py                               # every place with a record

Chain locations in the favoured categories have no record. They are scored from
the brand name alone, which is enough to tell a local chain from a national one.

Reads   data/raw/enrich/record/, private/profile.md
Writes  private/fit/<place id>.json
"""

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path

import anthropic

import batching
import budget
import extract
from common import RAW_DIR, ROOT, write_json_safely
from enrich import load_pool

PROFILE = ROOT / "private" / "profile.md"
FIT_DIR = ROOT / "private" / "fit"
RECORD_DIR = RAW_DIR / "enrich" / "record"
# The smallest model agreed with the side-by-side judgments at least as often as the
# larger ones (compare/evaluate.py), at a twentieth of the price.
MODEL = "claude-haiku-5-5"
EST_TOKENS = {"input_tokens": 2200, "output_tokens": 120}  # per place, rounded up

SCHEMA = {
    "type": "object",
    "properties": {"fit": {"type": "integer"}, "why": {"type": "string"}},
    "required": ["fit", "why"],
    "additionalProperties": False,
}

SYSTEM = """\
You help rank places on a personal map of Portland, Oregon, made for one person. The map \
can only label a few places at a time, so it needs to know which ones deserve his \
attention first.

<person>
{profile}
</person>

For each place you get a short record: its category, a specific kind, a one-sentence \
description, what it is known for, its features, and an "elsewhere" line reporting how \
other websites describe it. Treat the elsewhere line as evidence about the place's \
quality and character.

Give a fit score from 0 to 100 for how much this place deserves a label on his map:

- 85 to 100: squarely his kind of place, and distinctive in its own right
- 65 to 84: a good fit, or an excellent example of something he likes
- 45 to 64: fine; he might stop in
- 25 to 44: ordinary for him
- 0 to 24: not for him, or reported closed

Judge the particular place and not only its category. A remarkable place in a category \
he cares less about can outrank a dull one in a category he loves. Where the record says \
little, stay near the middle for that kind of place instead of guessing high or low. \
Ignore where in the city it is.

Then give your reason in at most 20 words.\
"""


def build_prompt(place, record):
    lines = [f"Name: {place['name']}", f"Category: {place['label']}"]
    lines += [f"Detail: {detail}" for detail in place.get("details", [])]
    if record is None:
        # Chain locations are not researched one by one; the brand is what matters.
        lines.append(
            "This is one location of a chain, and nothing was gathered about it. Judge it "
            "from what you know of the brand, including whether it is local to Portland or "
            "the Pacific Northwest, or national."
        )
        return "\n".join(lines)
    lines.append(f"Kind: {record['kind']}")
    lines.append(f"Description: {record['summary'] or '(none)'}")
    lines.append(f"Known for: {', '.join(record['known_for']) or '(nothing recorded)'}")
    features = ", ".join(f.replace("_", " ") for f in record["features"])
    lines.append(f"Features: {features or '(none recorded)'}")
    lines.append(f"Elsewhere: {record['press_note'] or '(no mentions)'}")
    if record["status"] == "closed":
        lines.append("Status: reported permanently closed")
    return "\n".join(lines)


def request_params(model, system, prompt):
    return {
        "model": model,
        "max_tokens": 4000,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
    }


def parse(message):
    if message.stop_reason != "end_turn":
        return None
    result = json.loads(next(block.text for block in message.content if block.type == "text"))
    result["fit"] = max(0, min(100, result["fit"]))
    return result


def usage_of(message):
    return {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--pairs", help="score only the places in this pairs file, one by one")
    parser.add_argument("--out", default=str(FIT_DIR), help="where to write the scores")
    parser.add_argument("--poll", type=int, default=60, help="seconds between batch checks")
    parser.add_argument(
        "--now", action="store_true", help="score one by one at full price instead of as a batch"
    )
    args = parser.parse_args()

    if not PROFILE.exists():
        raise SystemExit(f"{PROFILE} is missing: it describes who the map is for.")
    profile = PROFILE.read_text().strip()
    system = SYSTEM.format(profile=profile)
    stamp = {"model": args.model, "profile": hashlib.sha256(profile.encode()).hexdigest()[:12]}
    out_dir = Path(args.out)

    wanted = None
    if args.pairs:
        pairs = json.loads(Path(args.pairs).read_text())["pairs"]
        wanted = {pair[side]["id"] for pair in pairs for side in ("a", "b")}
    described = {path.stem for path in RECORD_DIR.glob("*.json")}
    places = [
        place
        for place in load_pool(described, include_chains=True)
        if (place["id"] in described or place["chain"])
        and (wanted is None or place["id"] in wanted)
    ]

    def is_current(place):
        """True if this place already has a score from this model and this profile."""
        path = out_dir / f"{place['id']}.json"
        if not path.exists():
            return False
        saved = json.loads(path.read_text())
        return all(saved.get(key) == value for key, value in stamp.items())

    todo = [place for place in places if not is_current(place)]
    chains = sum(place["id"] not in described for place in places)
    print(
        f"rank: {len(places) - chains} places with a record and {chains} chain locations; "
        f"{len(todo)} to score with {args.model}"
    )

    def prompt_for(place):
        path = RECORD_DIR / f"{place['id']}.json"
        record = json.loads(path.read_text())["record"] if path.exists() else None
        return request_params(args.model, system, build_prompt(place, record))

    client = anthropic.Anthropic()
    state_file = out_dir / "batch.json"
    each = extract.cost_usd(args.model, EST_TOKENS)
    if args.pairs or args.now:
        if todo:
            budget.require(each * len(todo), f"scoring {len(todo)} places one by one")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            replies = executor.map(lambda place: client.messages.create(**prompt_for(place)), todo)
            messages = {place["id"]: reply for place, reply in zip(todo, replies, strict=True)}
        failed, batch_id, batch = 0, None, False
    else:
        if not todo and not state_file.exists():
            return
        if not state_file.exists():
            budget.require(each * len(todo) / 2, f"scoring {len(todo)} places as a batch")
        params = {place["id"]: prompt_for(place) for place in todo}
        messages, failed, batch_id = batching.run(client, params, state_file, args.poll)
        batch = True

    written, usd, tokens_in, tokens_out = 0, 0.0, 0, 0
    for place_id, message in messages.items():
        usage = usage_of(message)
        usd += extract.cost_usd(args.model, usage, batch=batch)
        tokens_in += usage["input_tokens"]
        tokens_out += usage["output_tokens"]
        result = parse(message)
        if result is None:
            failed += 1
            continue
        write_json_safely({**result, **stamp}, out_dir / f"{place_id}.json")
        written += 1
    if messages:
        budget.record(
            "anthropic",
            f"rank {args.model}" + (" (batch)" if batch else ""),
            usd,
            batch=batch_id,
            ok=written,
            failed=failed,
        )
        each = f"{tokens_in / len(messages):.0f} tokens in, {tokens_out / len(messages):.0f} out"
        print(f"rank: {written} scored, {failed} failed, ${usd:.2f} ({each} per place)")
    if batch and state_file.exists():
        state_file.unlink()


if __name__ == "__main__":
    main()
