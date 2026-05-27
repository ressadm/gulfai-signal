#!/usr/bin/env python3
"""Direct assertion that the public dataset contains none of the exact bad
fragments seen by live QA, and that there are no duplicate IDs.

This runs alongside validate_current.py and is intentionally narrow: it
asserts hard contracts that the live site must never regress on.

Exit 0 = clean. Exit 1 = at least one assertion failed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    ROOT / "data" / "current.json",
    ROOT / "data" / "archive" / "2026-05-26.json",
]

# Bad trailing fragments. A field ending in any of these is broken.
BAD_TRAILING = (
    "This p",
    "(automat",
    "Aramco ",
    "use by l",
    "Pasqal qua",
)
# Bad fragments that should never appear mid-text either.
# "Pasqal qua" is the only fragment narrow enough to test as a mid-text
# regex with a right-side word boundary — every other bad trailing string
# is also a valid English prefix elsewhere.
BAD_MIDTEXT_RE = (re.compile(r"Pasqal qua(?![A-Za-z])"),)


def _walk_strings(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, x in enumerate(obj):
            yield from _walk_strings(x, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


def check_file(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{path}: file not found"]
    except json.JSONDecodeError as exc:
        return [f"{path}: JSON parse error at line {exc.lineno}: {exc.msg}"]

    # 1. Bad trailing / mid-text fragments anywhere.
    for path_str, s in _walk_strings(data):
        # Allow the v2.3.2 changelog summary itself to mention the literal
        # phrases used to describe the bug — they're documentation, not
        # data. Identify it by JSON path prefix.
        if path_str.startswith("archive_changelog_baseline["):
            continue
        for frag in BAD_TRAILING:
            if s.rstrip().endswith(frag):
                errors.append(f"{path.name}: {path_str} ends with banned fragment {frag!r}")
        for rx in BAD_MIDTEXT_RE:
            if rx.search(s):
                errors.append(f"{path.name}: {path_str} contains banned mid-word fragment matching {rx.pattern!r}")

    # 2. Duplicate IDs across the whole file.
    seen: dict[str, str] = {}
    for section, items in data.items():
        if section == "_meta" or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = item.get("id")
            if not iid:
                continue
            if iid in seen:
                errors.append(f"{path.name}: duplicate id {iid!r} (in {seen[iid]!r} and {section!r})")
            else:
                seen[iid] = section

    return errors


def main() -> int:
    all_errors: list[str] = []
    for p in TARGETS:
        all_errors.extend(check_file(p))
    if all_errors:
        print(f"FAIL ({len(all_errors)} errors):")
        for e in all_errors:
            print(f"  - {e}")
        return 1
    print(f"OK: no banned fragments, no duplicate IDs ({len(TARGETS)} file(s) checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
