"""Stage 3: publish the built site to the gh-pages branch, which GitHub Pages serves.

The built data is not kept on main: it is large and replaced whole on every
build. Each publish replaces the gh-pages branch with a single commit holding
the current site/ folder, so that branch never accumulates history.

    uv run pipeline/publish.py --dry-run   # assemble it and stop before pushing
    uv run pipeline/publish.py             # push it to origin's gh-pages branch

Reads   site/
Writes  the gh-pages branch of the `origin` remote (force-pushed)
"""

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from common import ROOT, SITE_DATA_DIR

SITE_DIR = ROOT / "site"
BRANCH = "gh-pages"  # the only branch this script ever pushes to


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="assemble the site but do not push")
    args = parser.parse_args()

    meta_path = SITE_DATA_DIR / "meta.json"
    if not meta_path.exists() or not (SITE_DATA_DIR / "features.geojson").exists():
        raise SystemExit("site/data/ is missing: run pipeline/build_features.py first.")
    built_at = json.loads(meta_path.read_text())["built_at"]

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "site"
        shutil.copytree(SITE_DIR, out, ignore=shutil.ignore_patterns(".DS_Store", "*.tmp"))
        # Without this GitHub runs the folder through Jekyll, which the page does not need.
        (out / ".nojekyll").touch()
        files = sorted(p for p in out.rglob("*") if p.is_file())
        for path in files:
            print(f"  {path.stat().st_size / 1e6:8.2f} MB  {path.relative_to(out)}")
        if args.dry_run:
            print(f"publish: {len(files)} files assembled (built {built_at}); nothing pushed")
            return

        remote = git("remote", "get-url", "origin", cwd=ROOT)
        git("init", "--quiet", "--initial-branch", BRANCH, cwd=out)
        # The commit carries the same author as the repository's own commits.
        for key in ("user.name", "user.email"):
            git("config", key, git("config", key, cwd=ROOT), cwd=out)
        git("add", "--all", cwd=out)
        git("commit", "--quiet", "--message", f"Publish the site built {built_at}", cwd=out)
        git("push", "--force", "--quiet", remote, f"{BRANCH}:{BRANCH}", cwd=out)
        print(f"publish: pushed {len(files)} files to {BRANCH} on {remote}")


if __name__ == "__main__":
    main()
