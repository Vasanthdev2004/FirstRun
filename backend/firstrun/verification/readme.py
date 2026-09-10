"""Pure deterministic rendering for the managed README setup block."""

from __future__ import annotations

import shlex

from firstrun.domain.contracts import Recipe


START = "<!-- firstrun:setup:start -->"
END = "<!-- firstrun:setup:end -->"


def render_block(recipe: Recipe) -> str:
    if not isinstance(recipe, Recipe):
        raise TypeError("recipe must be a validated Recipe")
    commands: list[str] = []
    for step in (*recipe.steps, recipe.start):
        command = shlex.join(step.argv)
        if step.cwd != ".":
            command = f"(cd -- {shlex.quote(step.cwd)} && {command})"
        commands.append(command)
    return START + "\n```bash\n" + "\n".join(commands) + "\n```\n" + END


def replace_block(markdown: str, recipe: Recipe) -> str:
    if not isinstance(markdown, str):
        raise TypeError("markdown must be text")
    if markdown.count(START) != 1 or markdown.count(END) != 1:
        raise ValueError("README must contain exactly one start and one end marker")
    start_index = markdown.index(START)
    end_index = markdown.index(END)
    if end_index < start_index:
        raise ValueError("end marker precedes start marker")
    return (
        markdown[:start_index]
        + render_block(recipe)
        + markdown[end_index + len(END) :]
    )


def check_block(markdown: str, recipe: Recipe) -> bool:
    return replace_block(markdown, recipe) == markdown


def replace_block_bytes(markdown: bytes, recipe: Recipe) -> bytes:
    """Render the managed block while preserving every unmanaged source byte."""

    if type(markdown) is not bytes:
        raise TypeError("markdown must be bytes")
    try:
        markdown.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("README must be UTF-8") from exc
    start = START.encode("ascii")
    end = END.encode("ascii")
    if markdown.count(start) != 1 or markdown.count(end) != 1:
        raise ValueError("README must contain exactly one start and one end marker")
    start_index = markdown.index(start)
    end_index = markdown.index(end)
    if end_index < start_index:
        raise ValueError("end marker precedes start marker")
    existing_block = markdown[start_index : end_index + len(end)]
    newline = b"\r\n" if b"\r\n" in existing_block else b"\n"
    rendered = render_block(recipe).encode("utf-8")
    if newline == b"\r\n":
        rendered = rendered.replace(b"\n", b"\r\n")
    return markdown[:start_index] + rendered + markdown[end_index + len(end) :]


def check_block_bytes(markdown: bytes, recipe: Recipe) -> bool:
    return replace_block_bytes(markdown, recipe) == markdown


__all__ = [
    "START",
    "END",
    "render_block",
    "replace_block",
    "check_block",
    "replace_block_bytes",
    "check_block_bytes",
]
