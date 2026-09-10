"""Durable, selected-repository GitHub workflow; no repository code runs here.

Only this trusted worker consumes signed events. Publication is a write-ahead
state machine. A Git object can be re-created by content identity; an uncertain
PR/check/ref POST is only reconciled, never blindly repeated.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode
from uuid import uuid4

from firstrun.artifacts import ArtifactStore

from firstrun.domain.contracts import Recipe
from firstrun.domain.evidence import CleanupEvidence, ContentDigest, RunEvidence
from firstrun.domain.repair import AgentRunEvidence, RepairProviderConfig
from firstrun.github_state import GitHubConfig, RepositoryRegistration, SQLiteStore
from firstrun.integrations.github import (
    GitHubAppConfig, GitHubClient, GitHubError, fetch_snapshot, protected_digest,
)
from firstrun.orchestration.repair import repair_snapshot, verify_snapshot
from firstrun.verification.readme import check_block_bytes, replace_block_bytes
from firstrun.verification.source import (
    CandidatePatch, SourcePolicyError, SourceSnapshot, build_candidate_patch,
)


class PublicationBlocked(RuntimeError):
    """A read-back or proof failed to establish the required authority."""


class ReconciliationPending(RuntimeError):
    """A possibly completed non-idempotent write needs read-only reconciliation."""


class CancellationRequested(RuntimeError):
    """The persisted stop flag is observed at a safe controller boundary."""


def make_client(config: GitHubConfig) -> GitHubClient:
    r = config.registration
    return GitHubClient(GitHubAppConfig(
        app_id=r.app_id, installation_id=r.installation_id,
        repository_id=r.repository_id, owner=r.owner, name=r.name,
        private_key_path=config.private_key_path,
    ))


def process_one(
    config: GitHubConfig, store: SQLiteStore, provider: RepairProviderConfig,
    *, artifact_root: Path,
) -> dict[str, Any]:
    """Claim one durable case. Run from an isolated, local worker process only."""

    owner = str(uuid4())  # Unique claim fencing token, never a reusable worker name.
    case = store.claim(owner, seconds=1800)
    if case is None:
        return {"state": "idle"}
    case_id = case["id"]
    registration = config.registration
    client = make_client(config)
    artifacts = ArtifactStore(artifact_root)
    publisher = Publisher(client, store, registration, case_id, owner, artifacts)
    execution_started = False
    cleanup_confirmed = True

    def check_cancel() -> None:
        current = store.get_case(case_id)
        if current and current["payload"].get("cancellation_requested"):
            raise CancellationRequested()

    def finish_cancel() -> None:
        store.finish(case_id, owner, "cancelled" if cleanup_confirmed else "interrupted", {
            "message": ("Cancelled at a safe phase boundary; run-owned cleanup confirmed."
                        if cleanup_confirmed else
                        "Cancellation requested; cleanup is unconfirmed and execution is quarantined.")
        })

    def observe(event: str, payload: dict[str, object]) -> None:
        nonlocal execution_started, cleanup_confirmed
        if event.endswith("_running"):
            # Before the observer returns no resources for this phase exist.
            check_cancel()
            cleanup_confirmed = False
        elif event.endswith("_completed"):
            cleanup_confirmed = _completed_cleanup_succeeded(payload)
        execution_started = True
        # Event namespaces cannot bypass the conservative restart quarantine.
        phase = "investigating" if "investigation" in event else "proof_running"
        if "baseline" in event:
            phase = "baseline_running"
        store.heartbeat(case_id, owner, seconds=1800)
        reference = artifacts.put(payload)
        store.update_case(case_id, owner, phase, {"worker_event": event,
                         "worker_binding": payload.get("binding"), "worker_artifact": reference})
        check_cancel()

    try:
        if case["registration"] != registration.model_dump(mode="json"):
            raise PublicationBlocked("Case registration changed; create a new approved case")
        if publisher.stale():
            return store.get_case(case_id)
        snapshot = fetch_snapshot(client, case["sha"], registration.source_prefix)
        validate_registration_source(client, registration, snapshot)
        check_cancel()
        payload = case["payload"]
        if "repair_artifact" not in payload:
            store.update_case(case_id, owner, "baseline_running", {"source": {
                "sha": snapshot.base_commit, "git_tree": snapshot.base_tree,
                "source_tree": snapshot.source_tree,
                "archive_digest": snapshot.archive_digest,
                "content_tree_digest": snapshot.content_tree_digest,
            }})
            execution_started = True
            result = repair_snapshot(
                snapshot, provider, observer=observe,
                expected_runtime_digest=registration.runtime_image_id,
                expected_runtime_repository_digest=registration.runtime_digest,
            )
            payload = {"repair_artifact": artifacts.put(result.to_dict())}
            store.update_case(case_id, owner, "publishing", payload)
            if result.outcome.value == "cleanup_failed":
                store.finish(case_id, owner, "interrupted", {"message": "Cleanup failed; repository execution is quarantined."})
                return store.get_case(case_id)
            check_cancel()
            if result.baseline is not None:
                store.set_health(registration.repository_id,
                                 checked_sha=snapshot.base_commit,
                                 outcome=result.baseline.outcome.value)
        repair = artifacts.get(payload["repair_artifact"])
        check_cancel()
        if publisher.stale():
            return store.get_case(case_id)
        if repair["state"] != "repair_ready":
            baseline = repair.get("baseline")
            if baseline and registration.automatic_prs:
                observed = RunEvidence.model_validate_json(json.dumps(baseline))
                publisher.publish_check(observed, "default", None)
            store.finish(case_id, owner, repair["state"], payload)
        else:
            publisher.publish(snapshot, repair, observe)
        return store.get_case(case_id)
    except CancellationRequested:
        # A running phase is bounded, and its existing worker owns cleanup. The
        # HTTP process never gets daemon access and never kills unrelated work.
        # Do not relabel unknown/failed cleanup as successful cancellation.
        finish_cancel()
    except ReconciliationPending:
        store.finish(case_id, owner, "reconcile_pending", {
            "message": "A write may have completed. Retry the worker for read-only reconciliation."
        })
    except GitHubError as exc:
        # Client errors contain only controller-selected codes, never response bodies.
        phase = "reconcile_pending" if store.get_case(case_id)["phase"] == "publishing" else "blocked"
        store.finish(case_id, owner, phase, {"message": str(exc)})
    except (PublicationBlocked, SourcePolicyError) as exc:
        if store.get_case(case_id)["payload"].get("cancellation_requested"):
            finish_cancel()
        else:
            store.finish(case_id, owner, "blocked", {"message": str(exc)[:512]})
    except Exception:
        # Unknown failures during execution must keep the repo quarantined, including
        # failures in the persistence observer. Never assume cleanup from an exception.
        store.finish(case_id, owner, "interrupted" if execution_started else "blocked", {
            "message": "Worker interrupted; inspect persisted phase/resources before recovery."
        })
    return store.get_case(case_id)


def _completed_cleanup_succeeded(payload: dict[str, object]) -> bool:
    cleanup = payload.get("cleanup")
    for key in ("evidence", "attempt"):
        evidence = payload.get(key)
        if isinstance(evidence, dict) and isinstance(evidence.get("cleanup"), dict):
            cleanup = evidence["cleanup"]
            break
    if not isinstance(cleanup, dict):
        return False
    try:
        return CleanupEvidence.model_validate_json(json.dumps(cleanup)).succeeded
    except ValueError:
        return False


def validate_registration_source(
    client: GitHubClient, registration: RepositoryRegistration, snapshot: SourceSnapshot,
) -> None:
    """Approval pins both the immutable target/code and the initial setup recipe."""
    approved = fetch_snapshot(client, registration.approved_sha, registration.source_prefix)
    if str(ContentDigest.from_bytes(approved.content(".firstrun/recipe.json"))) != registration.approved_recipe_digest:
        raise PublicationBlocked("Initial recipe differs from the owner-approved digest")
    from firstrun.worker.docker import TRUSTED_VERIFIER_DIGEST
    from firstrun.verification.policy import CONTROLLED_NODE_FIXTURE_POLICY, CONTROLLED_NODE_FIXTURE_POLICY_DIGEST
    if registration.verifier_digest != str(TRUSTED_VERIFIER_DIGEST):
        raise PublicationBlocked("Registered verifier differs from the controller verifier")
    if (registration.policy_digest != str(CONTROLLED_NODE_FIXTURE_POLICY_DIGEST)
            or registration.policy_revision != CONTROLLED_NODE_FIXTURE_POLICY.revision):
        raise PublicationBlocked("Registered policy differs from the controller policy")
    for source in (approved, snapshot):
        if protected_digest(source.files) != registration.protected_digest:
            raise PublicationBlocked("Protected source changed; new human approval required")
        if str(ContentDigest.from_bytes(source.content(".firstrun/target.json"))) != registration.target_digest:
            raise PublicationBlocked("Target changed; new human approval required")


class Publisher:
    """Private controller publisher. It accepts persisted controller evidence only."""

    def __init__(self, client: GitHubClient, store: SQLiteStore,
                 registration: RepositoryRegistration, case_id: str, owner: str,
                 artifacts: ArtifactStore | None = None):
        self.client, self.store, self.registration = client, store, registration
        self.case_id, self.owner = case_id, owner
        self.artifacts = artifacts
        self.prefix = f"/repos/{registration.owner}/{registration.name}"

    @property
    def case(self) -> dict[str, Any]:
        return self.store.get_case(self.case_id)

    def stale(self) -> bool:
        head = self.client.head(self.registration.branch)
        self.store.set_health(self.registration.repository_id, head=head)
        if head == self.case["sha"]:
            return False
        next_case = self.store.enqueue(self.registration, head)
        self.store.finish(self.case_id, self.owner, "stale", {
            "current_head": head, "replacement_case_id": next_case,
            "message": "Branch moved; previous proof is historical and a new case is queued.",
        })
        return True

    def publish(self, snapshot: SourceSnapshot, repair: dict[str, Any],
                observer: Callable[[str, dict[str, object]], None]) -> None:
        if self.artifacts is None:
            raise PublicationBlocked("Publication requires the trusted artifact store")
        if not self.registration.automatic_prs:
            self.store.finish(self.case_id, self.owner, "repair_ready", {
                "message": "Verified candidate retained; automatic PR permission is disabled."
            })
            return
        candidate, previous_proof = validate_repair(snapshot, repair)
        if self.stale():
            return
        self.store.update_case(self.case_id, self.owner, "publishing")
        files = dict(snapshot.files)
        files.update(candidate.replacements)
        git_tree = self._candidate_tree(snapshot, candidate, files)
        commit = self._candidate_commit(git_tree)
        exact = fetch_snapshot(self.client, commit, self.registration.source_prefix)
        if (exact.base_tree != git_tree or exact.files != files
                or exact.content_tree_digest != candidate.candidate_tree_digest):
            raise PublicationBlocked("Published Git object differs from the candidate proved")
        self.store.update_case(self.case_id, self.owner, "publishing", {
            "repair_commit": commit, "repair_git_tree": git_tree,
            "candidate_digest": candidate.patch_digest,
        })
        existing = self.case["payload"].get("exact_commit_proof_artifact")
        if existing is None:
            # This is an additional fresh no-LLM run of the *actual* Git commit.
            result = verify_snapshot(exact, observer=observer,
                                     expected_runtime_digest=self.registration.runtime_image_id,
                                     expected_runtime_repository_digest=self.registration.runtime_digest)
            if result.outcome.value == "cleanup_failed":
                # process_one's interrupted path retains a durable repository quarantine.
                raise RuntimeError("Exact-commit worker cleanup failed")
            if result.evidence is None or result.outcome.value != "passed" or result.error is not None:
                self.store.update_case(self.case_id, self.owner, "publishing", {
                    "exact_commit_attempt_artifact": self.artifacts.put(result.attempt.model_dump(mode="json")) if result.attempt else None,
                })
                raise PublicationBlocked("Actual repair commit did not produce complete proof")
            proof = result.evidence
            validate_exact_proof(exact, proof, previous_proof, repair)
            self.store.update_case(self.case_id, self.owner, "publishing", {
                "exact_commit_proof_artifact": self.artifacts.put(proof.model_dump(mode="json")),
            })
        else:
            proof = RunEvidence.model_validate_json(json.dumps(self.artifacts.get(existing)))
            validate_exact_proof(exact, proof, previous_proof, repair)
        if self.stale():
            return
        branch = f"firstrun/{self.case_id[:20]}-{candidate.patch_digest[7:19]}"
        self._write("branch", "POST", self.prefix + "/git/refs",
                    {"ref": "refs/heads/" + branch, "sha": commit},
                    lambda: self._find_branch(branch, commit))
        check = self.publish_check(proof, "candidate", candidate.patch_digest)
        pr_body = self._pr_body(snapshot, candidate, proof, repair, check)
        pr = self._write("pull", "POST", self.prefix + "/pulls", {
            "title": "fix: restore the declared first-run setup",
            "head": branch, "base": self.registration.branch,
            "body": pr_body, "maintainer_can_modify": False,
        }, lambda: self._find_pull(branch, commit, candidate))
        if self.stale():
            return
        # Explicit fresh read-back even when the journal was already confirmed.
        verified_pr = self._read_pull(pr["number"], branch, commit, candidate)
        self.store.finish(self.case_id, self.owner, "pr_open", {
            "pr_number": verified_pr["number"], "pr_url": verified_pr["html_url"],
            "checked_sha": commit, "check_url": check["html_url"],
            "message": "Verified candidate PR opened. Default-branch health is unchanged.",
        })

    def _get(self, path: str) -> Any | None:
        try:
            return self.client.request("GET", path)
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    def _write(self, key: str, method: str, path: str, body: dict[str, Any],
               discover: Callable[[], dict[str, Any] | None], *, content_addressed: bool = False) -> dict[str, Any]:
        if self.case["payload"].get("cancellation_requested"):
            raise CancellationRequested()
        if not self.registration.automatic_prs:
            raise PublicationBlocked("External writes are disabled")
        # Discover BEFORE intent creation distinguishes first dispatch from restart.
        found = discover()
        record = self.store.journal_intent(self.case_id, self.owner, key, method, path, body)
        if found is not None:
            self.store.journal_result(self.case_id, self.owner, key, found)
            return found
        if record.get("state") == "confirmed":
            raise PublicationBlocked("Previously confirmed external object no longer matches")
        if not record.get("created", False) and not content_addressed:
            raise ReconciliationPending("Write outcome remains uncertain")
        self.store.heartbeat(self.case_id, self.owner, seconds=1800)
        try:
            self.client.request(method, path, body)
        except GitHubError:
            # Even a response error can follow a successful upstream write.
            found = discover()
            if found is None:
                raise ReconciliationPending("Write requires read-back") from None
        else:
            found = discover()
        if found is None:
            raise ReconciliationPending("Write not yet visible in read-back")
        self.store.journal_result(self.case_id, self.owner, key, found)
        return found

    def _object(self, kind: str, sha: str, body: dict[str, Any]) -> dict[str, Any]:
        path = self.prefix + "/git/" + kind

        def discover() -> dict[str, Any] | None:
            result = self._get(path + "/" + sha)
            if result is not None and result.get("sha") != sha:
                raise PublicationBlocked("Git object read-back identity mismatch")
            # Stable bounded journal result, no timestamps/URLs from the API.
            return {"sha": sha} if result is not None else None

        return self._write(kind + ":" + sha, "POST", path, body, discover, content_addressed=True)

    def _candidate_tree(self, snapshot: SourceSnapshot, candidate: CandidatePatch,
                        files: Mapping[str, bytes]) -> str:
        changed = [{"path": path, "mode": "100644", "type": "blob", "content": data.decode("utf-8")}
                   for path, data in sorted(candidate.replacements.items())]
        subtree = git_files_tree(files)
        self._object("trees", subtree, {"base_tree": snapshot.source_tree, "tree": changed})
        parent = snapshot.base_tree
        ancestors: list[tuple[str, str, list[dict[str, Any]]]] = []
        for part in filter(None, self.registration.source_prefix.split("/")):
            tree = self.client.request("GET", self.prefix + "/git/trees/" + parent)
            entries = validate_tree(tree, parent)
            selected = next((item for item in entries if item["path"] == part), None)
            if selected is None or selected["type"] != "tree":
                raise PublicationBlocked("Source prefix ancestor changed")
            ancestors.append((parent, part, entries))
            parent = selected["sha"]
        if parent != snapshot.source_tree:
            raise PublicationBlocked("Source tree is not part of the pinned commit")
        for old_sha, name, entries in reversed(ancestors):
            updated = [dict(item, sha=subtree) if item["path"] == name else item for item in entries]
            new_sha = git_tree_id(updated)
            self._object("trees", new_sha, {"base_tree": old_sha, "tree": [
                {"path": name, "mode": "040000", "type": "tree", "sha": subtree},
            ]})
            subtree = new_sha
        return subtree

    def _candidate_commit(self, tree: str) -> str:
        registration = self.registration
        created = int(self.case["created_at"])
        timestamp = datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        identity = {"name": registration.author_name, "email": registration.author_email, "date": timestamp}
        message = f"fix: restore first-run setup\n\nFirstRun case {self.case_id}\n"
        author = f"{registration.author_name} <{registration.author_email}> {created} +0000"
        raw = (f"tree {tree}\nparent {self.case['sha']}\nauthor {author}\ncommitter {author}\n\n" + message).encode()
        sha = git_object_id("commit", raw)
        self._object("commits", sha, {"tree": tree, "parents": [self.case["sha"]],
                                     "message": message, "author": identity, "committer": identity})
        observed = self.client.request("GET", self.prefix + "/git/commits/" + sha)
        if (observed.get("tree", {}).get("sha") != tree
                or [p.get("sha") for p in observed.get("parents", [])] != [self.case["sha"]]):
            raise PublicationBlocked("Repair commit tree or parent mismatch")
        return sha

    def _find_branch(self, branch: str, commit: str) -> dict[str, Any] | None:
        result = self._get(self.prefix + "/git/ref/heads/" + quote(branch, safe="/"))
        if result is None:
            return None
        if result.get("object", {}).get("sha") != commit or result.get("ref") != "refs/heads/" + branch:
            raise PublicationBlocked("Stable repair branch points to different content")
        return {"sha": commit, "ref": "refs/heads/" + branch}

    def _find_pull(self, branch: str, commit: str, candidate: CandidatePatch) -> dict[str, Any] | None:
        query = urlencode({"state": "all", "head": f"{self.registration.owner}:{branch}",
                           "base": self.registration.branch, "per_page": 100})
        results = self.client.request("GET", self.prefix + "/pulls?" + query)
        if not isinstance(results, list) or len(results) >= 100 or len(results) > 1:
            raise PublicationBlocked("Repair PR lookup is ambiguous")
        return self._read_pull(results[0]["number"], branch, commit, candidate) if results else None

    def _read_pull(self, number: int, branch: str, commit: str, candidate: CandidatePatch) -> dict[str, Any]:
        if type(number) is not int or number <= 0:
            raise PublicationBlocked("Invalid PR identity")
        pr = self.client.request("GET", self.prefix + f"/pulls/{number}")
        head, base = pr.get("head", {}), pr.get("base", {})
        if (head.get("sha") != commit or head.get("ref") != branch
                or head.get("repo", {}).get("id") != self.registration.repository_id
                or base.get("repo", {}).get("id") != self.registration.repository_id
                or base.get("ref") != self.registration.branch):
            raise PublicationBlocked("PR head or repository differs from the exact verified candidate")
        if pr.get("state") != "open" or pr.get("merged"):
            raise PublicationBlocked("Existing repair PR is closed; a duplicate will not be opened")
        files = self.client.request("GET", self.prefix + f"/pulls/{number}/files?per_page=100")
        expected = {(self.registration.source_prefix + "/" if self.registration.source_prefix else "") + path:
                    git_object_id("blob", data) for path, data in candidate.replacements.items()}
        if (not isinstance(files, list) or len(files) != len(expected)
                or {item.get("filename"): item.get("sha") for item in files} != expected
                or any(item.get("status") != "modified" for item in files)):
            raise PublicationBlocked("PR changed files do not match the allowlisted candidate")
        # UI values are reconstructed from configured identities, never arbitrary URLs.
        return {"number": number, "html_url": f"https://github.com/{self.registration.owner}/{self.registration.name}/pull/{number}",
                "head_sha": commit}

    def publish_check(self, evidence: RunEvidence, lane: str, candidate_digest: str | None) -> dict[str, Any]:
        if lane not in {"candidate", "default"}:
            raise PublicationBlocked("Unknown check lane")
        if lane == "candidate" and not evidence.verified:
            raise PublicationBlocked("A candidate check requires independent functional proof")
        if lane == "default" and evidence.base_commit != self.case["sha"]:
            raise PublicationBlocked("Default branch check must use the exact baseline SHA")
        external_id = f"firstrun:{self.case_id}:{lane}:{evidence.base_commit}"
        name = f"FirstRun / {lane}"
        conclusion = "success" if evidence.verified else {
            "failed": "failure", "timed_out": "timed_out",
        }.get(evidence.outcome.value, "action_required")
        summary = json.dumps({
            "case_id": self.case_id, "tested_sha": evidence.base_commit,
            "tested_git_tree": evidence.base_git_tree,
            "content_tree": str(evidence.base_tree_digest), "candidate_digest": candidate_digest,
            "target": str(evidence.target_digest), "recipe": str(evidence.recipe_digest),
            "verifier": str(evidence.verifier_digest), "policy": str(evidence.policy_digest),
            "runtime": evidence.app_runtime.model_dump(mode="json"),
            "commands": [list(c.argv) for c in evidence.commands],
            "functional_acceptance": evidence.acceptance_probe.outcome.value,
            "fresh_state": evidence.fresh_state.satisfied, "cleanup": evidence.cleanup.succeeded,
            "run_id": str(evidence.run_id),
            "scope": "controlled Linux Node/npm fixture; no cloud/private services; no auto-merge",
        }, indent=2)
        body = {"name": name, "head_sha": evidence.base_commit, "external_id": external_id,
                "status": "completed", "conclusion": conclusion,
                "output": {"title": "Verified candidate" if lane == "candidate" else "Default branch first-run result",
                           "summary": "Controller proof at the exact revision:\n```json\n" + summary + "\n```"}}

        def discover() -> dict[str, Any] | None:
            found: list[dict[str, Any]] = []
            for page in range(1, 11):
                params = urlencode({"check_name": name, "filter": "all", "per_page": 100, "page": page})
                response = self.client.request("GET", self.prefix + f"/commits/{evidence.base_commit}/check-runs?" + params)
                runs = response.get("check_runs", [])
                found.extend(run for run in runs if run.get("external_id") == external_id
                             and run.get("app", {}).get("id") == self.registration.app_id)
                if len(runs) < 100:
                    break
            else:
                raise PublicationBlocked("Check lookup exceeded its bounded page limit")
            if len(found) > 1:
                raise PublicationBlocked("Duplicate check identity requires operator reconciliation")
            if not found:
                return None
            value = found[0]
            if (value.get("head_sha") != evidence.base_commit or value.get("conclusion") != conclusion
                    or value.get("status") != "completed" or value.get("output", {}).get("summary") != body["output"]["summary"]):
                raise PublicationBlocked("Check read-back differs from the exact proof")
            check_id = value.get("id")
            if type(check_id) is not int or check_id <= 0:
                raise PublicationBlocked("Invalid check identity")
            return {"id": check_id, "head_sha": evidence.base_commit,
                    "html_url": f"https://github.com/{self.registration.owner}/{self.registration.name}/runs/{check_id}"}

        return self._write("check:" + lane, "POST", self.prefix + "/check-runs", body, discover)

    def _pr_body(self, snapshot: SourceSnapshot, candidate: CandidatePatch, proof: RunEvidence,
                 repair: dict[str, Any], check: dict[str, Any]) -> str:
        baseline = repair.get("baseline") or repair.get("baseline_attempt") or {}
        logs = []
        for command in baseline.get("commands", []):
            for key in ("sanitized_stdout_tail", "sanitized_stderr_tail"):
                if command.get(key):
                    logs.append(command[key])
        observation = html.escape("\n".join(logs)[-1800:]).replace("@", "&#64;").replace("`", "&#96;")
        return (f"## Verified candidate\n\nThe declared newcomer setup failed at `{snapshot.base_commit}`.\n\n"
                f"Observed baseline outcome: `{baseline.get('outcome', 'unknown')}`.\n\n"
                f"<pre>{observation or 'See the controller evidence for the failing functional probe.'}</pre>\n\n"
                "Changes are limited to the executable recipe and its deterministically rendered README setup block.\n\n"
                "A separate fresh run of the actual repair commit passed the controller-owned create/read probe.\n\n"
                f"- Tested commit: `{proof.base_commit}`\n- Git tree: `{proof.base_git_tree}`\n"
                f"- Patch: `{candidate.patch_digest}`\n- Content tree: `{candidate.candidate_tree_digest}`\n"
                f"- Target: `{proof.target_digest}`\n- Verifier: `{proof.verifier_digest}`\n"
                f"- Runtime: `{proof.app_runtime.image_id}`\n"
                f"- [Controller proof summary]({check['html_url']})\n\n"
                "Scope: controlled Linux Node/npm fixture; no private-service or production validation. "
                "Default-branch health remains separate until that branch is freshly checked after merge. "
                "FirstRun never merges this PR.\n")


def validate_repair(snapshot: SourceSnapshot, repair: dict[str, Any]) -> tuple[CandidatePatch, RunEvidence]:
    agent = AgentRunEvidence.model_validate_json(json.dumps(repair.get("agent")))
    if not agent.milestone_eligible or repair.get("source_revision") != snapshot.base_commit:
        raise PublicationBlocked("Publication requires completed live Strands evidence at the pinned base")
    raw = repair.get("candidate") or {}
    candidate = build_candidate_patch(snapshot, {path: text.encode("utf-8") for path, text in raw.get("replacements", {}).items()})
    if candidate.patch_digest != raw.get("patch_digest") or candidate.candidate_tree_digest != raw.get("candidate_tree_digest"):
        raise PublicationBlocked("Persisted candidate bytes and digests differ")
    recipe_bytes = candidate.replacements.get(".firstrun/recipe.json", snapshot.content(".firstrun/recipe.json"))
    recipe = Recipe.model_validate_json(recipe_bytes)
    readme = candidate.replacements.get("README.md", snapshot.content("README.md"))
    if readme != replace_block_bytes(snapshot.content("README.md"), recipe):
        raise PublicationBlocked("Candidate edits README outside its managed block")
    check_block_bytes(readme, recipe)
    proofs = [value.get("evidence") for value in repair.get("proofs", []) if value.get("evidence")]
    if not proofs:
        raise PublicationBlocked("Missing candidate proof")
    proof = RunEvidence.model_validate_json(json.dumps(proofs[-1]))
    if (not proof.verified or proof.phase != "proof" or proof.base_commit != snapshot.base_commit
            or str(proof.candidate_digest) != candidate.patch_digest
            or str(proof.candidate_tree_digest) != candidate.candidate_tree_digest
            or str(proof.base_tree_digest) != snapshot.content_tree_digest
            or str(proof.recipe_digest) != str(ContentDigest.from_bytes(recipe_bytes))):
        raise PublicationBlocked("Candidate proof does not bind the exact approved bytes")
    return candidate, proof


def validate_exact_proof(snapshot: SourceSnapshot, proof: RunEvidence, previous: RunEvidence,
                         repair: dict[str, Any]) -> None:
    if (not proof.verified or proof.base_commit != snapshot.base_commit
            or proof.base_git_tree != snapshot.base_tree
            or str(proof.base_tree_digest) != snapshot.content_tree_digest
            or str(proof.base_archive_digest) != snapshot.archive_digest):
        raise PublicationBlocked("Fresh exact-commit proof failed its binding predicate")
    for field in ("target_digest", "recipe_digest", "readme_digest", "verifier_digest", "policy_digest", "app_runtime", "verifier_runtime"):
        if getattr(proof, field) != getattr(previous, field):
            raise PublicationBlocked("Exact-commit proof changed a pinned contract or runtime")
    historical: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"run_id", "attempt_id", "workspace_id", "app_container_id", "verifier_container_id"} and item:
                    historical.add(str(item))
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(repair)
    current = {str(getattr(proof, key)) for key in ("run_id", "attempt_id", "workspace_id", "app_container_id", "verifier_container_id")}
    if current & historical:
        raise PublicationBlocked("Exact-commit proof reused earlier mutable execution identity")


def git_object_id(kind: str, content: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(content)}\0".encode("ascii") + content).hexdigest()


def git_tree_id(entries: list[dict[str, Any]]) -> str:
    ordered = sorted(entries, key=lambda item: (item["path"] + ("/" if item["type"] == "tree" else "")).encode("utf-8"))
    raw = b"".join(item["mode"].lstrip("0").encode("ascii") + b" " + item["path"].encode("utf-8")
                   + b"\0" + bytes.fromhex(item["sha"]) for item in ordered)
    return git_object_id("tree", raw)


def git_files_tree(files: Mapping[str, bytes]) -> str:
    nested: dict[str, Any] = {}
    for path, data in files.items():
        node = nested
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = data

    def visit(node: dict[str, Any]) -> str:
        return git_tree_id([{"path": name, "mode": "040000" if isinstance(value, dict) else "100644",
                             "type": "tree" if isinstance(value, dict) else "blob",
                             "sha": visit(value) if isinstance(value, dict) else git_object_id("blob", value)}
                            for name, value in node.items()])
    return visit(nested)


def validate_tree(value: dict[str, Any], expected_sha: str) -> list[dict[str, Any]]:
    entries = value.get("tree")
    if value.get("sha") != expected_sha or value.get("truncated") is not False or not isinstance(entries, list) or len(entries) > 10000:
        raise PublicationBlocked("Cannot inspect the entire pinned ancestor Git tree")
    result = []
    for item in entries:
        path, mode, kind, sha = (item.get(key) for key in ("path", "mode", "type", "sha"))
        if (not isinstance(path, str) or not path or path in {".", ".."}
                or any(c in path for c in "/\\\x00\r\n")
                or (mode, kind) not in {("040000", "tree"), ("100644", "blob"), ("100755", "blob"), ("120000", "blob"), ("160000", "commit")}
                or not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{40}", sha)):
            raise PublicationBlocked("Invalid ancestor Git tree metadata")
        result.append({"path": path, "mode": mode, "type": kind, "sha": sha})
    if len({item["path"] for item in result}) != len(result) or git_tree_id(result) != expected_sha:
        raise PublicationBlocked("Ancestor Git tree content hash mismatch")
    return result
