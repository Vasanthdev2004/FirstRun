from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from firstrun.domain.contracts import (
    ContractFileError,
    Recipe,
    Target,
    load_contract,
    load_recipe,
    load_target,
    sha256_content_ref,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "notes-app"


def recipe_data() -> dict[str, object]:
    return json.loads((FIXTURE / ".firstrun" / "recipe.json").read_text(encoding="utf-8"))


def target_data() -> dict[str, object]:
    return json.loads((FIXTURE / ".firstrun" / "target.json").read_text(encoding="utf-8"))


class RecipeContractTests(unittest.TestCase):
    def test_fixture_recipe_loads_as_a_deeply_frozen_contract(self) -> None:
        recipe = load_recipe(FIXTURE / ".firstrun" / "recipe.json")

        self.assertEqual(1, recipe.version)
        self.assertIsInstance(recipe.steps, tuple)
        self.assertIsInstance(recipe.steps[0].argv, tuple)
        with self.assertRaises(ValidationError):
            recipe.version = 2  # type: ignore[misc]
        with self.assertRaises(ValidationError):
            recipe.steps[0].cwd = "elsewhere"  # type: ignore[misc]

    def test_recipe_rejects_extra_fields_at_every_level(self) -> None:
        for path, value in (
            (("extra",), True),
            (("steps", 0, "acceptance"), {"command": "true"}),
        ):
            with self.subTest(path=path):
                candidate = copy.deepcopy(recipe_data())
                if len(path) == 1:
                    candidate[path[0]] = value  # type: ignore[index]
                else:
                    candidate["steps"][path[1]][path[2]] = value  # type: ignore[index]
                with self.assertRaises(ValidationError):
                    Recipe.model_validate(candidate)

    def test_recipe_rejects_boolean_versions_and_timeouts(self) -> None:
        candidates: list[dict[str, object]] = []
        version = recipe_data()
        version["version"] = True
        candidates.append(version)
        step_timeout = recipe_data()
        step_timeout["steps"][0]["timeout_seconds"] = True  # type: ignore[index]
        candidates.append(step_timeout)
        start_timeout = recipe_data()
        start_timeout["start"]["timeout_seconds"] = True  # type: ignore[index]
        candidates.append(start_timeout)

        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ValidationError):
                Recipe.model_validate(candidate)

    def test_recipe_rejects_duplicate_step_ids_including_start(self) -> None:
        duplicate_foreground = recipe_data()
        duplicate_foreground["steps"].append(  # type: ignore[union-attr]
            copy.deepcopy(duplicate_foreground["steps"][0])  # type: ignore[index]
        )
        duplicate_start = recipe_data()
        duplicate_start["start"]["id"] = "install"  # type: ignore[index]

        for candidate in (duplicate_foreground, duplicate_start):
            with self.subTest(candidate=candidate), self.assertRaises(ValidationError):
                Recipe.model_validate(candidate)

    def test_recipe_rejects_invalid_cwd_values(self) -> None:
        invalid = (
            "",
            "/absolute",
            "../outside",
            "nested/../outside",
            "./nested",
            "nested//child",
            "C:\\secrets",
            "name:stream",
            "bad\x00cwd",
            "bad\rcwd",
            "bad\ncwd",
            "x" * 201,
        )
        for cwd in invalid:
            with self.subTest(cwd=repr(cwd)):
                candidate = recipe_data()
                candidate["steps"][0]["cwd"] = cwd  # type: ignore[index]
                with self.assertRaises(ValidationError):
                    Recipe.model_validate(candidate)

    def test_recipe_rejects_invalid_argv(self) -> None:
        invalid = (
            [],
            ["npm"] * 33,
            [""],
            ["x" * 2049],
            ["line\nbreak"],
            ["line\rbreak"],
            ["nul\x00byte"],
            [123],
            "npm ci",
        )
        for argv in invalid:
            with self.subTest(argv=repr(argv)):
                candidate = recipe_data()
                candidate["steps"][0]["argv"] = argv  # type: ignore[index]
                with self.assertRaises(ValidationError):
                    Recipe.model_validate(candidate)

    def test_recipe_enforces_step_count_id_and_timeout_bounds(self) -> None:
        candidates: list[dict[str, object]] = []
        no_steps = recipe_data()
        no_steps["steps"] = []
        candidates.append(no_steps)
        too_many = recipe_data()
        too_many["steps"] = [
            {
                "id": f"step{index}",
                "argv": ["npm", "ci"],
                "cwd": ".",
                "timeout_seconds": 1,
            }
            for index in range(13)
        ]
        candidates.append(too_many)
        for timeout in (0, 601):
            candidate = recipe_data()
            candidate["steps"][0]["timeout_seconds"] = timeout  # type: ignore[index]
            candidates.append(candidate)
        bad_id = recipe_data()
        bad_id["steps"][0]["id"] = "UPPER"  # type: ignore[index]
        candidates.append(bad_id)

        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ValidationError):
                Recipe.model_validate(candidate)


