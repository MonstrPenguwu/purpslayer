#!/usr/bin/env python3
"""
Builds weapon tier lists (crush / slash / stab) and writes them to
data/weapon-tiers.json.

Strategy
--------
The OSRS Wiki's cargo endpoint is not publicly exposed, and the Crush/Slash/Stab
weapon pages use deeply nested templates ({{CombatStylesDisplay}}) whose content
can't be recovered from raw wikitext.  Instead this script derives tier lists
from two sources combined:

1. **Existing recommended-gear.json** — tasks that already have complete melee
   weapon recommendations (pulled from the wiki's {{Recommended equipment}}
   template) and a clear fallbackWeak in gear.json.  Because those weapon lists
   are themselves sourced from the wiki, they're accurate and community-maintained.
   The best-covered task per weakness type is chosen as the reference.
   e.g. gargoyles (crush) → "Scythe of vitur / Granite hammer / Soulreaper axe /
                              Inquisitor's mace / Abyssal bludgeon / ..."

2. **Known baseline weapons** per style — small curated safety-net lists used only
   if no suitable reference task can be found in recommended-gear.json.  These
   cover the most widely recognised options and are unlikely to change often.

Run order (recommended-gear.json must exist first):
    python scripts/fetch_recommended_gear.py   # initial run, no augmentation yet
    python scripts/fetch_weapon_tiers.py
    python scripts/fetch_recommended_gear.py   # second run, now augments gaps
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEAR_PATH = ROOT / "data" / "gear.json"
REC_PATH  = ROOT / "data" / "recommended-gear.json"
OUT_PATH  = ROOT / "data" / "weapon-tiers.json"

# Fallback weapon lists used when no reference task is found.
# Listed best-to-worst for general slayer use.
# Monster-specific weapons that shouldn't appear in general tier lists.
# Arclight/Emberlight are phenomenal vs demons but useless everywhere else.
MONSTER_SPECIFIC = {
    # Demon-slaying weapons
    "Arclight", "Emberlight",
    # Vampyre-slaying weapons
    "Blisterwood flail", "Blisterwood sickle", "Ivandis flail",
    # Kalphite-specific
    "Keris partisan", "Keris",
    # Leafy monster requirement (kurask/turoth only)
    "Leaf-bladed battleaxe", "Leaf-bladed sword", "Leaf-bladed spear",
}

# General-purpose weapon fallbacks used when the derived list is short.
# Items are ordered best-to-worst for typical slayer use.
BASELINE: dict[str, list[str]] = {
    "crush": [
        "Soulreaper axe",
        "Inquisitor's mace",
        "Abyssal bludgeon",
        "Elder maul",
        "Dragon warhammer",
        "Granite hammer",
        "Barronite mace",
        "Sarachnis cudgel",
        "Dragon mace",
        "Rune mace",
    ],
    "slash": [
        "Soulreaper axe",
        "Scythe of vitur",
        "Blade of saeldor",
        "Abyssal tentacle",
        "Abyssal whip",
        "Zombie axe",
        "Noxious halberd",
        "Dragon scimitar",
        "Rune scimitar",
    ],
    "stab": [
        "Osmumten's fang",
        "Noxious halberd",
        "Ghrazi rapier",
        "Dragon hunter lance",
        "Zamorakian hasta",
        "Abyssal dagger",
        "Dragon dagger",
        "Dragon longsword",
    ],
}

# Minimum number of weapons to consider a derived list "complete".
# Below this, the baseline is appended (deduped) to fill the list out.
MIN_DERIVED = 8


def weakness_to_tier_key(fallback_weak: str) -> str | None:
    w = fallback_weak.lower()
    if "crush" in w:
        return "crush"
    if "slash" in w:
        return "slash"
    if "stab" in w:
        return "stab"
    return None


def derive_from_recommended_gear(gear_by_id: dict, rec: dict) -> dict:
    """
    For each style (crush/slash/stab), find the task in recommended-gear.json
    with the most weapon options AND a matching fallbackWeak.  Store both the
    ranked weapon list AND the reference task's complete gear template so that
    augmented tasks get BiS gear for every slot, not just the weapon.

    Output shape per style:
        {
          "weapons":      ["Weapon A", "Weapon B", ...],  # ranked best-to-worst
          "gearTemplate": [{"slot": "Head", "item": "..."}, ...],  # all slots
          "sourceTask":   "gargoyles"
        }
    """
    tiers: dict = {}
    tasks = rec.get("tasks", {})

    for tier_key in ("crush", "slash", "stab"):
        best_task_id: str | None = None
        best_weapons: list[str] = []
        best_gear_template: list[dict] = []
        best_count = 0

        for task_id, task_data in tasks.items():
            melee = task_data.get("melee")
            if not melee:
                continue
            gear_entry = gear_by_id.get(task_id, {})
            if weakness_to_tier_key(gear_entry.get("fallbackWeak", "")) != tier_key:
                continue

            weapon_entry = next(
                (e for e in melee.get("gear", []) if e.get("slot") == "Weapon"),
                None,
            )
            if not weapon_entry:
                continue

            weapons = [
                w.strip() for w in weapon_entry["item"].split("/")
                if w.strip() and w.strip() not in MONSTER_SPECIFIC
            ]
            if len(weapons) > best_count:
                best_count = len(weapons)
                best_task_id = task_id
                best_weapons = weapons
                best_gear_template = melee.get("gear", [])

        if best_weapons:
            source = f"'{best_task_id}' ({best_count} weapons)"
            if best_count < MIN_DERIVED:
                existing = set(best_weapons)
                extra = [w for w in BASELINE.get(tier_key, []) if w not in existing]
                best_weapons = best_weapons + extra
                source += f" + {len(extra)} baseline"
            print(f"  {tier_key}: {source}, {len(best_gear_template)} gear slots in template")
            tiers[tier_key] = {
                "weapons": best_weapons,
                "gearTemplate": best_gear_template,
                "sourceTask": best_task_id,
            }
        else:
            print(f"  {tier_key}: no reference task found, will use baseline")

    return tiers


def main():
    if not GEAR_PATH.exists():
        print(f"Error: {GEAR_PATH} not found")
        sys.exit(1)

    gear = json.loads(GEAR_PATH.read_text(encoding="utf-8"))
    gear_by_id = {e["id"]: e for e in gear}

    rec: dict = {}
    if REC_PATH.exists():
        try:
            rec = json.loads(REC_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Warning: could not read recommended-gear.json ({e})")

    print("Deriving weapon tiers from recommended-gear.json...")
    tiers = derive_from_recommended_gear(gear_by_id, rec)

    # Fill any missing styles from baseline (weapons only, no template)
    for style in ("crush", "slash", "stab"):
        if style not in tiers:
            tiers[style] = {
                "weapons": BASELINE.get(style, []),
                "gearTemplate": [],
                "sourceTask": None,
            }
            print(f"  {style}: using baseline ({len(BASELINE.get(style, []))} weapons)")

    output = {
        "generatedAt": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
        "source": "Derived from recommended-gear.json (wiki-sourced) + curated baseline",
        "tiers": tiers,
    }
    OUT_PATH.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote weapon tiers to {OUT_PATH}")
    for style, data in tiers.items():
        w = data["weapons"]
        print(f"  {style}: {', '.join(w[:5])}{'...' if len(w) > 5 else ''} ({len(data['gearTemplate'])} template slots)")


if __name__ == "__main__":
    main()
