#!/usr/bin/env python3
"""Repair pass for data/current.json.

Fixes specific known issues identified by the v2.3.1 quality audit:
- Truncated business_impact / key_deals strings (UI cards rendered "Aramco "
  or "(automat" mid-word). Where a sibling field (description / why_it_matters)
  holds the complete text, copy it across.
- Duplicate archive_changelog_baseline IDs (two entries shared acb_001).
- _legacy ID suffix is preserved as the canonical identifier but a sibling
  `historical` flag is added so the UI does not need to parse the ID.
- Adds a top-level `source_quality_legend` block to _meta so the UI can
  surface a credibility methodology link in future builds.

Idempotent: re-running the script on a clean file is a no-op.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / "data" / "current.json"


def _find(items, iid):
    for x in items:
        if isinstance(x, dict) and x.get("id") == iid:
            return x
    return None


def repair(data: dict) -> tuple[dict, list[str]]:
    changes: list[str] = []

    # 1. Truncated narrative fields — replace from description where it
    # exists and is materially longer.
    truncated_pairs = [
        ("commercial_adoption", "ca2026_001", "business_impact", "description"),
        ("commercial_adoption", "ca2026_002", "business_impact", "description"),
        ("commercial_adoption", "ca2026_003", "business_impact", "description"),
    ]
    for section, iid, target, source in truncated_pairs:
        item = _find(data.get(section, []), iid)
        if not item:
            continue
        cur = item.get(target, "") or ""
        src = item.get(source, "") or ""
        if src and len(src) > len(cur) + 5 and not cur.rstrip().endswith((".", "!", "?")):
            item[target] = src
            changes.append(f"{section}/{iid}: {target} restored from {source}")

    # 2. global_players key_deals[0] truncations — restore from description.
    for iid in (
        "gp2026_001", "gp2026_002", "gp2026_003", "gp2026_004",
        "gp2026_005", "gp2026_006", "gp2026_007", "gp2026_008",
    ):
        item = _find(data.get("global_players", []), iid)
        if not item:
            continue
        deals = item.get("key_deals") or []
        desc = item.get("description", "") or ""
        if deals and isinstance(deals[0], str) and desc:
            first = deals[0]
            if first.rstrip().endswith("…") or first.rstrip().endswith("...") or (
                len(desc) > len(first) + 10 and first[:80] == desc[:80]
            ):
                deals[0] = desc
                item["key_deals"] = deals
                changes.append(f"global_players/{iid}: key_deals[0] restored from description")

    # 2b. strategic_insights si2026_003: implications[0] holds a sentence
    # prefix of `insight` rather than a bullet. Replace with a real list of
    # implications (one bullet per logical implication).
    si3 = _find(data.get("strategic_insights", []), "si2026_003")
    if si3 and si3.get("implications"):
        first_impl = si3["implications"][0] if si3["implications"] else ""
        if isinstance(first_impl, str) and first_impl.rstrip().endswith(("the in", "the", "in")):
            si3["implications"] = [
                "AI data centers in the GCC are now treated as legitimate military targets, raising the infrastructure risk premium for capital allocation.",
                "Banking, payments, and enterprise services experienced outages from AWS UAE/Bahrain disruption — operational continuity must be re-engineered against geopolitical disruption.",
                "Near-term: no withdrawal of committed capital (too large); medium-term: hyperscalers may distribute next-wave capacity to India, Northern Europe, and Southeast Asia.",
                "UAE and Saudi Arabia must address this risk explicitly to preserve hyperscaler confidence; the April 8 ceasefire provides temporary stability but not structural resolution.",
            ]
            changes.append("strategic_insights/si2026_003: implications rewritten as bullets")

    # 3. Trim trailing whitespace on string fields globally (cosmetic).
    def _strip(obj):
        if isinstance(obj, dict):
            for k, v in list(obj.items()):
                if isinstance(v, str):
                    s = v.rstrip()
                    if s != v:
                        obj[k] = s
                else:
                    _strip(v)
        elif isinstance(obj, list):
            for x in obj:
                _strip(x)

    before = json.dumps(data, sort_keys=True)
    _strip(data)
    if json.dumps(data, sort_keys=True) != before:
        changes.append("trimmed trailing whitespace on string fields")

    # 4. Duplicate archive_changelog_baseline IDs. Two entries shared acb_001.
    acb = data.get("archive_changelog_baseline", [])
    seen: dict[str, int] = {}
    for i, item in enumerate(acb):
        if not isinstance(item, dict):
            continue
        iid = item.get("id", "")
        if iid in seen:
            # Renumber the second collision based on version digits or position.
            new_iid = f"acb_{i+1:03d}"
            while new_iid in seen:
                new_iid = f"acb_{int(new_iid.split('_')[1])+1:03d}"
            item["id"] = new_iid
            changes.append(f"archive_changelog_baseline[{i}]: id {iid} -> {new_iid}")
            seen[new_iid] = i
        else:
            seen[iid] = i

    # 5. Add `historical` flag to _legacy IDs so the UI no longer needs to
    # parse the suffix. Keep the ID stable for backwards compatibility.
    legacy_count = 0
    for section in data:
        if section == "_meta":
            continue
        items = data[section]
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = item.get("id", "")
            if iid.endswith("_legacy") and not item.get("historical"):
                item["historical"] = True
                legacy_count += 1
    if legacy_count:
        changes.append(f"tagged {legacy_count} _legacy items with historical=true")

    # 6. Document the source-quality methodology in _meta so the UI / future
    # consumers have a clear reference.
    meta = data.setdefault("_meta", {})
    legend = {
        "High": "Primary source: official company / government press release, regulator filing, or a tier-1 outlet (Reuters, Bloomberg, CNBC, NYT, FT, Arab News, The National) with first-party confirmation.",
        "Medium-High": "Reputable secondary source (industry trade press, MIT Sloan ME, Wamda, Magnitt, Lucidity Insights) corroborated by at least one primary indicator or a second secondary source.",
        "Medium": "Single secondary source with limited corroboration. Use with directional confidence; verify before acting commercially.",
        "Low-Medium": "Press-release distribution (PR Newswire, EIN Presswire, GlobeNewswire) or vendor-self-reported claims without independent verification. Treat as weak signal.",
        "Low": "Rumor, unsourced trade chatter, or single anonymous reference. Watchlist only.",
    }
    if meta.get("credibility_legend") != legend:
        meta["credibility_legend"] = legend
        changes.append("_meta.credibility_legend updated")

    if "recency_bucket_legend" not in meta:
        meta["recency_bucket_legend"] = {
            "2026-current": "Verified 2026 event, deployment, regulatory effect, or funding close.",
            "late-2025-active": "Late-2025 item with active 2026 continuation or first-quarter milestone.",
            "historical-context": "Pre-2026 or superseded item kept for baseline / change tracking.",
            "stale-review": "Item flagged for analyst re-verification before live use.",
        }
        changes.append("_meta.recency_bucket_legend added")

    # 7. Bump version metadata if anything changed.
    if changes:
        meta["version"] = "2.3.1"
        meta["compiled_date"] = meta.get("compiled_date", "2026-05-26")
        meta["last_quality_pass"] = "2026-05-27"
        meta["prior_version"] = meta.get("prior_version", "2.0.0 (2026-05-06)")
        changes.append("_meta.version -> 2.3.1; last_quality_pass=2026-05-27")

    return data, changes


def main() -> int:
    data = json.loads(CURRENT.read_text(encoding="utf-8"))
    data, changes = repair(data)
    if not changes:
        print("repair: no changes needed")
        return 0
    CURRENT.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"repair: applied {len(changes)} change(s)")
    for c in changes:
        print(f"  - {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
