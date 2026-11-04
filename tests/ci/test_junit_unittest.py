from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]


class JUnitUnittestRunnerTests(unittest.TestCase):
    def _run(self, body: str) -> tuple[int, ET.Element]:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "sample_pkg"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "test_sample.py").write_text(body)
            report = Path(tmp) / "out" / "junit.xml"
            completed = subprocess.run(
                [sys.executable, "-m", "scripts.ci.junit_unittest", "--junit-xml", str(report),
                 "--suite-name", "sample", "discover", "-s", str(package), "-t", tmp],
                cwd=ROOT, text=True, capture_output=True, check=False,
                env={"PYTHONPATH": f"{ROOT}:{tmp}", "PATH": "/usr/bin:/bin"},
            )
            return completed.returncode, ET.parse(report).getroot()

    def test_passing_suite_writes_counts_and_exits_zero(self) -> None:
        code, root = self._run(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_a(self): pass\n"
            "    @unittest.skip('x')\n"
            "    def test_b(self): pass\n"
        )
        self.assertEqual(code, 0)
        suite = root.find("testsuite")
        self.assertEqual(suite.get("name"), "sample")
        self.assertEqual((suite.get("tests"), suite.get("failures"), suite.get("errors"), suite.get("skipped")),
                         ("2", "0", "0", "1"))

    def test_failure_and_error_are_reported_and_exit_nonzero(self) -> None:
        code, root = self._run(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_fail(self): self.assertEqual(1, 2)\n"
            "    def test_error(self): raise RuntimeError('boom')\n"
        )
        self.assertNotEqual(code, 0)
        suite = root.find("testsuite")
        self.assertEqual((suite.get("failures"), suite.get("errors")), ("1", "1"))
        failed = {case.get("name"): case for case in suite.iter("testcase")}
        self.assertIsNotNone(failed["test_fail"].find("failure"))
        self.assertIn("boom", failed["test_error"].find("error").text)

    def test_import_failure_is_an_error_not_an_empty_pass(self) -> None:
        code, root = self._run("import does_not_exist_module\n")
        self.assertNotEqual(code, 0)
        self.assertNotEqual(root.find("testsuite").get("errors"), "0")


if __name__ == "__main__":
    unittest.main()
