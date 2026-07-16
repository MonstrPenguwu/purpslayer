#!/usr/bin/env python3
"""
Pulls the "Location comparisons" table from each monster's OSRS Wiki
Slayer task page (e.g. https://oldschool.runescape.wiki/w/Slayer_task/Fire_giants)
and writes it to data/task-locations.json.

Why this is separate from fetch_wiki_data.py:
That script reads the {{Infobox Monster}} template on the monster's own
page (combat level, hitpoints, etc). This script reads a different page
-- "Slayer task/<Task name>" -- which carries a hand-maintained wikitable
listing every place the monster can be killed on that task, with columns
for spawn amount, multicombat, cannon usability, safespots, and notes.
Not every task has one of these pages; where none exists the site falls
back to the curated location info already in data/gear.json.

Usage:
    pip install requests
    python scripts/fetch_task_locations.py
    python scripts/fetch_task_locations.py --debug firegiants
    python scripts/fetch_task_locations.py --resolve   # (re)discover slayerTaskTitle mappings
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_wiki_data import fetch_wikitext, strip_wikilinks, API_URL, HEADERS, REQUEST_DELAY_SECONDS

ROOT = Path(__file__).resolve().parent.parent
GEAR_PATH = ROOT / "data" / "gear.json"
OUT_PATH = ROOT / "data" / "task-locations.json"


def opensearch_candidate(query):
    r = requests.get(API_URL, params={
        "action": "opensearch", "search": query, "limit": 5, "format": "json"
    }, headers=HEADERS, timeout=20)
    r.raise_for_status()
    hits = r.json()[1]
    for h in hits:
        if h.lower().startswith("slayer task/"):
            return h
    return None


def resolve_task_title(entry):
    """Find the correct 'Slayer task/X' wiki page title for a gear.json entry."""
    name = entry["name"]
    # Strip parenthetical qualifiers and slashes, e.g. "Hydras (Alchemical/regular)" -> "Hydras"
    base = re.sub(r"\s*\([^)]*\)", "", name).strip()
    base = base.split("/")[0].strip()

    candidates = [f"Slayer task/{base}"]
    if entry.get("wikiTitle"):
        candidates.append(f"Slayer task/{entry['wikiTitle']}")

    for title in candidates:
        wt = fetch_wikitext(title)
        time.sleep(REQUEST_DELAY_SECONDS)
        if wt and "{{Infobox Slayer" in wt:
            return title, wt

    for query in [f"Slayer task/{base}", f"Slayer task/{entry.get('wikiTitle') or base}"]:
        found = opensearch_candidate(query)
        time.sleep(REQUEST_DELAY_SECONDS)
        if found:
            wt = fetch_wikitext(found)
            time.sleep(REQUEST_DELAY_SECONDS)
            if wt and "{{Infobox Slayer" in wt:
                return found, wt

    return None, None


YES_NO_RE = re.compile(r"\{\{\s*(Yes|No)[^}]*\}\}", re.IGNORECASE)


def yes_no(cell):
    m = YES_NO_RE.search(cell)
    if not m:
        return None
    return m.group(1).lower() == "yes"


def strip_remaining_templates(text):
    """Remove any leftover {{...}} templates we don't specifically handle
    (e.g. nested price-lookup templates like {{Coins|{{GEP|Item}}*5000}}),
    stripping innermost-first so nesting doesn't break the regex."""
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    return text


def clean_wiki_markup(text):
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"\{\{FloorNumber\|uk=(\d+)\}\}", r"floor \1", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{[Ff]airycode\|([^}]+)\}\}", lambda m: m.group(1).upper(), text)
    text = strip_wikilinks(text)
    text = strip_remaining_templates(text)
    text = re.sub(r"'''(.*?)'''", r"\1", text)  # bold
    text = re.sub(r"''(.*?)''", r"\1", text)    # italic
    text = re.sub(r"\(\s*(GE|ge)\s*:\s*\)", "", text)  # empty GE-price leftovers
    text = re.sub(r"\(\s*\)", "", text)  # any other now-empty parentheticals
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:])", r"\1", text)  # stray space left before punctuation
    return text


def clean_location_name(cell):
    return clean_wiki_markup(cell).strip(" /")


def clean_amount(cell):
    text = re.sub(r"<br\s*/?>", " / ", cell, flags=re.IGNORECASE)
    text = re.sub(r"\{\{nowrap\|([^}]*)\}\}", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{NA\|([^}]*)\}\}", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{FloorNumber\|uk=(\d+)\}\}", r"floor \1", text, flags=re.IGNORECASE)
    text = strip_wikilinks(text)
    text = strip_remaining_templates(text)
    text = re.sub(r"\s+", " ", text).strip(" /")
    return text


