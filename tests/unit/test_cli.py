from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from firstrun.cli import run


class CliTests(unittest.TestCase):
    @patch("firstrun.cli.run_doctor")
    def test_doctor_json_returns_authoritative_exit_code(self, doctor) -> None:
        doctor.return_value = {
            "schema_version": 1,
            "repo_path": "C:/repo",
            "outcome": "infrastructure_error",
            "exit_code": 12,
            "checks": {"docker_server": {"status": "failed", "message": "down"}},
        }
        output = io.StringIO()

        with redirect_stdout(output):
            code = run(("doctor", "--repo", ".", "--json"))

        self.assertEqual(code, 12)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["outcome"], "infrastructure_error")
        doctor.assert_called_once()

    @patch("firstrun.cli.run_doctor")
    def test_human_output_contains_each_check_without_details(self, doctor) -> None:
        doctor.return_value = {
            "schema_version": 1,
            "repo_path": "C:/repo",
            "outcome": "passed",
            "exit_code": 0,
            "checks": {
                "python": {
                    "status": "passed",
                    "message": "Python is supported",
                    "details": {"version": "3.12.13"},
                }
            },
        }
        output = io.StringIO()

        with redirect_stdout(output):
            code = run(("doctor",))

        self.assertEqual(code, 0)
        self.assertIn("FirstRun doctor: passed", output.getvalue())
        self.assertIn("python: passed", output.getvalue())
        self.assertNotIn("3.12.13", output.getvalue())


if __name__ == "__main__":
    unittest.main()
