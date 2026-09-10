"""Focused offline tests for M4 durable decisions and safe web projections."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from firstrun.artifacts import ArtifactStore
from firstrun.domain.contracts import Recipe, sha256_content_ref
from firstrun.domain.evidence import ContentDigest, build_run_evidence
from firstrun.domain.web import DecisionRequest, StartRunRequest
from firstrun.github_state import (
    GitHubConfig,
    RepositoryRegistration,
    SQLiteStore,
    protected_source_digest,
)
from firstrun.verification.readme import replace_block_bytes
from firstrun.verification.source import (
    SourceSnapshot,
    build_candidate_patch,
    content_tree_digest,
)
from firstrun.web_service import (
    ConflictError,
    DataUnavailableError,
    FirstRunWebService,
    NotFoundError,
)
from unit.test_evidence import binding, passing_observation


SHA = "1" * 40
TREE = "2" * 40
SOURCE_TREE = "3" * 40
NEW_SHA = "4" * 40
OWNER = "11111111-1111-4111-8111-111111111111"


def digest(character: str) -> str:
    return "sha256:" + character * 64


def request_id(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def fixture_snapshot(sha: str = SHA) -> SourceSnapshot:
    fixture = Path("fixtures/notes-app").absolute()
    files = {
        path.relative_to(fixture).as_posix(): path.read_bytes()
        for path in fixture.rglob("*")
        if path.is_file()
    }
    return SourceSnapshot(
        repository_root=Path.cwd(),
        approved_repo_path=fixture,
        base_commit=sha,
        base_tree=TREE if sha == SHA else "5" * 40,
        source_tree=SOURCE_TREE,
        archive_digest=digest("d"),
        content_tree_digest=content_tree_digest(files),
        files=MappingProxyType(files),
    )


def registration(snapshot: SourceSnapshot) -> RepositoryRegistration:
    return RepositoryRegistration(
        app_id=11,
        installation_id=22,
        repository_id=33,
        owner="owner",
        name="repo",
        branch="main",
        approved_sha=SHA,
        source_prefix="fixtures/notes-app",
        target_digest=sha256_content_ref(snapshot.content(".firstrun/target.json")),
        approved_recipe_digest=sha256_content_ref(
            snapshot.content(".firstrun/recipe.json")
        ),
        protected_digest=protected_source_digest(snapshot.files),
        verifier_digest=digest("6"),
        policy_revision="m1-policy-v1",
        policy_digest=digest("7"),
        runtime_digest=digest("a"),
        runtime_image_id=digest("b"),
        author_name="FirstRun Test",
        author_email="private-author@example.invalid",
        automatic_prs=False,
    )


def baseline_evidence(snapshot: SourceSnapshot, *, stdout: str = ""):
    authority = binding()
    original = passing_observation(authority)
    commands = list(original.commands)
    commands[0] = commands[0].model_copy(
        update={"sanitized_stdout_tail": stdout}
    )
    observation = original.model_copy(
        update={
            "base_commit": snapshot.base_commit,
            "base_git_tree": snapshot.base_tree,
            "source_git_tree": snapshot.source_tree,
            "base_tree_digest": ContentDigest(snapshot.content_tree_digest),
            "base_archive_digest": ContentDigest(snapshot.archive_digest),
            "target_digest": ContentDigest(
                sha256_content_ref(snapshot.content(".firstrun/target.json"))
            ),
            "observed_target_digest": ContentDigest(
                sha256_content_ref(snapshot.content(".firstrun/target.json"))
            ),
            "recipe_digest": ContentDigest(
                sha256_content_ref(snapshot.content(".firstrun/recipe.json"))
            ),
            "executed_recipe_digest": ContentDigest(
                sha256_content_ref(snapshot.content(".firstrun/recipe.json"))
            ),
            "readme_digest": ContentDigest(
                sha256_content_ref(snapshot.content("README.md"))
            ),
            "rendered_readme_digest": ContentDigest(
                sha256_content_ref(snapshot.content("README.md"))
            ),
            "commands": tuple(commands),
        }
    )
    return build_run_evidence(binding=authority, observation=observation)


class WebServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.snapshot = fixture_snapshot()
        self.registration = registration(self.snapshot)
        self.config = GitHubConfig(
            registration=self.registration,
            private_key_path=root / "private-key.pem",
            webhook_secret_path=root / "webhook-secret",
        )
        self.store = SQLiteStore(root / "state.sqlite")
        self.artifacts = ArtifactStore(root / "artifacts")
        self.loaded: list[str] = []

        def source_loader(sha: str) -> SourceSnapshot:
            self.loaded.append(sha)
            if sha == SHA:
                return self.snapshot
            if sha == NEW_SHA:
                return fixture_snapshot(NEW_SHA)
            raise ValueError("unknown exact revision")

        self.head = SHA
        self.service = FirstRunWebService(
            self.config,
            self.store,
            source_loader,
            lambda: self.head,
            self.artifacts,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start(self, number: int = 1):
        return self.service.start_run(33, StartRunRequest(request_id=request_id(number)))

    def test_repository_detail_uses_exact_approved_source_without_private_config(self) -> None:
        detail = self.service.get_repository(33)

        self.assertEqual([SHA], self.loaded)
        self.assertEqual(self.registration.target_digest, detail.target_digest)
        self.assertIn("npm", detail.setup_instructions)
        rendered = detail.model_dump_json()
        self.assertNotIn("private-key", rendered)
        self.assertNotIn("private-author", rendered)
        with self.assertRaises(NotFoundError):
            self.service.get_repository(34)

    def test_manual_run_identity_and_case_pagination_are_durable(self) -> None:
        first = self.start(1)
        replay = self.start(1)
        second = self.start(2)

        self.assertEqual(first.id, replay.id)
        self.assertNotEqual(first.id, second.id)
        page = self.service.list_cases(33, limit=1)
        self.assertEqual(1, len(page.items))
        self.assertIsNotNone(page.next_cursor)
        following = self.service.list_cases(33, cursor=page.next_cursor, limit=1)
        self.assertEqual(1, len(following.items))
        self.assertNotEqual(page.items[0].id, following.items[0].id)

    def test_cancel_is_versioned_idempotent_and_honest_for_active_work(self) -> None:
        queued = self.start(1)
        decision = DecisionRequest(
            request_id=request_id(10),
            expected_version=queued.version,
            case_sha=queued.sha,
            action="cancel",
        )
        cancelled = self.service.decide(33, queued.id, decision)
        replay = self.service.decide(33, queued.id, decision)
        self.assertEqual("cancelled", cancelled.case.phase)
        self.assertEqual(cancelled, replay)
        with self.assertRaises(ConflictError):
            self.service.decide(
                33,
                queued.id,
                DecisionRequest(
                    request_id=request_id(11),
                    expected_version=queued.version,
                    case_sha=queued.sha,
                    action="cancel",
                ),
            )

        active = self.start(2)
        claimed = self.store.claim(OWNER, 1_200)
        self.assertEqual(active.id, claimed["id"])
        running = self.store.update_case(active.id, OWNER, "baseline_running")
        result = self.service.decide(
            33,
            active.id,
            DecisionRequest(
                request_id=request_id(12),
                expected_version=running["version"],
                case_sha=active.sha,
                action="cancel",
            ),
        )
        self.assertEqual("baseline_running", result.case.phase)
        self.assertTrue(result.case.cancellation_requested)

    def test_recheck_requires_current_version_and_creates_new_case(self) -> None:
        case = self.start(1)
        claimed = self.store.claim(OWNER, 1_200)
        self.assertEqual(case.id, claimed["id"])
        waiting = self.store.finish(case.id, OWNER, "needs_input")
        self.head = NEW_SHA
        request = DecisionRequest(
            request_id=request_id(20),
            expected_version=waiting["version"],
            case_sha=case.sha,
            action="recheck",
        )

        result = self.service.decide(33, case.id, request)
        replay = self.service.decide(33, case.id, request)
        self.assertEqual(result, replay)
        self.assertNotEqual(case.id, result.replacement_case_id)
        self.assertEqual(NEW_SHA, result.case.sha)

    def test_case_projection_redacts_logs_and_exposes_only_allowlisted_diff(self) -> None:
        case = self.start(1)
        recipe = Recipe.model_validate_json(
            self.snapshot.content(".firstrun/recipe.json")
        )
        changed_step = recipe.steps[0].model_copy(
            update={"argv": (*recipe.steps[0].argv, "--ignore-scripts")}
        )
        changed_recipe = recipe.model_copy(
            update={"steps": (changed_step, *recipe.steps[1:])}
        )
        recipe_bytes = (changed_recipe.model_dump_json(indent=2) + "\n").encode()
        readme_bytes = replace_block_bytes(
            self.snapshot.content("README.md"), changed_recipe
        )
        candidate = build_candidate_patch(
            self.snapshot,
            {
                ".firstrun/recipe.json": recipe_bytes,
                "README.md": readme_bytes,
            },
        )
        evidence = baseline_evidence(
            self.snapshot, stdout="install failed token=actual-secret-value"
        )
        repair = {
            "schema_version": 1,
            "state": "repair_ready",
            "outcome": "passed",
            "source_revision": SHA,
            "message": "private model reasoning must not render",
            "baseline": evidence.model_dump(mode="json"),
            "agent": {
                "provider_endpoint": "https://private-provider.invalid",
                "provider_config": {"credential": "do-not-render"},
                "tool_calls": [{"sanitized_result": "internal-tool-result"}],
            },
            "proofs": [],
            "candidate": {
                "patch_digest": candidate.patch_digest,
                "candidate_tree_digest": candidate.candidate_tree_digest,
                "replacements": {
                    path: content.decode("utf-8")
                    for path, content in candidate.replacements.items()
                },
            },
        }
        reference = self.artifacts.put(repair)
        claimed = self.store.claim(OWNER, 1_200)
        self.assertEqual(case.id, claimed["id"])
        self.store.finish(
            case.id,
            OWNER,
            "repair_ready",
            {"repair_artifact": reference},
        )

        detail = self.service.get_case(33, case.id)
        rendered = detail.model_dump_json()
        self.assertIn("[REDACTED]", detail.baseline.commands[0].stdout_tail)
        self.assertEqual(
            (".firstrun/recipe.json", "README.md"), detail.candidate.paths
        )
        self.assertNotIn("actual-secret-value", rendered)
        self.assertNotIn("private-provider", rendered)
        self.assertNotIn("internal-tool-result", rendered)
        self.assertNotIn("model reasoning", rendered)

    def test_proof_json_is_deserialized_and_rebound_to_exact_case(self) -> None:
        case = self.start(1)
        evidence = baseline_evidence(self.snapshot)
        repair = {
            "state": "blocked",
            "outcome": "failed",
            "source_revision": SHA,
            "baseline": evidence.model_dump(mode="json"),
            "proofs": [],
            "candidate": None,
            "agent": None,
        }
        reference = self.artifacts.put(repair)
        claimed = self.store.claim(OWNER, 1_200)
        self.store.finish(
            case.id, OWNER, "blocked", {"repair_artifact": reference}
        )
        self.assertEqual(SHA, self.service.get_baseline(33, case.id).tested_sha)

        wrong = evidence.model_copy(update={"base_commit": NEW_SHA})
        wrong_reference = self.artifacts.put(
            {**repair, "baseline": wrong.model_dump(mode="json")}
        )
        other = self.start(2)
        claimed = self.store.claim(
            "22222222-2222-4222-8222-222222222222", 1_200
        )
        self.assertEqual(other.id, claimed["id"])
        self.store.finish(
            other.id, claimed["lease_owner"], "blocked", {"repair_artifact": wrong_reference}
        )
        with self.assertRaises(DataUnavailableError):
            self.service.get_baseline(33, other.id)


if __name__ == "__main__":
    unittest.main()