def clean_notes(cell):
    notes = []
    for line in cell.split("\n"):
        line = line.strip()
        if line.startswith("*"):
            line = line.lstrip("*").strip()
            line = re.sub(r"'''(.*?)'''", r"\1", line)
            line = clean_wiki_markup(line)
            if line:
                notes.append(line)
    if notes:
        return notes
    # some pages write a single prose sentence instead of a bulleted list
    prose = re.sub(r"'''(.*?)'''", r"\1", " ".join(cell.split("\n")))
    prose = clean_wiki_markup(prose)
    return [prose] if prose else []


MEJRS_BASE = "https://mejrs.github.io/osrs"


def parse_maplink_coords(cell_text):
    """Extract an (x, y, plane, mapId) point from a {{Map|type=maplink|...}}
    cell so we can deep-link to the community map viewer at mejrs.github.io/osrs
    (the OSRS Wiki's own interactive map is a Kartographer JS widget with no
    linkable URL of its own -- see RuneScape:Create Map / User:Mejrs docs).
    Coordinate lists show up in the wild in three different forms, so try
    each in turn and just take the first point."""
    mapid_m = re.search(r"mapid\s*=\s*(-?\d+)", cell_text, re.IGNORECASE)
    plane_m = re.search(r"\bplane\s*=\s*(-?\d+)", cell_text, re.IGNORECASE)
    mapid = mapid_m.group(1) if mapid_m else "-1"
    plane = plane_m.group(1) if plane_m else "0"

    # form 1: "x:1234,y:5678" (colon-labelled pair)
    m = re.search(r"x:(\d+)\s*,\s*y:(\d+)", cell_text, re.IGNORECASE)
    if m:
        return {"x": int(m.group(1)), "y": int(m.group(2)), "plane": int(plane), "mapId": int(mapid)}

    # form 2: "|x=1234|y=5678" -- key=value pair, same or separate lines
    xm = re.search(r"\bx\s*=\s*(\d+)", cell_text, re.IGNORECASE)
    ym = re.search(r"\by\s*=\s*(\d+)", cell_text, re.IGNORECASE)
    if xm and ym:
        return {"x": int(xm.group(1)), "y": int(ym.group(1)), "plane": int(plane), "mapId": int(mapid)}

    # form 3: bare "1451,9900" anonymous coordinate pairs (no space after comma,
    # to avoid matching prose like "levels = 104, 109")
    m = re.search(r"(?<!\d)(\d{2,6}),(\d{2,6})(?!\d)", cell_text)
    if m:
        return {"x": int(m.group(1)), "y": int(m.group(2)), "plane": int(plane), "mapId": int(mapid)}

    return None


def build_map_url(point):
    if not point:
        return None
    params = f"x={point['x']}&y={point['y']}&p={point['plane']}&z=4"
    if point.get("mapId") not in (None, -1):
        params += f"&m={point['mapId']}"
    return f"{MEJRS_BASE}?{params}"


def split_table_rows(table_body):
    rows = [r for r in table_body.split("|-") if r.strip()]
    if rows:
        # the last row's text runs up to the table's own closing "|}" marker,
        # which would otherwise leak into that row's last cell as content
        rows[-1] = re.sub(r"\|\}\s*$", "", rows[-1])
    return rows


def split_row_cells(row_text):
    lines = row_text.split("\n")
    cells = []
    current = []
    depth = 0
    for line in lines:
        if depth == 0 and line.startswith("|") and not line.startswith("|}"):
            if current:
                cells.append("\n".join(current))
            current = [line[1:]]
        else:
            current.append(line)
        depth += line.count("{{") - line.count("}}")
    if current:
        cells.append("\n".join(current))
    # row_text starts right after the "|-" row delimiter's newline, so the
    # first split segment is always an empty pre-cell artifact -- drop just
    # that one, but keep any genuinely empty cells further in the row (e.g.
    # a blank Maplink column) so column positions stay aligned.
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    return cells


def find_balanced_table(wikitext, start):
    """start points at the '{|' of a wikitable; return the text up to its matching '|}'."""
    i = start
    depth = 0
    while i < len(wikitext):
        if wikitext[i:i + 2] == "{|":
            depth += 1
            i += 2
            continue
        if wikitext[i:i + 2] == "|}":
            depth -= 1
            i += 2
            if depth == 0:
                return wikitext[start:i]
            continue
        i += 1
    return wikitext[start:]


