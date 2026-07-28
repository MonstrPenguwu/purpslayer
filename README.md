# PurpSlayer — Slayer Loadout Planner

A static gear-recommendation tool for OSRS Slayer tasks. Pick a monster, tick
the gear you own per equipment slot, save it as a named loadout, and pull it
back up next time you get the same task.

## How the data works

Four JSON files back the site, in two categories:

**Hand-curated (edit directly):**

- **`data/gear.json`** — per-slot gear recommendations (melee/ranged/mage) for
  each monster. Includes a `wikiTitle` mapping used by the fetch script.
- **`data/recommended-gear.json`** — per-task gear loadout suggestions scraped
  from wiki "Strategies" pages by `fetch_recommended_gear.py`. Structure:
  `{ tasks: { taskName: { melee: { gear: [...] }, ranged: {...}, ... } } }`.
  Edit freely; the script will overwrite on next run.

**Generated (do not hand-edit):**

- **`data/monster-facts.json`** — combat level, hitpoints, weakness, attack
  style, slayer level requirement. Pulled from the wiki Infobox by
  `scripts/fetch_wiki_data.py`.
- **`data/task-locations.json`** — location access requirements per task, pulled
  by `scripts/fetch_task_locations.py`.

The site loads all four at runtime. Missing entries fall back gracefully —
unknown monsters show static fallback values; missing recommended-gear entries
are silently skipped.

## Inventory search — live wiki API

The inventory input uses a live autocomplete backed by the OSRS Wiki's
`opensearch` endpoint:

```
https://oldschool.runescape.wiki/api.php?action=opensearch&search=<query>&limit=8&namespace=0&format=json&origin=*
```

The wiki sends `Access-Control-Allow-Origin: *` on this endpoint, so the
browser can hit it directly — no proxy or backend needed. Results are debounced
at 250ms and are never cached locally, so the item list is always current.

**Keyboard controls:** ArrowUp/Down to highlight, Enter to select the
highlighted item (or add the raw typed text if nothing is highlighted), Escape
to close.

**XSS note:** wiki results are the only external, runtime data the app inserts
into `innerHTML`. The `esc()` helper (defined at the top of the JS section)
HTML-escapes all wiki strings before insertion. All other data in the app is
static/curated JSON.

## Why most wiki data is still fetched server-side

The `opensearch` endpoint is the exception: it's explicitly CORS-enabled. Most
other wiki API endpoints and page-parsing approaches are not, so
`fetch_wiki_data.py` and `fetch_task_locations.py` run as scripts (locally or
via the included GitHub Action) and commit their output as plain JSON files that
the site loads same-origin.

## Setup

1. Push this repo to GitHub.
2. Repo Settings → Pages → Deploy from branch → `main` / root. Fully static,
   no build step.
3. To populate live wiki facts locally (run in this order):
   ```bash
   pip install requests
   python scripts/fetch_wiki_data.py
   python scripts/fetch_task_locations.py
   python scripts/fetch_weapon_tiers.py
   python scripts/fetch_recommended_gear.py
   ```
   `fetch_weapon_tiers.py` must run before `fetch_recommended_gear.py` — it
   queries the wiki's equipment cargo table to build crush/slash/stab weapon
   tier lists (`data/weapon-tiers.json`), which `fetch_recommended_gear.py`
   uses to fill in Weapon slots when the wiki's strategy page doesn't cover a
   combat style (e.g. trolls: wiki only has magic, so melee weapons are filled
   from the crush tier list based on the monster's `fallbackWeak`).
   Commit the resulting JSON files.
4. (Optional) `.github/workflows/update-data.yml` re-runs the scripts every
   Monday and auto-commits any changes. Enable Actions and it runs itself.

**Dev server** — any static file server works. During development this project
runs at `http://localhost:8753`.

## A note on accuracy

The fetch scripts parse wiki infobox templates from raw wikitext. Field names
(`combat`, `hitpoints`, `attack style`, `attribute`, `slaylvl`, etc.) match the
common template structure but can differ per page. Run a single-monster debug
pass before trusting a full run:

```bash
python scripts/fetch_wiki_data.py --debug firegiants
```

The `wikiTitle` mappings in `gear.json` (e.g. `"firegiants"` → `"Fire giant"`)
are best-guess. Several entries with multiple in-game variants (shades,
vampyres, Tzhaar, elves, generic dragons) are deliberately left unmapped.

Gear recommendations in `gear.json` and `recommended-gear.json` are curated
advice — treat them as a starting point and adjust freely.

## Project structure

```
index.html                            entire front end (HTML + CSS + JS, no build step)
data/gear.json                        hand-curated gear recommendations per monster
data/recommended-gear.json            strategy-page gear suggestions (fetch_recommended_gear.py)
data/monster-facts.json               generated combat facts (fetch_wiki_data.py)
data/task-locations.json              location access requirements (fetch_task_locations.py)
data/weapon-tiers.json                crush/slash/stab weapon tier lists (fetch_weapon_tiers.py)
scripts/fetch_wiki_data.py            pulls monster facts from the OSRS Wiki
scripts/fetch_task_locations.py       pulls location/access data from the OSRS Wiki
scripts/fetch_weapon_tiers.py         pulls weapon tier lists from wiki cargo DB (run before fetch_recommended_gear)
scripts/fetch_recommended_gear.py     pulls gear loadout suggestions; augments missing weapon slots from weapon-tiers
.github/workflows/update-data.yml     weekly auto-refresh via GitHub Actions
```

## Loadout persistence

Named loadouts are stored in `localStorage` under the key `purpslayer_loadouts` as
a JSON object keyed by loadout name. Inventory tags are stored per-loadout as
a plain string array. Clearing site data in the browser will erase all saved
loadouts.
