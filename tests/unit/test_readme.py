from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType

from firstrun.domain.contracts import Recipe, load_recipe
from firstrun.verification.readme import (
    END,
    START,
    check_block,
    check_block_bytes,
    render_block,
    replace_block,
    replace_block_bytes,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "notes-app"


def load_reference_renderer() -> ModuleType:
    path = ROOT / "tools" / "render_recipe.py"
    specification = importlib.util.spec_from_file_location("reference_render_recipe", path)
    if specification is None or specification.loader is None:
        raise AssertionError("could not load reference renderer")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class ReadmeRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.reference = load_reference_renderer()
        cls.broken_path = FIXTURE / ".firstrun" / "recipe.json"
        cls.fixed_path = ROOT / "examples" / "recipe.fixed.json"
        cls.markdown = (FIXTURE / "README.md").read_text(encoding="utf-8")

    def test_rendering_matches_reference_for_broken_and_fixed_recipes(self) -> None:
        for path in (self.broken_path, self.fixed_path):
            with self.subTest(path=path):
                raw = self.reference.read_json(path)
                recipe = load_recipe(path)
                self.assertEqual(self.reference.render_block(raw), render_block(recipe))

    def test_existing_fixture_matches_broken_recipe_but_not_fixed_recipe(self) -> None:
        self.assertTrue(check_block(self.markdown, load_recipe(self.broken_path)))
        self.assertFalse(check_block(self.markdown, load_recipe(self.fixed_path)))

    def test_replacement_preserves_all_unmanaged_content_and_is_idempotent(self) -> None:
        recipe = load_recipe(self.fixed_path)
        result = replace_block(self.markdown, recipe)

        self.assertEqual(
            self.markdown.split(START)[0],
            result.split(START)[0],
        )
        self.assertEqual(
            self.markdown.split(END)[1],
            result.split(END)[1],
        )
        self.assertTrue(check_block(result, recipe))
        self.assertEqual(result, replace_block(result, recipe))

    def test_byte_renderer_preserves_unmanaged_bytes_and_crlf_style(self) -> None:
        original = (FIXTURE / "README.md").read_bytes()
        fixed = load_recipe(self.fixed_path)
        rendered = replace_block_bytes(original, fixed)
        start = START.encode("ascii")
        end = END.encode("ascii")

        self.assertEqual(original.split(start)[0], rendered.split(start)[0])
        self.assertEqual(original.split(end)[1], rendered.split(end)[1])
        self.assertIn(b"npm run db:migrate\r\n", rendered)
        self.assertTrue(check_block_bytes(rendered, fixed))
        self.assertEqual(rendered, replace_block_bytes(rendered, fixed))

    def test_shell_quoting_and_relative_cwd_match_reference(self) -> None:
        raw = self.reference.read_json(self.broken_path)
        raw["steps"][0]["argv"] = ["echo", "one; two", "it's quoted"]
        raw["steps"][0]["cwd"] = "nested directory"
        recipe = Recipe.model_validate(copy.deepcopy(raw))

        rendered = render_block(recipe)

        self.assertEqual(self.reference.render_block(raw), rendered)
        self.assertIn("(cd -- 'nested directory' &&", rendered)
        self.assertIn("'one; two'", rendered)

    def test_missing_duplicate_and_reversed_markers_are_rejected(self) -> None:
        recipe = load_recipe(self.broken_path)
        invalid = (
            "no markers",
            self.markdown + START,
            self.markdown + END,
            f"{END}\ncontent\n{START}",
        )
        for markdown in invalid:
            with self.subTest(markdown=markdown), self.assertRaises(ValueError):
                replace_block(markdown, recipe)

    def test_renderer_requires_validated_recipe_and_text(self) -> None:
        recipe = load_recipe(self.broken_path)
        raw = json.loads(self.broken_path.read_text(encoding="utf-8"))
        with self.assertRaises(TypeError):
            render_block(raw)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            replace_block(b"markdown", recipe)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
