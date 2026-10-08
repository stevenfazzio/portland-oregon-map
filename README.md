# Portland, Oregon

An interactive map of Portland with its own idea of what deserves a label.

Most maps decide which places to name at each zoom level by general prominence, so
supermarkets and fast food win. This one scores every feature by a set of category
weights, then lets the same label-collision machinery do the rest: the best-scoring
feature in any patch of screen gets its name, the next gets a dot, the rest wait for
a closer zoom. A month control changes the scores, so fruit trees and wild berry
patches come forward when they are in season.

## Layout

    pipeline/   Python scripts that fetch the data and build the feature file
    site/       The static page: index.html, map.js, style.css
    site/data/  What the build writes for the page to load (not committed)
    compare/    A local page for judging pairs of places, used to tune the scoring
    data/raw/   Raw fetches (not committed)
    gh-pages    A branch holding only the built site, as served by GitHub Pages
    private/    Pairs to judge and the answers given (not committed)

## Build

Requires [uv](https://docs.astral.sh/uv/), which installs the one dependency.

    uv run pipeline/fetch_osm.py        # named places from OpenStreetMap
    uv run pipeline/fetch_trees.py      # fruit and nut street trees from the city
    uv run pipeline/fetch_berries.py    # sightings of wild berries from iNaturalist
    uv run pipeline/enrich.py           # optional, paid: a short record for each place
    uv run pipeline/rank.py             # optional, paid: a fit score for each record
    uv run pipeline/build_features.py   # score everything, write site/data/

The page loads its features from `site/data/`, which is not in the repository, so
the map shows nothing until the last step has run. The three fetches are free.

The fetch scripts keep an existing raw file; pass `--refresh` to fetch again. The
public OpenStreetMap query servers fail often. `fetch_osm.py` saves each tag key as
it arrives, so rerunning it resumes instead of starting over.

### Place records

`enrich.py` gives the independent places in the favoured categories a short factual
record: what kind of place it is, one sentence about it, what it is known for, and
features such as live music or a patio. It reads each place's own website and
searches for other pages that mention it (both through [Exa](https://exa.ai), key
in `EXA_API_KEY`), then has a Claude model write the record from that text alone
(key in `ANTHROPIC_API_KEY`). A record is shown on the map when the place's own
website supports it, or when another site is clearly writing about that place.
`--ids FILE` covers just the places listed in a file, whatever their category.

Every paid call is logged to `private/spend.jsonl`, and each step checks the total
against the ceiling in `pipeline/budget.py` before it starts. `uv run
pipeline/budget.py` prints the total. `pipeline/pilot.py` runs the same steps on a
small sample with several models side by side, to compare them before a full run.

### Ranking

`rank.py` has a model read each record and score from 0 to 100 how well the place
fits the person the map is made for. That description is private: it is read from
`private/profile.md`, which is not in this repository, and only the scores reach
the map. Chain locations are scored from the brand name alone. Without the scores
the map still builds, ranking by category weight.

A place scoring at least `top_fit` (in `pipeline/categories.toml`) is a top pick:
its name is bold and its dot larger, so that it still stands out once the map is
zoomed in far enough for everything around it to show.

`private/corrections.toml`, if it exists, overrides the result by hand: a place
named there can be given a score, a different category, or a different label.
`build_features.py` documents the format.

### Scoring

A place with a fit score is ranked by it; every other place is ranked by its
category's weight. Scoring is configured in two files:

- `pipeline/categories.toml` sorts places into categories and weights them.
- `pipeline/edibles.toml` lists the tree species and the wild berries, and when
  each is usually ripe. Those months are approximate.

Trees and berry patches are ranked month by month, so each competes only with
what is ripe at the same time, and the rarer or better-recorded one wins. They
are spaced four times as far apart as places and start two zoom levels later,
so that they do not crowd the places out. On the
map a filled green dot is in season in the chosen month; a green ring is not, and
waits for the closest zoom.

### Berry patches

Wild berries come from [iNaturalist](https://www.inaturalist.org) sightings. Only
research-grade ones are used (at least two people agree on the species), and only
those placed to within 100 m at their true position. Sightings of one species
within 75 m of each other are grouped into a patch. A patch appears earlier the
more people have recorded the plant there and the more worth picking the species
is; a plant recorded once waits for a close zoom.

What this cannot tell you:

- Sightings follow where observers walk, so parks with popular trails are
  over-represented and brambles on waste ground are barely recorded.
- A patch marks where the plant was recorded, in any season. It does not promise
  fruit, and the plant may since have been cleared.
- Whether you may pick there. Check the rules of whoever manages the land.

## View

    cd site && python3 -m http.server 8765 --bind 127.0.0.1

Then open <http://127.0.0.1:8765/>.

## Publish

    uv run pipeline/publish.py --dry-run   # list what would be published
    uv run pipeline/publish.py             # push site/ to the gh-pages branch

The built data is not kept on `main`, so the site is published from a separate
`gh-pages` branch. Each publish replaces that branch with a single commit of the
current `site/` folder; it has no history worth keeping.

## Tune

Category weights cannot say which of two bakeries is better, so the ordering is
checked against side-by-side judgments.

    uv run compare/make_pairs.py   # draw pairs into private/pairs.json
    uv run compare/serve.py        # judge them at http://127.0.0.1:8766/
    uv run compare/evaluate.py private/fit --categories   # how often the scores agree

Each answer is appended to `private/judgments.jsonl` as it is given, so the page
can be closed and reopened at any point.

## Data

- Places: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
- Trees: [City of Portland street tree inventory](https://www.portland.gov/trees).
- Berries: [iNaturalist](https://www.inaturalist.org) observations that their
  observers licensed CC0, CC BY or CC BY-NC. Each patch links to the sightings
  behind it, where every observer is credited.
- Place descriptions: written by a language model from each place's own website
  and from other sites that write about it. They can be wrong or out of date.
- Opening hours: as recorded in OpenStreetMap, with the month they were last
  confirmed where a mapper noted one. Many have no such date and may be stale.
- Basemap: [OpenFreeMap](https://openfreemap.org), © OpenMapTiles, data from OpenStreetMap.
