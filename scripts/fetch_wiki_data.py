#!/usr/bin/env python3
"""
Pulls live monster facts (combat level, hitpoints, weakness, aggressive,
poisonous, slayer level, etc.) from the OSRS Wiki and writes them to
data/monster-facts.json.

Why this runs here and not in the browser:
The OSRS Wiki's API does not send Access-Control-Allow-Origin headers on
most endpoints, so a GitHub Pages site can't reliably call it live from a
visitor's browser (CORS blocks it). Running the fetch here -- locally, or
on a schedule via the GitHub Action in .github/workflows/update-data.yml --
sidesteps that entirely: the site just loads the resulting static JSON file,
which is a same-origin request and always works.

Usage:
    pip install requests
    python scripts/fetch_wiki_data.py

Notes on accuracy:
This parses the "Infobox Monster" template straight out of each wiki page's
wikitext. Field names (combat, hitpoints, max hit, attack style, attribute,
aggressive, poisonous, slaylvl, slayxp, assignedby, attack speed) match the
template as it's commonly used across monster pages, but the wiki is
community-edited, so a handful of pages may use slightly different or extra
fields. Run with --debug on one monster if something looks off, and check
data/monster-facts.json's "warnings" list after each run.
"""

import json
import re
import sys
import time
import argparse
from pathlib import Path

try:
    import requests
except ImportError:
    print("This script needs the 'requests' package: pip install requests")
    sys.exit(1)

API_URL = "https://oldschool.runescape.wiki/api.php"
# The wiki asks bots/tools to identify themselves with a descriptive UA
# and contact method -- be a good API citizen.
HEADERS = {
    "User-Agent": "Cairn-SlayerLoadoutPlanner/1.0 (github pages project; contact: set-your-contact-here)"
}
REQUEST_DELAY_SECONDS = 1.2  # be gentle with the wiki's servers

ROOT = Path(__file__).resolve().parent.parent
GEAR_PATH = ROOT / "data" / "gear.json"
OUT_PATH = ROOT / "data" / "monster-facts.json"

FIELD_MAP = {
    "combat": "combatLevel",
    "hitpoints": "hitpoints",
    "max hit": "maxHit",
    "attack style": "attackStyle",
    "attributes": "attribute",
    "elementalweaknesstype": "elementalWeaknessType",
    "elementalweaknesspercent": "elementalWeaknessPercent",
    "aggressive": "aggressive",
    "poisonous": "poisonous",
    "attack speed": "attackSpeed",
    "slaylvl": "slayerLevel",
    "slayxp": "slayerXp",
    "assignedby": "assignedBy",
    "cat": "category",
    "members": "membersOnly",
    "release": "releaseDate",
}


def fetch_wikitext(title):
    params = {
        "action": "query",
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "titles": title,
        "format": "json",
        "formatversion": "2",
        "redirects": "1",
    }
    r = requests.get(API_URL, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None
    revs = pages[0].get("revisions")
    if not revs:
        return None
    return revs[0]["slots"]["main"]["content"]


def strip_wikilinks(value):
    # "[[Fire giant]]" -> "Fire giant", "[[Fire giant|giants]]" -> "giants"
    return re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", value).strip()


def parse_infobox(wikitext, title):
    # Grab the first {{Infobox Monster ... }} block (balanced braces).
    m = re.search(r"\{\{Infobox [Mm]onster", wikitext)
    if not m:
        return None
    start = m.start()
    depth = 0
    i = start
    end = None
    while i < len(wikitext):
        if wikitext[i:i + 2] == "{{":
            depth += 1
            i += 2
            continue
        if wikitext[i:i + 2] == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                end = i
                break
            continue
        i += 1
    if end is None:
        return None
    block = wikitext[start:end]

    # Split on lines starting with '|' at depth 0 relative to the template.
    fields = {}
    current_key = None
    current_val = []
    for line in block.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|") and "=" in stripped:
            if current_key is not None:
                fields[current_key] = " ".join(current_val).strip()
            key, _, val = stripped[1:].partition("=")
            current_key = key.strip().lower()
            current_val = [val.strip()]
        elif current_key is not None:
            current_val.append(stripped)
    if current_key is not None:
        fields[current_key] = " ".join(current_val).strip()

    # Infobox Monster commonly has versioned params like "combat1", "combat2"
    # for multi-variant pages (e.g. different fire giant levels). Collect
    # the base field plus any numbered variants.
    result = {}
    for wiki_field, out_key in FIELD_MAP.items():
        variants = []
        if wiki_field in fields and fields[wiki_field]:
            variants.append(fields[wiki_field])
        n = 1
        while f"{wiki_field}{n}" in fields:
            if fields[f"{wiki_field}{n}"]:
                variants.append(fields[f"{wiki_field}{n}"])
            n += 1
        if variants:
            variants = [strip_wikilinks(v) for v in variants]
            # de-duplicate while preserving order
            seen = []
            for v in variants:
                if v not in seen:
                    seen.append(v)
            result[out_key] = seen[0] if len(seen) == 1 else seen

    result["sourceUrl"] = f"https://oldschool.runescape.wiki/w/{title.replace(' ', '_')}"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", help="print raw parsed fields for one monster id and exit")
    args = parser.parse_args()

    gear = json.loads(GEAR_PATH.read_text())

    if args.debug:
        target = next((g for g in gear if g["id"] == args.debug), None)
        if not target or not target.get("wikiTitle"):
            print(f"No wikiTitle mapped for id '{args.debug}'")
            return
        wikitext = fetch_wikitext(target["wikiTitle"])
        if wikitext is None:
            print("Page not found.")
            return
        parsed = parse_infobox(wikitext, target["wikiTitle"])
        print(json.dumps(parsed, indent=2))
        return

    facts = {}
    warnings = []
    total = len([g for g in gear if g.get("wikiTitle")])
    done = 0

    for entry in gear:
        title = entry.get("wikiTitle")
        if not title:
            continue
        done += 1
        print(f"[{done}/{total}] Fetching '{title}' for id '{entry['id']}'...")
        try:
            wikitext = fetch_wikitext(title)
            if wikitext is None:
                warnings.append(f"'{title}' (id: {entry['id']}): page not found on wiki")
                continue
            parsed = parse_infobox(wikitext, title)
            if parsed is None:
                warnings.append(f"'{title}' (id: {entry['id']}): no Infobox Monster template found")
                continue
            facts[entry["id"]] = parsed
        except requests.RequestException as e:
            warnings.append(f"'{title}' (id: {entry['id']}): request failed ({e})")
        time.sleep(REQUEST_DELAY_SECONDS)

    output = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "https://oldschool.runescape.wiki",
        "monsters": facts,
        "warnings": warnings,
    }
    OUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"\nWrote {len(facts)} monster records to {OUT_PATH}")
    if warnings:
        print(f"{len(warnings)} warning(s) -- see the 'warnings' array in the output file.")


if __name__ == "__main__":
    main()
