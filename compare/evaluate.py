"""Check how often a way of scoring places agrees with the side-by-side judgments.

Each judgment says which of two places should be shown first. A scorer agrees
when it gives the chosen place the higher score. With no arguments this checks
the category weights alone; each argument is a folder of scores written by
pipeline/rank.py.

    uv run compare/evaluate.py
    uv run compare/evaluate.py private/fit
    uv run compare/evaluate.py private/fit --categories    # also list scores by category
"""

import collections
import contextlib
import io
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from build_features import build_places, drop_duplicates, load_toml  # noqa: E402
from common import RAW_DIR  # noqa: E402

JUDGMENTS = ROOT / "private" / "judgments.jsonl"


def final_answers():
    """Replay the log: an undo cancels the answer before it."""
    answers = {}
    for line in JUDGMENTS.read_text().splitlines():
        row = json.loads(line)
        if row.get("undo"):
            answers.pop(row["pair_id"], None)
        else:
            answers[row["pair_id"]] = row
    return list(answers.values())


def all_places():
    raw = json.loads((RAW_DIR / "osm_places.json").read_text())
    with contextlib.redirect_stdout(io.StringIO()):
        return drop_duplicates(build_places(raw, load_toml("categories.toml")))


def category_table(places, scores):
    """Median score and the top five places in each category, best category first."""
    by_category = collections.defaultdict(list)
    for place in places:
        if place["id"] in scores:
            by_category[place["cat"]].append((scores[place["id"]], place["name"]))
    print(f"\n{'category':18} {'places':>6} {'median':>6} {'80+':>4}  top five")
    for category, rows in sorted(
        by_category.items(), key=lambda item: -statistics.median(r[0] for r in item[1])
    ):
        rows.sort(reverse=True)
        top = "; ".join(f"{name} {score:g}" for score, name in rows[:5])
        median = statistics.median(r[0] for r in rows)
        high = sum(r[0] >= 80 for r in rows)
        print(f"{category:18} {len(rows):6d} {median:6.0f} {high:4d}  {top}")


def fit_scores(folder):
    return {
        path.stem: json.loads(path.read_text())["fit"]
        for path in Path(folder).glob("*.json")
        if path.stem != "batch"
    }


def evaluate(name, scores, answers, show_misses):
    tally = {"within": [0, 0], "cross": [0, 0]}
    ties = unscored = 0
    misses = []
    for row in answers:
        a, b = row["a"], row["b"]
        if row["choice"] not in ("a", "b"):
            continue
        if a["id"] not in scores or b["id"] not in scores:
            unscored += 1
            continue
        if scores[a["id"]] == scores[b["id"]]:
            ties += 1  # counted as half right, since a coin flip would be
            tally[row["kind"]][0] += 0.5
        else:
            picked = "a" if scores[a["id"]] > scores[b["id"]] else "b"
            tally[row["kind"]][0] += picked == row["choice"]
            if picked != row["choice"]:
                winner, loser = (a, b) if row["choice"] == "a" else (b, a)
                misses.append(
                    f"    you chose {winner['name']} ({scores[winner['id']]:g}) over "
                    f"{loser['name']} ({scores[loser['id']]:g})"
                )
        tally[row["kind"]][1] += 1
    right = tally["within"][0] + tally["cross"][0]
    total = tally["within"][1] + tally["cross"][1]
    print(
        f"{name}: agrees on {right:g} of {total} ({100 * right / total:.0f}%); "
        f"same category {tally['within'][0]:g} of {tally['within'][1]}, "
        f"different categories {tally['cross'][0]:g} of {tally['cross'][1]}"
        + (f"; {ties} tied scores" if ties else "")
        + (f"; {unscored} pairs not scored" if unscored else "")
    )
    if show_misses:
        print("\n".join(misses))


def main():
    answers = final_answers()
    decided = sum(row["choice"] in ("a", "b") for row in answers)
    print(f"{len(answers)} pairs answered, {decided} with a preference")
    folders = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    places = all_places()
    weights = {place["id"]: place["score"] for place in places}
    evaluate("category weights", weights, answers, show_misses=not folders)
    for n, folder in enumerate(folders, start=1):
        evaluate(folder, fit_scores(folder), answers, show_misses=n == len(folders))
    if "--categories" in sys.argv and folders:
        category_table(places, fit_scores(folders[-1]))


if __name__ == "__main__":
    main()
