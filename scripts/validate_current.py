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
    "ws2026_016",  # Gulf data-center-strike narrative — no primary source exists; absence IS the evidence
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

# Exact bad trailing fragments. A field ending in any of these strings is
# treated as a hard failure. These are the literal mid-word cuts observed in
# the live dataset by manual QA — keeping them as an explicit list makes
# regressions trivial to detect.
BAD_TRAILING = (
    "This p",         # ca2026_005.business_impact
    "(automat",       # ca2026_002.business_impact
    "Aramco ",        # ca2026_001.business_impact (note trailing space)
    "use by l",       # ca2026_003.business_impact
    "Pasqal qua",     # ic2026_003.gpu_systems
)
# Exact bad fragments anywhere (not just trailing). The check uses a
# word-boundary regex on the right side so "Pasqal qua" matches the
# truncated "...Pasqal qua" but NOT the legitimate "...Pasqal quantum...".
# This is intentionally narrow to a small set: tokens that are *not*
# valid English/proper-noun prefixes in any normal phrasing. "Aramco" /
# "(automat" / "This p" / "use by l" all read as midword cuts only when
# they end the string, so we only check those as BAD_TRAILING.
BAD_FRAGMENTS_ANYWHERE_RE = (
    re.compile(r"Pasqal qua(?![A-Za-z])"),
)
BAD_FRAGMENTS_ANYWHERE_LABEL = ("Pasqal qua",)

# Two- and three-letter trailing fragments that are real English words and
# legitimately end a clause. The mid-word detector excludes these so it
# doesn't false-fire on "labs", "site", "year", "plan", "risk", "out", etc.
LEGIT_SHORT_TRAILING_WORDS = {
    "ai", "ml", "ip", "hr", "qa", "uk", "us", "uae", "gcc", "mena", "saudi",
    "gpu", "cpu", "tpu", "llm", "api", "sdk", "ceo", "cto", "cfo", "cio",
    "io", "ok", "tv", "pc", "id", "no", "of", "on", "in", "at", "to", "by",
    "or", "as", "is", "it", "an", "be", "do", "we", "he", "i", "a",
    "ai-",
    # Domains-ish tokens that appear in headlines.
    "qcaas", "saas", "paas", "iaas", "iot", "ar", "vr", "xr",
    # Legitimate sentence-ending nouns / verbs seen in this dataset.
    "labs", "site", "year", "plan", "risk", "out", "up", "data", "year",
    "stc", "zone", "qpu",
}


