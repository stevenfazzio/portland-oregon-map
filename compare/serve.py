"""Serve the side-by-side judging page and record each answer as it is given.

    uv run compare/serve.py

Then open http://127.0.0.1:8766/. Answers are appended to private/judgments.jsonl,
one line each, the moment they are given, so closing the page loses nothing and
reopening it carries on from the next unanswered pair.
"""

import argparse
import datetime
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = Path(__file__).with_name("index.html")
RECORD_DIR = ROOT / "data" / "raw" / "enrich" / "record"
CHOICES = {"a", "b", "same", "skip"}


class Session:
    def __init__(self, pairs_path, judgments_path):
        self.pairs = json.loads(pairs_path.read_text())["pairs"]
        self.by_id = {pair["id"]: pair for pair in self.pairs}
        self.judgments_path = judgments_path
        self.answered = []  # pair ids, in the order they were answered
        if judgments_path.exists():
            for line in judgments_path.read_text().splitlines():
                self._replay(json.loads(line))

    def _replay(self, row):
        if row.get("undo"):
            if row["pair_id"] in self.answered:
                self.answered.remove(row["pair_id"])
        elif row["pair_id"] in self.by_id and row["pair_id"] not in self.answered:
            self.answered.append(row["pair_id"])

    def _append(self, row):
        row["at"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        self.judgments_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.judgments_path, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._replay(row)

    def answer(self, pair_id, choice):
        pair = self.by_id[pair_id]
        # Each line stands on its own, so it stays readable if the pairs file is replaced.
        self._append(
            {
                "pair_id": pair_id,
                "kind": pair["kind"],
                "choice": choice,
                "a": {k: pair["a"][k] for k in ("id", "name", "cat")},
                "b": {k: pair["b"][k] for k in ("id", "name", "cat")},
            }
        )

    def undo(self):
        if self.answered:
            self._append({"pair_id": self.answered[-1], "undo": True})

    @staticmethod
    def with_record(card):
        """Add what enrich.py wrote about the place, so the judge sees what a ranker sees."""
        path = RECORD_DIR / f"{card['id']}.json"
        record = json.loads(path.read_text())["record"] if path.exists() else None
        return {**card, "record": record}

    def state(self):
        done = set(self.answered)
        upcoming = next((pair for pair in self.pairs if pair["id"] not in done), None)
        if upcoming:
            upcoming = {
                **upcoming,
                "a": self.with_record(upcoming["a"]),
                "b": self.with_record(upcoming["b"]),
            }
        return {
            "total": len(self.pairs),
            "done": len(done),
            "pair": upcoming,
            "can_undo": bool(self.answered),
        }


def make_handler(session, origins):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status=200):
            self._send(status, json.dumps(payload).encode(), "application/json")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(session.state())
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            # Only this page may record answers, not another site open in the browser.
            origin = self.headers.get("Origin")
            if origin is not None and origin not in origins:
                return self._json({"error": "wrong origin"}, 403)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "body is not JSON"}, 400)
            if self.path == "/api/answer":
                pair_id, choice = body.get("pair_id"), body.get("choice")
                if pair_id not in session.by_id or choice not in CHOICES:
                    return self._json({"error": "unknown pair or choice"}, 400)
                if pair_id in session.answered:
                    return self._json({"error": "already answered; undo first"}, 409)
                session.answer(pair_id, choice)
            elif self.path == "/api/undo":
                session.undo()
            else:
                return self._json({"error": "not found"}, 404)
            self._json(session.state())

        def log_message(self, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--pairs", default=str(ROOT / "private" / "pairs.json"))
    parser.add_argument("--judgments", default=str(ROOT / "private" / "judgments.jsonl"))
    args = parser.parse_args()

    pairs_path = Path(args.pairs)
    if not pairs_path.exists():
        raise SystemExit(f"{pairs_path} not found. Run compare/make_pairs.py first.")
    session = Session(pairs_path, Path(args.judgments))
    origins = {f"http://127.0.0.1:{args.port}", f"http://localhost:{args.port}"}
    # One request at a time, so two answers can never interleave in the log.
    server = HTTPServer(("127.0.0.1", args.port), make_handler(session, origins))
    state = session.state()
    print(f"{state['done']} of {state['total']} pairs answered")
    print(f"open http://127.0.0.1:{args.port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
