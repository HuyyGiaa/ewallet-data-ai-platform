from __future__ import annotations

import unittest

from ops.gcp.verify_delta_run import verify_delta_run


class Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class VerifyDeltaRunTest(unittest.TestCase):
    def test_exact_table_set_and_delta_logs_pass(self) -> None:
        def runner(command, **kwargs):
            if command[-1].endswith("/run/silver/"):
                return Result(stdout="gs://bucket/lakehouse/runs/run/silver/a/\n")
            return Result(stdout="gs://bucket/lakehouse/runs/run/silver/a/_delta_log/0.json\n")

        verify_delta_run(
            "gs://bucket/lakehouse/runs/run/silver", {"a"}, runner=runner
        )

    def test_extra_table_is_rejected(self) -> None:
        def runner(command, **kwargs):
            return Result(
                stdout=(
                    "gs://bucket/lakehouse/runs/run/gold/a/\n"
                    "gs://bucket/lakehouse/runs/run/gold/unexpected/\n"
                )
            )

        with self.assertRaisesRegex(ValueError, "table set mismatch"):
            verify_delta_run(
                "gs://bucket/lakehouse/runs/run/gold", {"a"}, runner=runner
            )


if __name__ == "__main__":
    unittest.main()
