#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Render a JUnit XML file as a Markdown summary table.

Usage:
    python scripts/junit_to_summary.py <junit.xml> [output.md]

Writes the Markdown to *output.md* if given, and always prints it to stdout.
Used by ``run_tests.sh --report`` to produce a human-readable artifact that can
be posted to a pull request (e.g. ``gh pr comment <PR> --body-file ...``)
without committing anything to the repository.
"""

import sys
import xml.etree.ElementTree as ET


def render(xml_path: str) -> str:
    root = ET.parse(xml_path).getroot()
    suite = root.find("testsuite") if root.tag == "testsuites" else root

    rows: list[tuple[str, str, str, str]] = []
    passed = 0
    for tc in suite.findall("testcase"):
        name = tc.get("name", "?")
        elapsed = f"{float(tc.get('time', 0) or 0):.1f}s"
        failure, error, skipped = (
            tc.find("failure"),
            tc.find("error"),
            tc.find("skipped"),
        )
        if failure is not None:
            status, note = "FAIL", (failure.get("message") or "")[:90]
        elif error is not None:
            status, note = "ERROR", (error.get("message") or "")[:90]
        elif skipped is not None:
            status, note = "SKIP", (skipped.get("message") or "")[:90]
        else:
            status, note, passed = "PASS", "", passed + 1
        rows.append((name, status, elapsed, note))

    total = suite.get("tests", str(len(rows)))
    lines = [
        "## Integration test results - live SAS Viya",
        "",
        f"**{total} tests | {passed} passed | {suite.get('skipped', 0)} skipped | "
        f"{suite.get('failures', 0)} failed | {suite.get('errors', 0)} errors | "
        f"{float(suite.get('time', 0) or 0):.0f}s**",
        "",
        "| Test | Result | Time | Note |",
        "|---|---|---|---|",
    ]
    lines += [f"| `{n}` | {s} | {t} | {note} |" for n, s, t, note in rows]
    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    md = render(sys.argv[1])
    if len(sys.argv) >= 3:
        with open(sys.argv[2], "w", encoding="utf-8") as fh:
            fh.write(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
