---
name: memory
description: Display the current state of Claude Code's project memory — all stored memories and their types
user-invocable: true
---

Display the current state of Claude Code's project memory.

## Steps

1. Read the memory index at `~/.claude/projects/-Users-ians-Projects-mojo-django-mojo/memory/MEMORY.md`
2. List all memory files in that directory using Glob pattern `*.md`
3. For each memory file (excluding MEMORY.md), read it and display:
   - Name (from frontmatter)
   - Type (user, feedback, project, reference)
   - Description (from frontmatter)
   - Content summary (first few lines of body)
4. Present a clean summary table followed by the full MEMORY.md index

## Notes

- Memory lives at `~/.claude/projects/-Users-ians-Projects-mojo-django-mojo/memory/` — this is personal/local, not committed to git
- The MEMORY.md file is the index — it is loaded automatically every session
- Individual .md files contain the actual memory content with typed frontmatter
- Do NOT modify any memory files — this command is read-only
