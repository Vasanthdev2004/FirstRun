from __future__ import annotations

import unittest

from firstrun.domain import EXIT_CODES, Outcome, exit_code_for


class OutcomeTests(unittest.TestCase):
    def test_exit_codes_are_unique_and_nonzero_except_passed(self) -> None:
        self.assertEqual(exit_code_for(Outcome.PASSED), 0)
        nonzero = [code for outcome, code in EXIT_CODES.items() if outcome is not Outcome.PASSED]
        self.assertEqual(len(nonzero), len(set(nonzero)))
        self.assertTrue(all(code > 0 for code in nonzero))

    def test_documented_exit_mapping_is_stable(self) -> None:
        self.assertEqual(
            EXIT_CODES,
            {
                Outcome.PASSED: 0,
                Outcome.FAILED: 10,
                Outcome.TIMED_OUT: 11,
                Outcome.INFRASTRUCTURE_ERROR: 12,
                Outcome.POLICY_BLOCKED: 13,
                Outcome.UNSUPPORTED: 14,
                Outcome.CLEANUP_FAILED: 15,
            },
        )


if __name__ == "__main__":
    unittest.main()
