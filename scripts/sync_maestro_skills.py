#!/usr/bin/env python3
"""Generate ChatGPT/Codex Maestro skills from the Claude-managed sources."""

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / ".claude" / "skills"
TARGET_ROOT = ROOT / ".agents" / "skills"
SKILL_NAMES = (
    "maestro-task",
    "maestro-scope",
    "maestro-build",
    "maestro-auto",
    "maestro-vibe",
)

INTERFACES = {
    "maestro-task": (
        "Maestro Task",
        "File investigated work on the Maestro board",
        "Use $maestro-task to investigate this request and file it on the Maestro board.",
    ),
    "maestro-scope": (
        "Maestro Scope",
        "Investigate and plan a Maestro board item",
        "Use $maestro-scope to investigate and produce an approved plan for this Maestro item.",
    ),
    "maestro-build": (
        "Maestro Build",
        "Build and close a planned Maestro board item",
        "Use $maestro-build to claim, implement, verify, and close this planned Maestro item.",
    ),
    "maestro-auto": (
        "Maestro Auto",
        "Scope and build a Maestro item batch",
        "Use $maestro-auto to scope and build this batch behind one consolidated approval gate.",
    ),
    "maestro-vibe": (
        "Maestro Vibe",
        "Ship a small change with lightweight tracking",
        "Use $maestro-vibe to implement and verify this small change, then record it on Maestro.",
    ),
}


def split_frontmatter(text, source):
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"{source}: missing YAML frontmatter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"{source}: unterminated YAML frontmatter") from exc
    return lines[1:closing], "\n".join(lines[closing + 1 :]).lstrip("\n")


def scalar(frontmatter, key, source):
    prefix = f"{key}:"
    for index, line in enumerate(frontmatter):
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if value in (">", ">-", "|", "|-"):
            parts = []
            for continuation in frontmatter[index + 1 :]:
                if continuation and not continuation[0].isspace():
                    break
                parts.append(continuation.strip())
            return " ".join(part for part in parts if part)
        return value.strip('"\'')
    raise ValueError(f"{source}: missing {key!r} in frontmatter")


def source_version(frontmatter):
    for line in frontmatter:
        if line.startswith("maestro-skill-version:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def yaml_block(value):
    words = value.split()
    lines = []
    current = []
    length = 0
    for word in words:
        projected = length + len(word) + (1 if current else 0)
        if current and projected > 76:
            lines.append("  " + " ".join(current))
            current = [word]
            length = len(word)
        else:
            current.append(word)
            length = projected
    if current:
        lines.append("  " + " ".join(current))
    return "\n".join(lines)


def adapt_body(body):
    body = re.sub(r"`/maestro-(task|scope|build|auto|vibe)", r"`$maestro-\1", body)
    body = body.replace("`CLAUDE.md`", "`AGENTS.md`")
    body = body.replace("Opus, high effort", "frontier model, high reasoning")
    return body


def render_skill(name):
    source = SOURCE_ROOT / name / "SKILL.md"
    frontmatter, body = split_frontmatter(source.read_text(encoding="utf-8"), source)
    source_name = scalar(frontmatter, "name", source)
    if source_name != name:
        raise ValueError(f"{source}: expected name {name!r}, found {source_name!r}")
    description = scalar(frontmatter, "description", source)
    version = source_version(frontmatter)
    note = (
        f"<!-- Generated from .claude/skills/{name}/SKILL.md "
        f"(maestro-skill-version: {version}). Do not edit directly. -->"
    )
    return (
        "---\n"
        f"name: {name}\n"
        "description: >-\n"
        f"{yaml_block(description)}\n"
        "---\n\n"
        f"{note}\n\n"
        f"{adapt_body(body).rstrip()}\n"
    )


def quote_yaml(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_openai_yaml(name):
    display_name, short_description, default_prompt = INTERFACES[name]
    return (
        "interface:\n"
        f"  display_name: {quote_yaml(display_name)}\n"
        f"  short_description: {quote_yaml(short_description)}\n"
        f"  default_prompt: {quote_yaml(default_prompt)}\n"
        "dependencies:\n"
        "  tools:\n"
        "    - type: \"mcp\"\n"
        "      value: \"maestro\"\n"
        "      description: \"Read and update the shared Maestro workspace and boards\"\n"
        "      transport: \"streamable_http\"\n"
        "      url: \"https://maestromojo.com/mcp\"\n"
    )


def expected_files():
    for name in SKILL_NAMES:
        target = TARGET_ROOT / name
        yield target / "SKILL.md", render_skill(name)
        yield target / "agents" / "openai.yaml", render_openai_yaml(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when generated skills are missing or stale without writing files",
    )
    args = parser.parse_args()

    stale = []
    for path, expected in expected_files():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == expected:
            continue
        stale.append(path.relative_to(ROOT))
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")

    if args.check and stale:
        for path in stale:
            print(f"stale: {path}", file=sys.stderr)
        print("Run scripts/sync_maestro_skills.py to refresh generated skills.", file=sys.stderr)
        return 1

    action = "checked" if args.check else "synced"
    print(f"{action} {len(SKILL_NAMES)} Maestro skills in {TARGET_ROOT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
