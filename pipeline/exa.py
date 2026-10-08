"""Thin wrappers over the two Exa endpoints the pipeline uses.

Exa fetches and cleans web pages, and searches the web for pages about a
place. Each call records its cost in the spend ledger, using the figure Exa
reports in the response. The key is read from EXA_API_KEY.
"""

import os

import budget
from common import fetch_json

API = "https://api.exa.ai"


def _post(path, body, what):
    response = fetch_json(
        API + path,
        json_body=body,
        headers={"x-api-key": os.environ["EXA_API_KEY"]},
        timeout=90,
    )
    cost = (response.get("costDollars") or {}).get("total")
    if cost is None:
        raise RuntimeError(f"Exa {path} returned no cost; refusing to spend blind")
    budget.record("exa", what, cost)
    return response


def contents(urls, *, max_chars, subpages=0, subpage_target=None, summary=None, what="contents"):
    """Fetch page text for up to 100 URLs. Failures are listed in the response's `statuses`."""
    assert len(urls) <= 100, "Exa accepts at most 100 URLs per request"
    body = {"urls": urls, "text": {"maxCharacters": max_chars}}
    if subpages:
        body["subpages"] = subpages
        body["subpageTarget"] = subpage_target or []
    if summary:
        body["summary"] = summary
    return _post("/contents", body, what)


def search(query, *, kind, num_results, highlight_query, exclude_domains=None, what="search"):
    """Search for pages matching the query, returning the most relevant passages of each."""
    body = {
        "query": query,
        "type": kind,
        "numResults": num_results,
        "userLocation": "US",
        "contents": {"highlights": {"query": highlight_query, "maxCharacters": 700}},
    }
    if exclude_domains:
        body["excludeDomains"] = exclude_domains
    return _post("/search", body, what)
