"""Run stdlib unittest and write a JUnit XML report Jenkins can publish.

Usage mirrors ``python -m unittest`` after the report options, for example
``python -m scripts.ci.junit_unittest --junit-xml out.xml discover -s tests/x -t .``
or ``... --junit-xml out.xml tests.module_a tests.module_b``. It uses only the
standard library so it runs inside every locked component environment.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
import time
import traceback
import unittest
import xml.etree.ElementTree as ET


@dataclass
class _Case:
    classname: str
    name: str
    seconds: float = 0.0
    outcome: str = "passed"
    message: str = ""
    detail: str = ""


class _Collector(unittest.TextTestResult):
    def __init__(self, stream, descriptions, verbosity):  # type: ignore[no-untyped-def]
        super().__init__(stream, descriptions, verbosity)
        self.cases: list[_Case] = []
        self._started: dict[str, float] = {}

    def _case(self, test: unittest.TestCase) -> _Case:
        test_id = test.id()
        classname, _, name = test_id.rpartition(".")
        case = _Case(classname or test_id, name or test_id)
        case.seconds = time.monotonic() - self._started.pop(test_id, time.monotonic())
        self.cases.append(case)
        return case

    def startTest(self, test: unittest.TestCase) -> None:
        self._started[test.id()] = time.monotonic()
        super().startTest(test)

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        self._case(test)

    def addFailure(self, test: unittest.TestCase, err) -> None:  # type: ignore[no-untyped-def]
        super().addFailure(test, err)
        self._record(test, "failure", err)

    def addError(self, test: unittest.TestCase, err) -> None:  # type: ignore[no-untyped-def]
        super().addError(test, err)
        self._record(test, "error", err)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        case = self._case(test)
        case.outcome, case.message = "skipped", reason

    def addExpectedFailure(self, test: unittest.TestCase, err) -> None:  # type: ignore[no-untyped-def]
        super().addExpectedFailure(test, err)
        self._case(test)

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        case = self._case(test)
        case.outcome, case.message = "failure", "unexpected success"

    def _record(self, test: unittest.TestCase, outcome: str, err) -> None:  # type: ignore[no-untyped-def]
        case = self._case(test)
        case.outcome = outcome
        case.message = f"{err[0].__name__}: {err[1]}"
        case.detail = "".join(traceback.format_exception(*err))


def _write(path: Path, suite_name: str, cases: list[_Case], seconds: float) -> None:
    count = lambda outcome: sum(case.outcome == outcome for case in cases)  # noqa: E731
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", {
        "name": suite_name,
        "tests": str(len(cases)),
        "failures": str(count("failure")),
        "errors": str(count("error")),
        "skipped": str(count("skipped")),
        "time": f"{seconds:.3f}",
    })
    for case in cases:
        element = ET.SubElement(suite, "testcase", {
            "classname": case.classname, "name": case.name, "time": f"{case.seconds:.3f}",
        })
        if case.outcome in {"failure", "error"}:
            ET.SubElement(element, case.outcome, {"message": case.message}).text = case.detail
        elif case.outcome == "skipped":
            ET.SubElement(element, "skipped", {"message": case.message})
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit-xml", type=Path, required=True)
    parser.add_argument("--suite-name", default="unittest")
    parser.add_argument("unittest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    started = time.monotonic()
    program = unittest.main(
        module=None,
        argv=["unittest", *args.unittest_args],
        testRunner=unittest.TextTestRunner(resultclass=_Collector, verbosity=1),
        exit=False,
    )
    result = program.result
    _write(args.junit_xml, args.suite_name, result.cases, time.monotonic() - started)
    failed = not result.wasSuccessful() or not result.testsRun
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
