"""A running ledger of money spent on paid services, with a ceiling.

Every paid call records what it cost in private/spend.jsonl. Anything that is
about to spend calls require() first, which stops the run if the estimate
would take the project past its budget.

    uv run pipeline/budget.py     # print what has been spent so far
"""

import collections
import datetime
import json
import os
import threading

from common import ROOT

BUDGET_USD = 50.00
LEDGER = ROOT / "private" / "spend.jsonl"
_lock = threading.Lock()  # paid calls run on several threads; ledger lines must not interleave


def entries():
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]


def spent():
    return sum(entry["usd"] for entry in entries())


def record(service, what, usd, **detail):
    """Append one charge. Written and flushed before the caller moves on."""
    row = {
        "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "service": service,
        "what": what,
        "usd": round(usd, 6),
        **detail,
    }
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(LEDGER, "a") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())


def require(estimate_usd, what):
    """Refuse to start something that would not fit in what is left."""
    so_far = spent()
    if so_far + estimate_usd > BUDGET_USD:
        raise SystemExit(
            f"{what} is estimated at ${estimate_usd:.2f}, but ${so_far:.2f} of the "
            f"${BUDGET_USD:.2f} budget is already spent. Not starting."
        )
    print(
        f"budget: {what} estimated at ${estimate_usd:.2f}; "
        f"${so_far:.2f} spent, ${BUDGET_USD - so_far:.2f} left"
    )


def main():
    by_service = collections.defaultdict(float)
    for entry in entries():
        by_service[f"{entry['service']}: {entry['what']}"] += entry["usd"]
    for label, usd in sorted(by_service.items()):
        print(f"  ${usd:8.4f}  {label}")
    print(f"  ${spent():8.4f}  total of ${BUDGET_USD:.2f}")


if __name__ == "__main__":
    main()
