"""Fail unless a JUnit report shows every spike test ran and passed.

``python3 -I check_junit.py report.xml --min-tests N``: any skipped, failed or
errored test, or fewer than N tests, is a failure.  A required gVisor test can
therefore never be skipped silently.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET  # nosec B405 - parses our own pytest report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--min-tests", type=int, required=True)
    args = parser.parse_args()

    root = ET.parse(args.report).getroot()  # nosec B314 - our own pytest report
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {key: sum(int(s.get(key, 0)) for s in suites) for key in ("tests", "failures", "errors", "skipped")}
    print("spike results:", totals)
    for case in root.iter("testcase"):
        for outcome in ("skipped", "failure", "error"):
            if case.find(outcome) is not None:
                print(f"  {outcome.upper()}: {case.get('classname')}::{case.get('name')}")
    problems = [f"{key}={totals[key]}" for key in ("failures", "errors", "skipped") if totals[key]]
    if totals["tests"] < args.min_tests:
        problems.append(f"only {totals['tests']} tests ran (expected at least {args.min_tests})")
    if problems:
        print("gVisor spike FAILED: " + ", ".join(problems))
        return 1
    print("gVisor spike: all required tests ran and passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