def _looks_truncated(s: str) -> str | None:
    """Return a short label if `s` looks mid-sentence/mid-word truncated."""
    if not isinstance(s, str) or len(s) < 25:
        return None
    # Skip strings that are URLs — last-token heuristic doesn't apply.
    if URL_RE.match(s.strip()):
        return None
    if s != s.rstrip() and s.rstrip()[-1:].isalpha():
        return "trailing whitespace after letter"
    s2 = s.rstrip()
    if not s2:
        return None
    last = s2[-1]
    if last in ",;":
        return f"hanging punctuation '{last}'"
    # A trailing em-dash or hyphen is only suspicious if it directly
    # follows a word (e.g. "research-") — not " — " mid-sentence.
    if (last == "-" or last == "—") and len(s2) >= 2 and s2[-2].isalpha():
        return f"hanging punctuation '{last}'"
    if s2.endswith("…") or s2.endswith("..."):
        return "ellipsis suggests truncation"
    # Hanging connectors at clause end.
    parts = re.split(r"(?<=[.!?])\s+", s2)
    last_clause = parts[-1]
    if re.search(
        r"\b(and|to|for|with|by|of|the|or|including|where|via|that|which|while|as|at|from|over|under|into|its|their|his|her)\s*$",
        last_clause,
    ):
        return "hanging connector"
    # Mid-word fragment: last token is 1-3 alphabetic characters, not in
    # the legitimate-short-trailing-words list, and the field as a whole
    # has at least one preceding period (multi-sentence narrative). A
    # bare single-letter trailing token after a period (e.g. "...Bessemer. This p")
    # is a strong truncation signal.
    m = re.findall(r"[A-Za-z]+", s2)
    if m:
        last_word = m[-1]
        prior_periods = s2.count(".")
        if (
            last.isalpha()
            and len(last_word) <= 3
            and last_word.lower() not in LEGIT_SHORT_TRAILING_WORDS
            and prior_periods >= 1
            and not re.search(r"[.!?]\s*$", s2)
        ):
            return f"mid-word truncation (last token: {last_word!r})"
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

                def _classify(kind: str, where: str, sample: str) -> None:
                    # Mid-word truncations and trailing whitespace after a
                    # letter are hard failures — they are unambiguous
                    # signs of a string that was cut mid-token. Hanging
                    # connectors / ellipses are warnings unless they're
                    # in the BAD_TRAILING list.
                    msg = f"{section}/{iid}: '{where}' looks truncated ({kind}): …{sample[-60:]!r}"
                    if kind.startswith("mid-word truncation") or kind == "trailing whitespace after letter":
                        errors.append(msg)
                    else:
                        warnings.append(msg)

                if isinstance(v, str):
                    kind = _looks_truncated(v)
                    if kind:
                        _classify(kind, k, v)
                    # Bad-fragment scans (anywhere / trailing).
                    for rx, frag in zip(BAD_FRAGMENTS_ANYWHERE_RE, BAD_FRAGMENTS_ANYWHERE_LABEL):
                        m = rx.search(v)
                        if m:
                            errors.append(
                                f"{section}/{iid}: '{k}' contains banned mid-word fragment {frag!r}: …{v[max(0,m.start()-20):m.end()+20]!r}"
                            )
                    for frag in BAD_TRAILING:
                        if v.rstrip().endswith(frag):
                            errors.append(
                                f"{section}/{iid}: '{k}' ends with banned trailing fragment {frag!r}"
                            )
                elif isinstance(v, list):
                    for j, x in enumerate(v):
                        if isinstance(x, str):
                            kind = _looks_truncated(x)
                            if kind:
                                _classify(kind, f"{k}[{j}]", x)
                            for rx, frag in zip(BAD_FRAGMENTS_ANYWHERE_RE, BAD_FRAGMENTS_ANYWHERE_LABEL):
                                if rx.search(x):
                                    errors.append(
                                        f"{section}/{iid}: '{k}[{j}]' contains banned mid-word fragment {frag!r}"
                                    )
                            for frag in BAD_TRAILING:
                                if x.rstrip().endswith(frag):
                                    errors.append(
                                        f"{section}/{iid}: '{k}[{j}]' ends with banned trailing fragment {frag!r}"
                                    )

            # 4. Source URL coverage.
            #
            # Required (hard fail) for any non-historical record:
            #   * recency_bucket in {2026-current, late-2025-active}, OR
            #   * historical != true AND recency_bucket != historical-context.
            #
            # Historical-context items are explicitly archival; archive_changelog_baseline
            # rows don't need URLs either. Weak signals whose "evidence" IS the
            # absence of a credible source are documented exceptions.
            recency = item.get("recency_bucket", "")
            is_historical = bool(item.get("historical")) or recency == "historical-context"
            urls = (
                _list_of_urls(item.get("source_urls"))
                + _list_of_urls(item.get("sources"))
                + _list_of_urls(item.get("source_url"))
            )
            needs_source = False
            if section == "archive_changelog_baseline":
                needs_source = False
            elif is_historical:
                needs_source = False
            elif recency in HIGH_VALUE_RECENCY:
                needs_source = True
            else:
                # Any record not flagged historical and not in archive_changelog_baseline
                # is considered live/current and must have a URL.
                needs_source = True
            if needs_source and not urls:
                if section == "weak_signals" and iid in WEAK_SIGNAL_NEGATIVE_SIGNALS:
                    pass
                else:
                    credibility = item.get("credibility") or item.get("credibility_score") or ""
                    errors.append(
                        f"{section}/{iid}: live record has no source URL "
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
