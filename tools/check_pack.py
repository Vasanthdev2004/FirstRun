"""Check handoff assets only. This is NOT the FirstRun application test suite."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from render_recipe import START, END, read_json, validate_recipe, render_block, replace_block, check_block
ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / 'fixtures' / 'notes-app'

class PackChecks(unittest.TestCase):
    def setUp(self):
        self.broken = read_json(REPO / '.firstrun/recipe.json')
        self.fixed = read_json(ROOT / 'examples/recipe.fixed.json')
        self.markdown = (REPO / 'README.md').read_text(encoding='utf-8')

    def test_01_required_entrypoints(self):
        for name in ['AGENTS.md', 'START_HERE.md', 'TOMORROW.md', 'STATUS.md', 'docs/PRODUCT.md',
                     'docs/CONTRACTS.md', 'docs/SECURITY.md', 'docs/BUILD_PLAN.md',
                     'docs/V1_AUDIT.md', 'docs/SOURCES_AND_RULES.md', 'prompts/01-plan.md',
                     'prompts/02-build.md', 'prompts/03-review.md', 'tools/probe_notes.py']:
            self.assertTrue((ROOT / name).is_file(), name)

    def test_02_all_json_parses_without_duplicate_keys(self):
        for file in ROOT.rglob('*.json'):
            read_json(file)

    def test_03_reference_recipes_are_valid(self):
        validate_recipe(self.broken)
        validate_recipe(self.fixed)

    def test_04_baseline_readme_matches_broken_recipe(self):
        self.assertTrue(check_block(self.markdown, self.broken))

    def test_05_fixed_recipe_is_a_detectable_doc_change(self):
        self.assertFalse(check_block(self.markdown, self.fixed))

    def test_06_rendering_preserves_unmanaged_content(self):
        result = replace_block(self.markdown, self.fixed)
        self.assertTrue(check_block(result, self.fixed))
        self.assertEqual(result.split(START)[0], self.markdown.split(START)[0])
        self.assertEqual(result.split(END)[1], self.markdown.split(END)[1])

    def test_07_renderer_is_idempotent(self):
        result = replace_block(self.markdown, self.fixed)
        self.assertEqual(result, replace_block(result, self.fixed))

    def test_08_recipe_cannot_contain_verifier(self):
        self.broken['acceptance'] = {'command': 'true'}
        with self.assertRaises(ValueError): validate_recipe(self.broken)

    def test_09_duplicate_step_ids_rejected(self):
        self.broken['steps'].append(copy.deepcopy(self.broken['steps'][0]))
        with self.assertRaises(ValueError): validate_recipe(self.broken)

    def test_10_path_traversal_rejected(self):
        self.broken['steps'][0]['cwd'] = '../outside'
        with self.assertRaises(ValueError): validate_recipe(self.broken)

    def test_11_absolute_paths_rejected(self):
        self.broken['steps'][0]['cwd'] = '/tmp/elsewhere'
        with self.assertRaises(ValueError): validate_recipe(self.broken)

    def test_12_windows_ambiguous_paths_rejected(self):
        self.broken['steps'][0]['cwd'] = r'C:\secrets'
        with self.assertRaises(ValueError): validate_recipe(self.broken)

    def test_13_bad_timeouts_rejected(self):
        for timeout in [0, -1, 601, True, '30']:
            obj = copy.deepcopy(self.broken)
            obj['steps'][0]['timeout_seconds'] = timeout
            with self.assertRaises(ValueError): validate_recipe(obj)

    def test_14_multiline_args_rejected(self):
        self.broken['steps'][0]['argv'] = ['echo', 'line1\nline2']
        with self.assertRaises(ValueError): validate_recipe(self.broken)

    def test_15_shell_metacharacters_are_quoted(self):
        self.broken['steps'][0]['argv'] = ['echo', 'one; two']
        self.assertIn("echo 'one; two'", render_block(self.broken))

    def test_16_missing_markers_rejected(self):
        with self.assertRaises(ValueError): replace_block('no markers', self.broken)

    def test_17_duplicate_markers_rejected(self):
        with self.assertRaises(ValueError): replace_block(self.markdown + START, self.broken)

    def test_18_oracle_only_adds_existing_migration(self):
        self.assertEqual(self.fixed['steps'][0], self.broken['steps'][0])
        self.assertEqual(self.fixed['start'], self.broken['start'])
        self.assertEqual(len(self.fixed['steps']), len(self.broken['steps']) + 1)
        self.assertEqual(self.fixed['steps'][1]['argv'], ['npm', 'run', 'db:migrate'])
        package = read_json(REPO / 'package.json')
        self.assertIn('db:migrate', package['scripts'])

    def test_19_schemas_reject_extra_fields_by_design(self):
        for name in ['recipe', 'target']:
            schema = read_json(ROOT / f'schemas/{name}.schema.json')
            self.assertFalse(schema['additionalProperties'])
        recipe = read_json(ROOT / 'schemas/recipe.schema.json')
        self.assertEqual(set(recipe['properties']), {'version', 'steps', 'start'})

    def test_20_acceptance_matrix_has_unique_ids(self):
        cases = read_json(ROOT / 'spec-tests/acceptance_matrix.json')['cases']
        self.assertEqual(len(cases), len({case['id'] for case in cases}))
        self.assertGreaterEqual(len(cases), 15)

    def test_21_fixture_javascript_syntax(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node missing: fixture syntax not checked')
        for name in ['src/server.mjs', 'scripts/migrate.mjs']:
            result = subprocess.run([node, '--check', str(REPO / name)], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_22_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'bad.json'
            p.write_text('{"version":1,"version":2}', encoding='utf-8')
            with self.assertRaises(ValueError): read_json(p)

    def test_23_no_mutable_database_in_fixture(self):
        self.assertFalse((REPO / '.local').exists())
        self.assertFalse(list(REPO.rglob('*.sqlite')))
        self.assertFalse((REPO / 'node_modules').exists())

    def test_24_recipe_version_is_not_a_boolean(self):
        self.broken['version'] = True
        with self.assertRaises(ValueError): validate_recipe(self.broken)

if __name__ == '__main__':
    print('HANDOFF ASSET CHECKS ONLY — no Docker/Strands/GitHub execution is tested.\n', flush=True)
    unittest.main(verbosity=2)