def parse_location_table(wikitext):
    m = re.search(r"=+\s*Locations?(\s*comparisons?)?\s*=+", wikitext, re.IGNORECASE)
    if not m:
        return None
    rest = wikitext[m.end():]
    tbl_m = re.search(r"\{\|", rest)
    if not tbl_m:
        return None
    table_text = find_balanced_table(rest, tbl_m.start())

    # header row is everything before the first "|-"; drop it
    body = table_text.split("|-", 1)[1] if "|-" in table_text else ""
    rows = split_table_rows(body)

    locations = []
    for row in rows:
        cells = split_row_cells(row)
        if len(cells) < 3:
            continue
        loc_name = clean_location_name(cells[0])
        if not loc_name:
            continue
        # cells layout: [Location, Maplink, Amount, Multicombat, Cannonable, Safespottable, Notes]
        # some pages omit Maplink or Notes -- work from the back for the boolean trio when possible.
        amount = clean_amount(cells[2]) if len(cells) > 2 else None
        multicombat = yes_no(cells[3]) if len(cells) > 3 else None
        cannonable = yes_no(cells[4]) if len(cells) > 4 else None
        safespottable = yes_no(cells[5]) if len(cells) > 5 else None
        notes = clean_notes(cells[6]) if len(cells) > 6 else []
        point = parse_maplink_coords(cells[1]) if len(cells) > 1 else None
        locations.append({
            "location": loc_name,
            "amount": amount,
            "multicombat": multicombat,
            "cannonable": cannonable,
            "safespottable": safespottable,
            "notes": notes,
            "mapUrl": build_map_url(point),
        })
    return locations if locations else None


def parse_getting_there(wikitext):
    """Fallback for single-location task pages that skip the comparison table
    and instead write a prose '==Getting there==' section with a bullet list
    of teleport methods."""
    m = re.search(r"=+\s*(Getting there|Transportation)\s*=+", wikitext, re.IGNORECASE)
    if not m:
        return None
    rest = wikitext[m.end():]
    next_heading = re.search(r"^=+[^=\n]+=+$", rest, re.MULTILINE)
    section = rest[:next_heading.start()] if next_heading else rest[:1500]
    methods = []
    for line in section.split("\n"):
        line = line.strip()
        if line.startswith("*"):
            line = line.lstrip("*").strip()
            line = re.sub(r"'''(.*?)'''", r"\1", line)
            line = clean_wiki_markup(line)
            if line:
                methods.append(line)
    return methods if methods else None


SCP_RE = re.compile(r"\{\{SCP\|[^|]+\|(\d+)\}\}")


