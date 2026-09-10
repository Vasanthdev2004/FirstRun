"""Reference recipe/README renderer, not a command executor or sandbox."""
from __future__ import annotations
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shlex
from typing import Any

START = '<!-- firstrun:setup:start -->'
END = '<!-- firstrun:setup:end -->'

def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f'Duplicate JSON key: {key}')
        out[key] = value
    return out

def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)

def validate_recipe(recipe: dict[str, Any]) -> None:
    if not isinstance(recipe, dict) or set(recipe) != {'version', 'steps', 'start'}:
        raise ValueError('Recipe must contain only version, steps, start')
    if type(recipe['version']) is not int or recipe['version'] != 1:
        raise ValueError('Unsupported recipe version')
    if not isinstance(recipe['steps'], list) or not 1 <= len(recipe['steps']) <= 12:
        raise ValueError('Recipe needs 1-12 foreground steps')
    ids = set()
    for step in [*recipe['steps'], recipe['start']]:
        if not isinstance(step, dict) or set(step) != {'id', 'argv', 'cwd', 'timeout_seconds'}:
            raise ValueError('Invalid step fields')
        if not isinstance(step['id'], str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', step['id']):
            raise ValueError('Invalid step ID')
        if step['id'] in ids:
            raise ValueError('Duplicate step ID')
        ids.add(step['id'])
        args = step['argv']
        if not isinstance(args, list) or not 1 <= len(args) <= 32:
            raise ValueError('argv must contain 1-32 arguments')
        if any(not isinstance(a, str) or not a or len(a)>2048 or any(c in a for c in '\0\r\n') for a in args):
            raise ValueError('Invalid command argument')
        cwd = step['cwd']
        if not isinstance(cwd, str) or not cwd or len(cwd)>200 or '\\' in cwd or ':' in cwd or any(c in cwd for c in '\0\r\n'):
            raise ValueError('Invalid cwd')
        p = PurePosixPath(cwd)
        if p.is_absolute() or '..' in p.parts or str(p) != cwd:
            raise ValueError('cwd must be normalized and relative')
        if type(step['timeout_seconds']) is not int or not 1 <= step['timeout_seconds'] <= 600:
            raise ValueError('Invalid timeout')
    # A real worker must ALSO enforce containment through symlinks and command
    # authorization. This shape validator is not a security boundary.

def render_block(recipe: dict[str, Any]) -> str:
    validate_recipe(recipe)
    commands = []
    for step in [*recipe['steps'], recipe['start']]:
        command = shlex.join(step['argv'])
        if step['cwd'] != '.':
            command = f"(cd -- {shlex.quote(step['cwd'])} && {command})"
        commands.append(command)
    return START + '\n```bash\n' + '\n'.join(commands) + '\n```\n' + END

def replace_block(markdown: str, recipe: dict[str, Any]) -> str:
    if markdown.count(START) != 1 or markdown.count(END) != 1:
        raise ValueError('README must contain exactly one start and one end marker')
    a, b = markdown.index(START), markdown.index(END)
    if b < a:
        raise ValueError('End marker precedes start marker')
    return markdown[:a] + render_block(recipe) + markdown[b + len(END):]

def check_block(markdown: str, recipe: dict[str, Any]) -> bool:
    return replace_block(markdown, recipe) == markdown

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--recipe', type=Path, required=True)
    p.add_argument('--readme', type=Path, required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true')
    group.add_argument('--write', action='store_true')
    a = p.parse_args()
    try:
        recipe = read_json(a.recipe)
        markdown = a.readme.read_text(encoding='utf-8')
        proposed = replace_block(markdown, recipe)
        if a.check:
            matched = proposed == markdown
            print('MATCH' if matched else 'DOC_RECIPE_MISMATCH')
            return 0 if matched else 1
        a.readme.write_text(proposed, encoding='utf-8')
        print('Updated managed setup block; review the diff.')
        return 0
    except (ValueError, OSError) as exc:
        p.exit(2, f'{exc}\n')
if __name__ == '__main__':
    raise SystemExit(main())
