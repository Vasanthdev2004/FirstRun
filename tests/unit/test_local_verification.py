from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from firstrun.domain.outcomes import Outcome
from firstrun.verification.local import verify_local
from firstrun.verification.source import capture_approved_source


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "notes-app"
TARGET = FIXTURE / ".firstrun" / "target.json"


class LocalVerificationPolicyTests(unittest.TestCase):
    def test_wrong_target_path_is_blocked_before_source_or_docker(self) -> None:
        with (
            patch("firstrun.verification.local.capture_approved_source") as capture,
            patch("firstrun.verification.local.prepare_docker_runtime") as docker,
        ):
            result = verify_local(FIXTURE, FIXTURE / "target.json")

        self.assertIs(result.outcome, Outcome.POLICY_BLOCKED)
        capture.assert_not_called()
        docker.assert_not_called()

    def test_docs_mismatch_is_blocked_before_any_docker_call(self) -> None:
        snapshot = capture_approved_source(FIXTURE)
        files = dict(snapshot.files)
        files["README.md"] = files["README.md"].replace(b"npm run dev", b"npm run other")
        mismatched = replace(snapshot, files=MappingProxyType(files))
        with (
            patch(
                "firstrun.verification.local.capture_approved_source",
                return_value=mismatched,
            ),
            patch("firstrun.verification.local.prepare_docker_runtime") as docker,
        ):
            result = verify_local(FIXTURE, TARGET)

        self.assertIs(result.outcome, Outcome.POLICY_BLOCKED)
        self.assertIn("README managed block", result.message or "")
        docker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
