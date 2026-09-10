from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from firstrun.domain.contracts import Recipe, Target
from firstrun.domain.evidence import ControllerBinding
from firstrun.domain.outcomes import Outcome
from firstrun.domain.repair import RepairProviderConfig
from firstrun.preflight.docker import ResolvedImage
from firstrun.verification.readme import replace_block_bytes
from firstrun.verification.source import SourceSnapshot, content_tree_digest
from firstrun.worker.docker import (
    DockerPhaseResult,
    PreparedDockerRuntime,
    WorkerProblem,
    run_docker_phase,
)
from firstrun.orchestration.repair import repair_snapshot, verify_snapshot


FIXTURE = ROOT / "fixtures" / "notes-app"
IMAGE_ID = "sha256:" + "d" * 64
REPOSITORY_REFERENCE = "node@sha256:" + "e" * 64


def _snapshot(*, repaired: bool = False) -> SourceSnapshot:
    files = {
        ".firstrun/target.json": (FIXTURE / ".firstrun/target.json").read_bytes(),
        ".firstrun/recipe.json": (FIXTURE / ".firstrun/recipe.json").read_bytes(),
        "README.md": (FIXTURE / "README.md").read_bytes(),
        "package.json": (FIXTURE / "package.json").read_bytes(),
    }
    if repaired:
        recipe_bytes = (ROOT / "examples" / "recipe.fixed.json").read_bytes()
        recipe = Recipe.model_validate_json(recipe_bytes)
        files[".firstrun/recipe.json"] = recipe_bytes
        files["README.md"] = replace_block_bytes(files["README.md"], recipe)
    return SourceSnapshot(
        repository_root=ROOT,
        approved_repo_path=FIXTURE,
        base_commit="a" * 40,
        base_tree="b" * 40,
        source_tree="c" * 40,
        archive_digest="sha256:" + "f" * 64,
        content_tree_digest=content_tree_digest(files),
        files=MappingProxyType(files),
    )


def _runtime() -> PreparedDockerRuntime:
    return PreparedDockerRuntime(
        docker_context="test",
        docker_endpoint="test-endpoint",
        docker_server_version="test",
        image=ResolvedImage(
            supplied_reference="node:22-bookworm-slim",
            repository_digest=REPOSITORY_REFERENCE,
            image_id=IMAGE_ID,
            os="linux",
            architecture="amd64",
        ),
        runner=Mock(),
        config=Mock(),
    )


def _provider() -> RepairProviderConfig:
    return RepairProviderConfig(
        aws_profile="temporary-test",
        region="us-east-1",
        model_id="test.model-v1",
        provider_cost_acknowledged=True,
        credential_identity_verified=True,
    )


class PreparedRepairSeamTests(unittest.TestCase):
    def test_snapshot_digest_mismatch_is_rejected_before_runtime(self) -> None:
        bad = _snapshot()
        bad = SourceSnapshot(**{**bad.__dict__, "content_tree_digest": "sha256:" + "0" * 64})
        with patch(
            "firstrun.orchestration.repair.prepare_docker_runtime"
        ) as prepare_runtime:
            result = verify_snapshot(bad)
        self.assertIs(result.outcome, Outcome.POLICY_BLOCKED)
        prepare_runtime.assert_not_called()

    def test_verify_snapshot_persists_binding_then_terminal_phase(self) -> None:
        observed: list[tuple[str, dict[str, object]]] = []

        def fake_phase(*args: object, **kwargs: object) -> DockerPhaseResult:
            binding = kwargs["binding"]
            self.assertIsInstance(binding, ControllerBinding)
            self.assertTrue(kwargs["authorized_recipe"])
            return DockerPhaseResult(Outcome.FAILED, binding, None, "expected test failure")

        with (
            patch(
                "firstrun.orchestration.repair.prepare_docker_runtime",
                return_value=_runtime(),
            ),
            patch(
                "firstrun.orchestration.repair.run_docker_phase",
                side_effect=fake_phase,
            ),
        ):
            result = verify_snapshot(
                _snapshot(repaired=True),
                observer=lambda event, payload: observed.append((event, payload)),
                expected_runtime_digest=IMAGE_ID,
                expected_runtime_repository_digest=REPOSITORY_REFERENCE,
            )

        self.assertIs(result.outcome, Outcome.FAILED)
        self.assertEqual(
            [event for event, _ in observed],
            ["verification_running", "verification_completed"],
        )
        self.assertEqual(
            observed[0][1]["binding"], observed[1][1]["binding"]
        )

    def test_observer_failure_prevents_resource_phase(self) -> None:
        with (
            patch(
                "firstrun.orchestration.repair.prepare_docker_runtime",
                return_value=_runtime(),
            ),
            patch("firstrun.orchestration.repair.run_docker_phase") as phase,
        ):
            result = verify_snapshot(
                _snapshot(),
                observer=lambda _event, _payload: (_ for _ in ()).throw(
                    RuntimeError("store unavailable")
                ),
            )
        self.assertIs(result.outcome, Outcome.INFRASTRUCTURE_ERROR)
        phase.assert_not_called()

    def test_registered_runtime_mismatch_prevents_repair_execution(self) -> None:
        with (
            patch(
                "firstrun.orchestration.repair.sys.flags",
                SimpleNamespace(isolated=1),
            ),
            patch(
                "firstrun.orchestration.repair.prepare_docker_runtime",
                return_value=_runtime(),
            ),
            patch("firstrun.orchestration.repair.run_docker_phase") as phase,
            patch("firstrun.orchestration.repair.run_live_repair_agent") as agent,
        ):
            result = repair_snapshot(
                _snapshot(),
                _provider(),
                expected_runtime_digest="sha256:" + "0" * 64,
            )
        self.assertIs(result.outcome, Outcome.POLICY_BLOCKED)
        phase.assert_not_called()
        agent.assert_not_called()

    def test_worker_default_baseline_policy_remains_strict(self) -> None:
        snapshot = _snapshot(repaired=True)
        target = Target.model_validate_json(snapshot.content(".firstrun/target.json"))
        recipe = Recipe.model_validate_json(snapshot.content(".firstrun/recipe.json"))
        with patch("firstrun.worker.docker._QUARANTINED_ENDPOINTS", set()):
            default = run_docker_phase(
                _runtime(), snapshot, target, recipe, phase="baseline"
            )
        self.assertIs(default.outcome, Outcome.POLICY_BLOCKED)

        with (
            patch("firstrun.worker.docker._QUARANTINED_ENDPOINTS", set()),
            patch(
                "firstrun.worker.docker._create_app",
                side_effect=WorkerProblem(
                    Outcome.INFRASTRUCTURE_ERROR, "stop before Docker"
                ),
            ),
            patch("firstrun.worker.docker._cleanup", return_value=([], set(), {})),
        ):
            reviewed = run_docker_phase(
                _runtime(),
                snapshot,
                target,
                recipe,
                phase="baseline",
                authorized_recipe=True,
            )
        self.assertIs(reviewed.outcome, Outcome.INFRASTRUCTURE_ERROR)


if __name__ == "__main__":
    unittest.main()
