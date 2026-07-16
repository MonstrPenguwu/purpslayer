#!/usr/bin/env python3
"""
Pulls the wiki-maintained "Recommended equipment" gear tables from each
monster's OSRS Wiki Slayer task page and writes them to
data/recommended-gear.json.

Why this exists: gear.json's melee/ranged/magic arrays are hand-curated
(by a human or a past Claude session) and go stale as new items release --
Avernic treads, the ancient rings (Ultor/Bellator/Venator/Magus), Masori,
and the moon armour sets were all missing until this was built. Roughly a
third of Slayer task pages carry a wiki-maintained {{Recommended equipment}}
template per combat style, tiered best-to-budget exactly like our own
gear.json schema -- so for those tasks we prefer this live data over the
curated fallback entirely. Tasks without the template keep using gear.json.

Usage:
    pip install requests
    python scripts/fetch_recommended_gear.py
    python scripts/fetch_recommended_gear.py --debug firegiants
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
from fetch_wiki_data import fetch_wikitext, strip_wikilinks, REQUEST_DELAY_SECONDS
from fetch_task_locations import strip_remaining_templates, clean_wiki_markup

ROOT = Path(__file__).resolve().parent.parent
GEAR_PATH = ROOT / "data" / "gear.json"
OUT_PATH = ROOT / "data" / "recommended-gear.json"

SLOT_MAP = {
    "head": "Head", "cape": "Cape", "neck": "Neck", "ammo": "Ammo",
    "weapon": "Weapon", "shield": "Shield", "body": "Body", "legs": "Legs",
    "hands": "Hands", "feet": "Feet", "ring": "Ring",
}
STYLE_MAP = {"melee": "melee", "ranged": "ranged", "magic": "magic"}

PLINK_RE = re.compile(r"\{\{[Pp]link\|([^|}]+)")
EFN_RE = re.compile(r"\{\{efn[^{}]*\}\}", re.IGNORECASE)
SLOT_KEY_RE = re.compile(r"^([a-z]+?)(\d+)$")


def find_balanced_template(wikitext, start):
    """start points at the '{{' opening a template; return its full text."""
    i = start
    depth = 0
    while i < len(wikitext):
        if wikitext[i:i + 2] == "{{":
            depth += 1
            i += 2
            continue
        if wikitext[i:i + 2] == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return wikitext[start:i]
            continue
        i += 1
    return wikitext[start:]


def parse_template_fields(block):
    """Generic {{Template|key = value|key2 = value2}} parser (multi-line
    values supported), same pattern used across the other fetch scripts."""
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
        elif current_key is not None and not re.match(r"^\}+$", stripped):
            current_val.append(stripped)
    if current_key is not None:
        fields[current_key] = " ".join(current_val).strip()
    return fields


def parse_item_list(raw_value):
    """'{{plink|A}}{{efn|...}} / {{plink|B}}' -> ['A', 'B']"""
    text = EFN_RE.sub("", raw_value)
    items = []
    for piece in text.split("/"):
        m = PLINK_RE.search(piece)
        if m:
            name = strip_wikilinks(m.group(1)).strip()
            name = name.split("#", 1)[0].strip()  # "God capes#Imbuing" -> "God capes"
            if name:
                items.append(name)
    return items


def parse_recommended_equipment_block(block):
    """One {{Recommended equipment|style=X|head1=...|...}} block ->
    (style, {SlotName: [item, item, ...]}) ordered best-to-worst."""
    fields = parse_template_fields(block)
    style_raw = strip_wikilinks(fields.get("style", "")).strip().lower()
    style = STYLE_MAP.get(style_raw)
    if not style:
        return None, None

    tiers = {}  # slot -> {tier_num: [items]}
    for key, val in fields.items():
        m = SLOT_KEY_RE.match(key)
        if not m:
            continue
        slot_raw, tier = m.group(1), int(m.group(2))
        slot = SLOT_MAP.get(slot_raw)
        if not slot:
            continue
        items = parse_item_list(val)
        if items:
            tiers.setdefault(slot, {})[tier] = items

    result = {}
    for slot, tier_map in tiers.items():
        ordered = []
        for tier in sorted(tier_map):
            for item in tier_map[tier]:
                if item not in ordered:
                    ordered.append(item)
        if ordered:
            result[slot] = " / ".join(ordered)
    return style, result


def parse_all_recommended_equipment(wikitext):
    """Find every {{Recommended equipment}} block on the page and merge
    them by style (a page can have more than one tabber, e.g. boss variants
    get their own set further down -- last one per style wins, since that's
    usually the more specific/complete version further down the page).

    Note: this deliberately does NOT scrape the wiki's freeform "Inventory"
    section that follows each block -- unlike the strictly-keyed equipment
    template, that section is loose prose bullets that bleed into unrelated
    page content (mechanic notes, other strategies, etc.), producing noisy
    junk rather than a usable item list. Inventory suggestions are handled
    client-side instead via a live item search against the wiki."""
    styles = {}
    idx = 0
    while True:
        idx = wikitext.find("{{Recommended equipment", idx)
        if idx == -1:
            break
        block = find_balanced_template(wikitext, idx)
        style, slots = parse_recommended_equipment_block(block)
        if style and slots:
            styles[style] = {"gear": [{"slot": s, "item": i} for s, i in slots.items()]}
        idx += len(block)
    return styles if styles else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", help="print parsed gear for one monster id and exit")
    args = ap.parse_args()

    gear = json.loads(GEAR_PATH.read_text(encoding="utf-8"))

    if args.debug:
        target = next((g for g in gear if g["id"] == args.debug), None)
        if not target or not target.get("slayerTaskTitle"):
            print(f"No slayerTaskTitle mapped for id '{args.debug}'")
            return
        wt = fetch_wikitext(target["slayerTaskTitle"])
        print(json.dumps(parse_all_recommended_equipment(wt), indent=2))
        return

    facts = {}
    warnings = []
    mapped = [g for g in gear if g.get("slayerTaskTitle")]
    total = len(mapped)
    for i, entry in enumerate(mapped, 1):
        title = entry["slayerTaskTitle"]
        print(f"[{i}/{total}] Checking '{title}' for id '{entry['id']}'...")
        try:
            wt = fetch_wikitext(title)
            if wt is None:
                warnings.append(f"'{title}' (id: {entry['id']}): page not found")
                continue
            styles = parse_all_recommended_equipment(wt)
            if styles is None:
                continue  # no Recommended equipment template on this page -- not a warning, just not present
            facts[entry["id"]] = {
                "sourceUrl": f"https://oldschool.runescape.wiki/w/{title.replace(' ', '_')}",
                **styles,
            }
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
    print(f"\nWrote recommended gear for {len(facts)} tasks to {OUT_PATH}")
    if warnings:
        print(f"{len(warnings)} warning(s) -- see the 'warnings' array in the output file.")


if __name__ == "__main__":
    main()
