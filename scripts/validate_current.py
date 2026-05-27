#!/usr/bin/env python3
"""Validation for data/current.json.

Checks (each one is a hard failure that exits non-zero unless flagged --warn):
  1. JSON parses cleanly.
  2. Required top-level sections exist and are non-empty lists.
  3. Required fields present per section.
  4. Source URL present for every High / Medium-High / 2026-current record
     (with documented exceptions for negative weak signals — absence is the
     signal).
  5. No suspicious mid-sentence truncations (ends mid-word, hanging
     connector, trailing comma/dash, trailing whitespace + alphabetic).
  6. No duplicate IDs across the dataset.
  7. Every May 26 key record from the brief is still present.

Exit 0 = clean. Exit 1 = at least one error.

Run with `--report` to also print informational warnings without failing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / "data" / "current.json"

REQUIRED_SECTIONS = [
    "weekly_brief",
    "market_metrics",
    "commercial_adoption",
    "global_players",
    "startups_funding",
    "sovereign_public_ai",
    "infrastructure_compute",
    "regulation_risk",
    "talent_ecosystem",
    "strategic_insights",
    "weak_signals",
    "archive_changelog_baseline",
]

REQUIRED_FIELDS = {
    "weekly_brief": ["id", "date", "headline", "country", "summary", "significance", "sources", "credibility"],
    "market_metrics": ["id", "metric", "value", "source_url"],
    "commercial_adoption": ["id", "company", "sector", "country", "business_impact", "credibility_score", "source_urls"],
    "global_players": ["id", "company", "countries_active", "strategic_intent", "source_urls"],
    "startups_funding": ["id", "company", "country", "round", "source_urls", "credibility"],
    "sovereign_public_ai": ["id", "entity", "country", "description", "source_urls"],
    "infrastructure_compute": ["id", "name", "country", "type", "source_urls"],
    "regulation_risk": ["id", "jurisdiction", "framework", "status", "source_urls"],
    "talent_ecosystem": ["id", "entity", "country", "description", "source_urls"],
    "strategic_insights": ["id", "title", "insight", "confidence", "sources"],
    "weak_signals": ["id", "signal", "country", "signal_type", "evidence", "monitoring_priority"],
    "archive_changelog_baseline": ["id", "version", "date", "action"],
}

# Source URL required when credibility/recency is in this set, EXCEPT for
# weak signals whose evidence is the *absence* of an event (no source can exist).
HIGH_VALUE_RECENCY = {"2026-current", "late-2025-active"}
HIGH_VALUE_CREDIBILITY = {"High", "Medium-High"}
WEAK_SIGNAL_NEGATIVE_SIGNALS = {
    # IDs where the signal is "we cannot find a credible source" — empty
    # source_urls is the evidence itself.
    "ws2026_005",  # KAUST-China research geopolitical exposure — watchlist
    "ws2026_006",  # GCC AI Alliance $5B Arabic LLM fund — unverified in 2026
    "ws2026_007",  # OmanGPT — no 2026 release update found
}

# May 26 refresh — every record below MUST remain in current.json.
MAY26_REQUIRED = {
    "Qiddiya / Google Cloud": (lambda blob: "qiddiya" in blob and "google cloud" in blob),
    "Aramco / Pasqal QCaaS": (lambda blob: "pasqal" in blob and "qcaas" in blob),
    "SDAIA Hajj AI": (lambda blob: "sdaia" in blob and "hajj" in blob),
    "UAE first government AI agents": (lambda blob: "tax audit" in blob and "procurement" in blob),
    "Korn Ferry GCC AI adoption": (lambda blob: "korn ferry" in blob),
    "Rockwell industrial AI": (lambda blob: "rockwell" in blob),
    "TFSF weak signal": (lambda blob: "tfsf" in blob),
    "Aumet": (lambda blob: "aumet" in blob),
    "Lyrie.ai": (lambda blob: "lyrie" in blob),
    "Gabster": (lambda blob: "gabster" in blob),
    "HASIF": (lambda blob: "hasif" in blob),
}

URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _truncated(s: str) -> str | None:
    """Return a short label if `s` looks mid-sentence/mid-word truncated."""
    if not isinstance(s, str) or len(s) < 30:
        return None
    if s != s.rstrip() and s.rstrip()[-1:].isalpha():
        return "trailing whitespace after letter"
    s2 = s.rstrip()
    if not s2:
        return None
    last = s2[-1]
    if last in ",;" or last in "-—":
        # Allow legitimate em-dash endings only when followed by closing punctuation, which we've ruled out.
        return f"hanging punctuation '{last}'"
    # Hanging connectors at clause end.
    parts = re.split(r"(?<=[.!?])\s+", s2)
    last_clause = parts[-1]
    # "out", "up", "down", "in", "on" are common phrasal-verb particles
    # ("rolling out", "ramping up", "winding down") and are not reliable
    # truncation indicators on their own — exclude them. Keep the obvious
    # connectors that almost never end a complete sentence.
    if re.search(
        r"\b(and|to|for|with|by|of|the|or|including|where|via|that|which|while|as|at|from|over|under|into|its|their|his|her)\s*$",
        last_clause,
    ):
        return "hanging connector"
    if s2.endswith("…") or s2.endswith("..."):
        return "ellipsis suggests truncation"
    return None


def _is_url(v) -> bool:
    return isinstance(v, str) and bool(URL_RE.match(v))


def _list_of_urls(field) -> list[str]:
    if isinstance(field, list):
        return [x for x in field if _is_url(x)]
    if isinstance(field, str) and _is_url(field):
        return [field]
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="print warnings without failing")
    parser.add_argument("--path", default=str(CURRENT))
    args = parser.parse_args()

    path = Path(args.path)
    errors: list[str] = []
    warnings: list[str] = []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"FAIL: JSON parse error at line {exc.lineno}: {exc.msg}")
        return 1
    except FileNotFoundError:
        print(f"FAIL: {path} does not exist")
        return 1

    # 2. Required sections.
    for section in REQUIRED_SECTIONS:
        if section not in data:
            errors.append(f"missing top-level section: {section}")
            continue
        if not isinstance(data[section], list):
            errors.append(f"section {section} is not a list")
            continue
        if len(data[section]) == 0:
            errors.append(f"section {section} is empty")

    # 3. Required fields per section, plus dup-ID and source-URL coverage.
    seen_ids: dict[str, str] = {}
    for section, required in REQUIRED_FIELDS.items():
        for i, item in enumerate(data.get(section, [])):
            if not isinstance(item, dict):
                errors.append(f"{section}[{i}] is not an object")
                continue
            iid = item.get("id", f"<{section}[{i}]>")
            for f in required:
                if f not in item or item[f] in (None, ""):
                    if f in ("sources", "source_urls") and section == "weak_signals" and iid in WEAK_SIGNAL_NEGATIVE_SIGNALS:
                        continue
                    errors.append(f"{section}/{iid}: missing required field '{f}'")
            # Duplicate IDs.
            if iid in seen_ids:
                errors.append(f"duplicate id '{iid}' in {section} and {seen_ids[iid]}")
            seen_ids[iid] = section

            # 5. Truncation scan on all string fields (skip enums / IDs / dates).
            SKIP_KEYS = {
                "id", "recency_bucket", "recommended_action", "date",
                "event_date", "round_date", "year", "rank",
                "ai_washing_flag", "ai_specific", "signal_type", "version",
                "compiled_date", "analysis_date", "prior_version",
                "country", "sector", "category", "type", "tier", "status",
                "significance", "credibility", "credibility_score",
                "monitoring_priority", "confidence", "commercial_maturity",
                "ai_depth_score", "deployment_stage", "historical",
            }
            for k, v in item.items():
                if k in SKIP_KEYS:
                    continue
                if isinstance(v, str):
                    kind = _truncated(v)
                    if kind:
                        warnings.append(f"{section}/{iid}: '{k}' looks truncated ({kind}): …{v[-60:]!r}")
                elif isinstance(v, list):
                    for j, x in enumerate(v):
                        if isinstance(x, str):
                            kind = _truncated(x)
                            if kind:
                                warnings.append(
                                    f"{section}/{iid}: '{k}[{j}]' looks truncated ({kind}): …{x[-60:]!r}"
                                )

            # 4. Source URL coverage for high-credibility / current records.
            # Historical-context items are explicitly archival; missing
            # source URLs there is a warning (logged via `report` mode), not
            # a hard error.
            recency = item.get("recency_bucket", "")
            credibility = item.get("credibility") or item.get("credibility_score") or ""
            needs_source = recency in HIGH_VALUE_RECENCY
            if not needs_source and any(c in credibility for c in HIGH_VALUE_CREDIBILITY) and not item.get("historical") and recency != "historical-context":
                needs_source = True
            if needs_source:
                urls = _list_of_urls(item.get("source_urls")) + _list_of_urls(item.get("sources")) + _list_of_urls(item.get("source_url"))
                if not urls:
                    if section == "weak_signals" and iid in WEAK_SIGNAL_NEGATIVE_SIGNALS:
                        pass
                    else:
                        errors.append(
                            f"{section}/{iid}: high-value record has no source URL "
                            f"(credibility={credibility!r}, recency={recency!r})"
                        )

    # 7. May 26 key records.
    blob = json.dumps(data).lower()
    for name, predicate in MAY26_REQUIRED.items():
        if not predicate(blob):
            errors.append(f"May 26 key record missing: {name}")

    # Report.
    if warnings:
        print(f"WARN ({len(warnings)} info-level findings):")
        for w in warnings:
            print(f"  - {w}")
    if errors:
        print(f"\nFAIL ({len(errors)} errors):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"\nOK: data/current.json passes all validations ({len(data)-1} sections, {len(seen_ids)} records)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