def parse_infobox_slayer(wikitext):
    m = re.search(r"\{\{Infobox Slayer", wikitext)
    if not m:
        return {}
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
    block = wikitext[start:end] if end else wikitext[start:start + 800]

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

    def clean_req(raw):
        if not raw:
            return None
        text = raw
        # {{SCP|Skill|Level}} -> "Level Skill", e.g. "85 Slayer"
        text = re.sub(r"\{\{SCP\|([^|}]+)\|([^|}]+)\}\}", r"\2 \1", text)
        text = strip_wikilinks(text)
        text = strip_remaining_templates(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text if text and text.lower() != "none" else None

    result = {}
    combatreq = fields.get("combatreq", "")
    scp = SCP_RE.search(combatreq)
    result["combatLevelReq"] = scp.group(1) if scp else clean_req(combatreq)
    result["skillReq"] = clean_req(fields.get("skillreq", ""))
    result["otherReq"] = clean_req(fields.get("otherreq", ""))

    masters = {}
    for master in ["turael", "spria", "mazchna", "vannaka", "chaeldar", "nieve", "steve",
                   "konar", "duradel", "krystilia", "aya"]:
        if fields.get(master):
            masters[master] = strip_wikilinks(fields[master])
    result["assignedAmounts"] = masters
    return result


LOCATION_ACCESS_PATH = ROOT / "data" / "location-access.json"


def base_location_title(name):
    """"Slayer Tower (2nd floor)" -> "Slayer Tower" -- strips a trailing
    parenthetical qualifier so we fetch the actual wiki page for the place,
    not a floor/room variant of it."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


def build_location_access(facts):
    """For every distinct location named in the location-comparison tables,
    fetch that location's own wiki page and pull its '==Transportation==' /
    '==Getting there==' bullet list -- these dungeon/area pages consistently
    document teleports, fairy rings, and walking routes to the place itself,
    which the per-monster Slayer task page doesn't repeat."""
    raw_names = set()
    for task in facts.values():
        for loc in task.get("locations", []):
            raw_names.add(loc["location"])

    access = {}
    fetched_by_title = {}
    sorted_names = sorted(raw_names)
    for i, raw_name in enumerate(sorted_names, 1):
        base = base_location_title(raw_name)
        if base not in fetched_by_title:
            print(f"[{i}/{len(sorted_names)}] Fetching location page '{base}'...")
            try:
                wt = fetch_wikitext(base)
            except requests.RequestException as e:
                print(f"  (skipping -- request failed: {e})")
                wt = None
            fetched_by_title[base] = parse_getting_there(wt) if wt else None
            time.sleep(REQUEST_DELAY_SECONDS)
        methods = fetched_by_title[base]
        if methods:
            access[raw_name] = {
                "sourceUrl": f"https://oldschool.runescape.wiki/w/{base.replace(' ', '_')}",
                "methods": methods,
            }
    return access


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", help="print parsed table for one monster id and exit")
    ap.add_argument("--resolve", action="store_true", help="(re)discover slayerTaskTitle for entries missing one")
    args = ap.parse_args()

    gear = json.loads(GEAR_PATH.read_text(encoding="utf-8"))

    if args.debug:
        target = next((g for g in gear if g["id"] == args.debug), None)
        if not target:
            print(f"No entry with id '{args.debug}'")
            return
        title = target.get("slayerTaskTitle")
        if not title:
            title, wt = resolve_task_title(target)
        else:
            wt = fetch_wikitext(title)
        if not wt:
            print(f"No Slayer task page found for '{target['id']}'")
            return
        print(f"Resolved title: {title}")
        print(json.dumps(parse_infobox_slayer(wt), indent=2))
        locs = parse_location_table(wt)
        print("locations:", json.dumps(locs, indent=2))
        if not locs:
            print("gettingThere:", json.dumps(parse_getting_there(wt), indent=2))
        return

    if args.resolve:
        changed = False
        for entry in gear:
            if entry.get("slayerTaskTitle"):
                continue
            title, wt = resolve_task_title(entry)
            entry["slayerTaskTitle"] = title
            changed = True
            print(f"{entry['id']:22s} -> {title}")
        if changed:
            GEAR_PATH.write_text(json.dumps(gear, indent=2) + "\n", encoding="utf-8")
        return

    facts = {}
    warnings = []
    mapped = [g for g in gear if g.get("slayerTaskTitle")]
    total = len(mapped)
    for i, entry in enumerate(mapped, 1):
        title = entry["slayerTaskTitle"]
        print(f"[{i}/{total}] Fetching '{title}' for id '{entry['id']}'...")
        try:
            wt = fetch_wikitext(title)
            if wt is None:
                warnings.append(f"'{title}' (id: {entry['id']}): page not found")
                continue
            locations = parse_location_table(wt)
            getting_there = None if locations else parse_getting_there(wt)
            if locations is None and getting_there is None:
                warnings.append(f"'{title}' (id: {entry['id']}): no location table or getting-there section found")
                continue
            record = {
                "sourceUrl": f"https://oldschool.runescape.wiki/w/{title.replace(' ', '_')}",
                "requirements": parse_infobox_slayer(wt),
            }
            if locations:
                record["locations"] = locations
            if getting_there:
                record["gettingThere"] = getting_there
            facts[entry["id"]] = record
        except requests.RequestException as e:
            warnings.append(f"'{title}' (id: {entry['id']}): request failed ({e})")
        time.sleep(REQUEST_DELAY_SECONDS)

    output = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "https://oldschool.runescape.wiki",
        "tasks": facts,
        "warnings": warnings,
    }
    OUT_PATH.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {len(facts)} task location records to {OUT_PATH}")
    if warnings:
        print(f"{len(warnings)} warning(s) -- see the 'warnings' array in the output file.")

    print("\nFetching per-location transportation info...")
    location_access = build_location_access(facts)
    LOCATION_ACCESS_PATH.write_text(json.dumps({
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "https://oldschool.runescape.wiki",
        "locations": location_access,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote transportation info for {len(location_access)} locations to {LOCATION_ACCESS_PATH}")


if __name__ == "__main__":
    main()
