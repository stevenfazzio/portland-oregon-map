"""Shared helpers for the pipeline scripts: paths, safe writes, HTTP with retry."""

import json
import math
import os
import random
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
SITE_DATA_DIR = ROOT / "site" / "data"

# City of Portland plus a margin: south, west, north, east.
BBOX = (45.43, -122.84, 45.65, -122.47)

USER_AGENT = "portland-oregon-map/0.1 (personal project)"
RETRY_STATUS = {429, 500, 502, 503, 504}


def write_json_safely(obj, output_path, *, min_items=1, items_key=None):
    """Write JSON so output_path only ever holds a complete, re-readable file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=output_path.parent, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
        with open(tmp_path) as f:
            back = json.load(f)
        if items_key is not None:
            n = len(back[items_key])
            assert n == len(obj[items_key]), f"wrote {len(obj[items_key])} items, read back {n}"
            assert n >= min_items, f"only {n} items; refusing to replace {output_path}"
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp_path, 0o666 & ~umask)
        os.replace(tmp_path, output_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def fetch_json(
    url, *, params=None, data=None, json_body=None, headers=None, timeout=120, max_retries=4
):
    """GET, or POST when data (form fields) or json_body is given, and parse JSON.

    Retries throttling and server faults with backoff; any other HTTP error is
    raised on the first response, since retrying a bad request only hides it.
    """
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    all_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    body = urllib.parse.urlencode(data).encode() if data else None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        all_headers["Content-Type"] = "application/json"
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=body, headers=all_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS or attempt == max_retries - 1:
                raise
            retry_after = e.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else min(2**attempt * 5, 60)
            print(f"  HTTP {e.code}, retrying in {wait:.0f}s")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == max_retries - 1:
                raise
            wait = min(2**attempt * 5, 60)
            print(f"  {type(e).__name__}: {e}, retrying in {wait:.0f}s")
        time.sleep(wait + random.uniform(0, wait * 0.1))
    raise RuntimeError(f"exhausted retries for {url}")


def mercator(lon, lat):
    """Web Mercator position as fractions of the world, both in [0, 1)."""
    x = (lon + 180.0) / 360.0
    s = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)
    return x, y
