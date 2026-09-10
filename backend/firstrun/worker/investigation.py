"""Fresh, controller-owned Docker investigation session for M2 tools."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace

from firstrun.domain.contracts import Recipe, RecipeStep, Target
from firstrun.domain.evidence import (
    CleanupEvidence,
    CommandEvidence,
    ContentDigest,
    ControllerBinding,
)
from firstrun.domain.outcomes import Outcome
from firstrun.verification.policy import CONTROLLED_NODE_FIXTURE_POLICY
from firstrun.verification.source import SourcePolicyError, SourceSnapshot
from firstrun.worker.docker import (
    PreparedDockerRuntime,
    WorkerProblem,
    _DeadlineDockerCliRunner,
    _IDLE_SUPERVISOR,
    _assert_container,
    _cleanup,
    _create_app,
    _endpoint_is_quarantined,
    _execute_foreground,
    _load_unique_json,
    _quarantine_endpoint,
    _seed_fresh_workspace,
    _start_container,
    _write_workspace,
)


@dataclass(frozen=True)
class InvestigationEvidence:
    """Bounded lifecycle record kept by the controller, never authored by the model."""

    binding: ControllerBinding
    app_container_id: str | None
    workspace_marker_digest: ContentDigest | None
    setup_command: CommandEvidence | None
    diagnostics: tuple[CommandEvidence, ...]
    cleanup: CleanupEvidence
    outcome: Outcome
    sanitized_errors: tuple[str, ...] = ()

    @property
    def proof_may_start(self) -> bool:
        return bool(
            self.outcome in {Outcome.PASSED, Outcome.FAILED}
            and self.app_container_id is not None
            and self.workspace_marker_digest is not None
            and self.setup_command is not None
            and self.cleanup.app_container_created
            and self.cleanup.app_container_removed
            and self.cleanup.workspace_created
            and self.cleanup.workspace_removed
            and self.cleanup.succeeded
        )


class DockerInvestigationSession:
    """Lazily create one fresh sandbox for an agent attempt and always reconcile it."""

    def __init__(
        self,
        runtime: PreparedDockerRuntime,
        snapshot: SourceSnapshot,
        target: Target,
        baseline_recipe: Recipe,
        *,
        max_diagnostics: int,
        wall_time_seconds: int = 120,
        binding: ControllerBinding | None = None,
    ) -> None:
        if type(max_diagnostics) is not int or not 1 <= max_diagnostics <= 8:
            raise ValueError("max_diagnostics must be between one and eight")
        if type(wall_time_seconds) is not int or not 1 <= wall_time_seconds <= 600:
            raise ValueError("investigation wall time must be between one and 600 seconds")
        if baseline_recipe.steps[0].argv[:2] != ("npm", "ci"):
            raise ValueError("investigation requires the approved baseline install step")
        expected_os, expected_architecture = target.runtime.platform.split("/", 1)
        if (
            runtime.image.supplied_reference != target.runtime.image_ref
            or runtime.image.os != expected_os
            or runtime.image.architecture != expected_architecture
        ):
            raise ValueError("prepared runtime is not bound to the selected target")
        self.binding = binding or ControllerBinding(
            run_id=uuid.uuid4(),
            attempt_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
        )
        self._base_runtime = runtime
        self._runtime = replace(
            runtime,
            runner=_DeadlineDockerCliRunner(
                runtime.runner,
                time.monotonic() + wall_time_seconds,
                budget_name="phase",
            ),
        )
        self._snapshot = snapshot
        self._target = target
        self._install_step = baseline_recipe.steps[0]
        self._max_diagnostics = max_diagnostics
        self._allowed_scripts = _immutable_package_scripts(snapshot)
        self._created: list[tuple[str, str]] = []
        self._app_id: str | None = None
        self._workspace_created = False
        self._marker_digest: ContentDigest | None = None
        self._setup: CommandEvidence | None = None
        self._diagnostics: list[CommandEvidence] = []
        self._errors: list[str] = []
        self._blocking_outcome: Outcome | None = None
        self._closed = False
        self._evidence: InvestigationEvidence | None = None

    def open(self) -> None:
        """Eagerly establish fresh mutable state before the agent may inspect it."""

        self._ensure_open()

    def run_package_script(self, script_name: str) -> CommandEvidence:
        """Run one immutable package script in the case-bound investigation sandbox."""

        if self._closed:
            raise WorkerProblem(Outcome.POLICY_BLOCKED, "investigation session is closed")
        if _endpoint_is_quarantined(self._base_runtime.docker_endpoint):
            raise WorkerProblem(
                Outcome.CLEANUP_FAILED,
                "M2 worker endpoint is quarantined after an earlier cleanup failure",
            )
        if script_name not in self._allowed_scripts:
            raise WorkerProblem(
                Outcome.POLICY_BLOCKED,
                "diagnostic script is not declared by immutable package.json",
            )
        if len(self._diagnostics) >= self._max_diagnostics:
            raise WorkerProblem(Outcome.POLICY_BLOCKED, "diagnostic command budget is exhausted")
        try:
            self._ensure_open()
            if self._setup is None or not self._setup.succeeded:
                raise WorkerProblem(
                    Outcome.FAILED,
                    "approved install step did not complete in the investigation sandbox",
                )
            step = RecipeStep(
                id=f"diagnostic_{len(self._diagnostics) + 1}",
                argv=("npm", "run", script_name),
                cwd=".",
                timeout_seconds=60,
            )
            assert self._app_id is not None
            result = _execute_foreground(self._runtime, self._app_id, step)
        except WorkerProblem as exc:
            if exc.outcome in {
                Outcome.INFRASTRUCTURE_ERROR,
                Outcome.TIMED_OUT,
                Outcome.CLEANUP_FAILED,
            }:
                self._blocking_outcome = exc.outcome
            raise
        self._diagnostics.append(result)
        if result.outcome in {Outcome.INFRASTRUCTURE_ERROR, Outcome.TIMED_OUT}:
            self._blocking_outcome = result.outcome
        return result

    def close(self) -> InvestigationEvidence:
        """Remove only resources owned by this investigation and freeze its evidence."""

        if self._evidence is not None:
            return self._evidence
        self._closed = True
        cleanup_runtime = replace(
            self._base_runtime,
            runner=_DeadlineDockerCliRunner(
                self._base_runtime.runner,
                time.monotonic()
                + CONTROLLED_NODE_FIXTURE_POLICY.docker.cleanup_wall_time_seconds,
                budget_name="cleanup",
            ),
        )
        cleanup_errors, removed, recovered = _cleanup(
            cleanup_runtime, self.binding, self._created
        )
        self._app_id = self._app_id or recovered.get("app")
        app_removed = self._app_id is not None and self._app_id in removed
        workspace_removed = self._workspace_created and app_removed
        if cleanup_errors:
            _quarantine_endpoint(self._base_runtime.docker_endpoint)
        errors = tuple(_bounded_error(value) for value in cleanup_errors[:32])
        cleanup = CleanupEvidence(
            attempted=True,
            app_container_created=self._app_id is not None,
            app_container_removed=app_removed,
            verifier_container_created=False,
            verifier_container_removed=False,
            workspace_created=self._workspace_created,
            workspace_removed=workspace_removed,
            run_owned_resources_only=True,
            worker_quarantined=bool(errors),
            sanitized_errors=errors,
        )
        outcome = Outcome.PASSED
        if errors:
            outcome = Outcome.CLEANUP_FAILED
        elif self._blocking_outcome is not None:
            outcome = self._blocking_outcome
        elif self._errors:
            outcome = Outcome.INFRASTRUCTURE_ERROR
        elif self._setup is not None and not self._setup.succeeded:
            outcome = self._setup.outcome
        else:
            blocking_diagnostic = next(
                (
                    command
                    for command in self._diagnostics
                    if command.outcome
                    in {Outcome.INFRASTRUCTURE_ERROR, Outcome.TIMED_OUT}
                ),
                None,
            )
            if blocking_diagnostic is not None:
                outcome = blocking_diagnostic.outcome
        self._evidence = InvestigationEvidence(
            binding=self.binding,
            app_container_id=self._app_id,
            workspace_marker_digest=self._marker_digest,
            setup_command=self._setup,
            diagnostics=tuple(self._diagnostics),
            cleanup=cleanup,
            outcome=outcome,
            sanitized_errors=tuple(self._errors[:32]) + errors,
        )
        return self._evidence

    def _ensure_open(self) -> None:
        if self._app_id is not None:
            return
        try:
            self._app_id = _create_app(self._runtime, self.binding)
            self._created.append((self._app_id, "app"))
            _assert_container(
                self._runtime,
                self._app_id,
                self.binding,
                "app",
                "none",
                _IDLE_SUPERVISOR,
                None,
            )
            _start_container(self._runtime, self._app_id)
            self._workspace_created = True
            self._marker_digest = _seed_fresh_workspace(
                self._runtime, self._app_id, self.binding
            )
            _write_workspace(self._runtime, self._snapshot.files, self._app_id)
            self._setup = _execute_foreground(
                self._runtime, self._app_id, self._install_step
            )
            if self._setup.outcome is Outcome.FAILED:
                return
            if not self._setup.succeeded:
                raise WorkerProblem(
                    self._setup.outcome,
                    "approved install step failed in the investigation sandbox",
                )
        except WorkerProblem as exc:
            self._errors.append(_bounded_error(str(exc)))
            self._blocking_outcome = (
                exc.outcome
                if exc.outcome
                in {
                    Outcome.INFRASTRUCTURE_ERROR,
                    Outcome.TIMED_OUT,
                    Outcome.CLEANUP_FAILED,
                }
                else Outcome.INFRASTRUCTURE_ERROR
            )
            raise
        except Exception as exc:
            self._errors.append("unexpected trusted investigation failure")
            self._blocking_outcome = Outcome.INFRASTRUCTURE_ERROR
            raise WorkerProblem(
                Outcome.INFRASTRUCTURE_ERROR,
                "unexpected trusted investigation failure",
            ) from exc


def _immutable_package_scripts(snapshot: SourceSnapshot) -> frozenset[str]:
    try:
        package = _load_unique_json(snapshot.content("package.json"))
    except (SourcePolicyError, UnicodeError, ValueError) as exc:
        raise ValueError("pinned package.json is invalid") from exc
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if not isinstance(scripts, dict) or any(
        not isinstance(name, str) or not isinstance(command, str)
        for name, command in scripts.items()
    ):
        raise ValueError("pinned package scripts are invalid")
    return frozenset(scripts)


def _bounded_error(value: str) -> str:
    return value.replace("\x00", "?").replace("\r", " ").strip()[:4096]


__all__ = ["DockerInvestigationSession", "InvestigationEvidence"]