class TargetContractTests(unittest.TestCase):
    def test_fixture_target_loads_and_is_frozen(self) -> None:
        target = load_target(FIXTURE / ".firstrun" / "target.json")

        self.assertEqual("notes-create-read-v1", target.acceptance.verifier_id)
        self.assertEqual("linux/amd64", target.runtime.platform)
        with self.assertRaises(ValidationError):
            target.app_port = 4000  # type: ignore[misc]

    def test_target_rejects_extra_fields_wrong_constants_and_ranges(self) -> None:
        candidates: list[dict[str, object]] = []
        extra = target_data()
        extra["permissions"] = ["network"]
        candidates.append(extra)
        for field, value in (
            ("network", "bridge"),
            ("app_port", 1023),
            ("app_port", 65536),
        ):
            candidate = target_data()
            candidate[field] = value
            candidates.append(candidate)
        wrong_platform = target_data()
        wrong_platform["runtime"]["platform"] = "windows/amd64"  # type: ignore[index]
        candidates.append(wrong_platform)
        wrong_path = target_data()
        wrong_path["readiness"]["path"] = "/ready"  # type: ignore[index]
        candidates.append(wrong_path)
        wrong_status = target_data()
        wrong_status["readiness"]["status"] = 204  # type: ignore[index]
        candidates.append(wrong_status)
        wrong_probe = target_data()
        wrong_probe["acceptance"]["verifier_id"] = "repo-test"  # type: ignore[index]
        candidates.append(wrong_probe)
        for branch in ("readiness", "acceptance"):
            for timeout in (0, 121):
                candidate = target_data()
                candidate[branch]["timeout_seconds"] = timeout  # type: ignore[index]
                candidates.append(candidate)

        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ValidationError):
                Target.model_validate(candidate)

    def test_target_rejects_booleans_for_all_integer_fields(self) -> None:
        paths = (
            ("version",),
            ("app_port",),
            ("readiness", "status"),
            ("readiness", "timeout_seconds"),
            ("acceptance", "timeout_seconds"),
        )
        for path in paths:
            with self.subTest(path=path):
                candidate = target_data()
                if len(path) == 1:
                    candidate[path[0]] = True
                else:
                    candidate[path[0]][path[1]] = True  # type: ignore[index]
                with self.assertRaises(ValidationError):
                    Target.model_validate(candidate)


class ContractFileTests(unittest.TestCase):
    def test_loader_rejects_duplicate_keys_at_any_depth(self) -> None:
        duplicate_documents = (
            '{"version":1,"version":1,"steps":[],"start":{}}',
            '{"version":1,"steps":[{"id":"a","id":"b"}],"start":{}}',
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recipe.json"
            for document in duplicate_documents:
                with self.subTest(document=document):
                    path.write_text(document, encoding="utf-8")
                    with self.assertRaisesRegex(ContractFileError, "duplicate JSON key"):
                        load_recipe(path)

    def test_loader_rejects_invalid_utf8_nonfinite_json_and_oversize(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contract.json"
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(ContractFileError, "UTF-8"):
                load_recipe(path)
            path.write_text("NaN", encoding="utf-8")
            with self.assertRaisesRegex(ContractFileError, "unsupported JSON constant"):
                load_recipe(path)
            path.write_bytes(b"{}")
            with self.assertRaisesRegex(ContractFileError, "byte limit"):
                load_recipe(path, max_bytes=1)

    def test_loader_requires_a_regular_non_symlink_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaisesRegex(ContractFileError, "regular file"):
                load_recipe(directory)

            path = directory / "contract.json"
            path.write_bytes(b"{}")
            real_stat = os.lstat(path)
            fake_values = list(real_stat)
            fake_values[0] = stat.S_IFLNK | 0o777
            with patch(
                "firstrun.domain.contracts.os.lstat",
                return_value=os.stat_result(fake_values),
            ):
                with self.assertRaisesRegex(ContractFileError, "symlink"):
                    load_recipe(path)

    def test_generic_loader_requires_a_model_type_and_positive_limit(self) -> None:
        recipe_path = FIXTURE / ".firstrun" / "recipe.json"
        with self.assertRaises(TypeError):
            load_contract(recipe_path, object)  # type: ignore[type-var]
        for limit in (0, -1, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                load_recipe(recipe_path, max_bytes=limit)

    def test_content_reference_hashes_exact_raw_bytes(self) -> None:
        expected = "sha256:" + hashlib.sha256(b"abc").hexdigest()
        self.assertEqual(expected, sha256_content_ref(b"abc"))
        self.assertNotEqual(expected, sha256_content_ref(b"abc\n"))
        with self.assertRaises(TypeError):
            sha256_content_ref("abc")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
