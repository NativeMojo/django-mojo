# Bug: Markdown renderer not rendering tables

**Status:** resolved
**Date:** 2026-04-09
**Component:** `mojo/apps/docit/services/markdown.py`

## Symptom

`POST /api/docit/render` returns raw pipe syntax with `<br />` tags instead of `<table>` HTML when the input contains markdown tables. Example output:

```html
<p>| Area | Status |<br />
|------|--------|<br />
| OSSEC rules | Healthy |</p>
```

## Root Cause

`_discover_plugins()` was defined but never called — the line was commented out. Both renderer instances were initialized with `plugins=[]`, so mistune's `table` plugin (and all other plugins) were never loaded. The `hard_wrap=True` setting then converted the table's newlines into `<br />` tags, producing paragraph text instead of a table.

## Fix

- Wired up `_discover_plugins()` so both renderers load the full plugin list: `table`, `url`, `task_lists`, `footnotes`, `abbr`, `mark`, `math`, `strikethrough`, `spoiler`
- Fixed `task_list` -> `task_lists` (correct mistune 3.x plugin name)

## Files Changed

- `mojo/apps/docit/services/markdown.py` — enable plugins
- `tests/test_docit/docit_core.py` — added `test_markdown_table_rendering` and `test_render_endpoint_table`
- `CHANGELOG.md` — documented the fix
