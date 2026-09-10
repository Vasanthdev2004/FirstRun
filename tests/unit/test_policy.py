from __future__ import annotations

import unittest

from pydantic import ValidationError

from firstrun.domain.outcomes import Outcome
from firstrun.verification.policy import (
    ApprovedCommand,
    CONTROLLED_NODE_FIXTURE_POLICY,
    CONTROLLED_NODE_FIXTURE_POLICY_DIGEST,
    ControlledNodeFixturePolicy,
    canonical_policy_json,
    classify_target_tuple,
    digest_policy,
)


class ControlledNodeFixturePolicyTests(unittest.TestCase):
    def test_policy_is_exact_frozen_and_contains_no_oracle_repair(self) -> None:
        policy = CONTROLLED_NODE_FIXTURE_POLICY

        self.assertEqual(policy.fixture_path, "fixtures/notes-app")
        self.assertEqual(policy.target_path, ".firstrun/target.json")
        self.assertEqual(policy.recipe_path, ".firstrun/recipe.json")
        self.assertEqual(policy.readme_path, "README.md")
        self.assertEqual(
            policy.baseline_commands[0].argv,
            ("npm", "ci", "--offline", "--no-audit", "--no-fund"),
        )
        self.assertEqual(policy.start_command.argv, ("npm", "run", "dev"))
        self.assertNotIn(b"db:migrate", canonical_policy_json(policy))
        with self.assertRaises(ValidationError):
            policy.revision = "changed"

        with self.assertRaises(ValidationError):
            ControlledNodeFixturePolicy(
                baseline_commands=(
                    ApprovedCommand(
                        step_id="install",
                        argv=("npm", "install"),
                        cwd=".",
                        timeout_seconds=60,
                    ),
                ),
                start_command=policy.start_command,
            )

    def test_canonical_policy_digest_is_stable(self) -> None:
        self.assertEqual(
            str(CONTROLLED_NODE_FIXTURE_POLICY_DIGEST),
            "sha256:75d96f29b3f11efabdeb7fc2781662af36327b190c13601900b2bfcd9b195c53",
        )
        self.assertEqual(
            digest_policy(CONTROLLED_NODE_FIXTURE_POLICY),
            CONTROLLED_NODE_FIXTURE_POLICY_DIGEST,
        )
        encoded = canonical_policy_json(CONTROLLED_NODE_FIXTURE_POLICY)
        self.assertNotIn(b" ", encoded)
        self.assertNotIn(b"\n", encoded)

    def test_only_registered_schema_valid_target_tuple_is_supported(self) -> None:
        policy = CONTROLLED_NODE_FIXTURE_POLICY
        self.assertIs(
            classify_target_tuple(
                image_reference=policy.runtime_image_reference,
                platform=policy.platform,
                verifier_id=policy.verifier_id,
            ),
            Outcome.PASSED,
        )
        for changed in (
            {"image_reference": "node:23-bookworm-slim"},
            {"platform": "linux/arm64"},
            {"verifier_id": "other-v1"},
        ):
            values = {
                "image_reference": policy.runtime_image_reference,
                "platform": policy.platform,
                "verifier_id": policy.verifier_id,
            }
            values.update(changed)
            with self.subTest(changed=changed):
                self.assertIs(classify_target_tuple(**values), Outcome.UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
