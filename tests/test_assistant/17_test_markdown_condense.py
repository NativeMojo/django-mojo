"""
Tests for markdown condensing in assistant responses.

Verifies that _parse_blocks / _condense_markdown correctly:
- Collapses excessive blank lines
- Repairs markdown tables with blank lines between rows
- Strips duplicate markdown tables when a matching table block exists
- Preserves code blocks and blockquotes
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_condense_collapses_blank_lines(opts):
    """3+ consecutive blank lines should be collapsed to one blank line."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = "First paragraph.\n\n\n\n\nSecond paragraph.\n\n\n\nThird paragraph."
    clean, blocks = _parse_blocks(text)
    assert_true("\n\n\n" not in clean,
                "Should not have 3+ consecutive newlines in output")
    assert_true("First paragraph." in clean, "First paragraph should be preserved")
    assert_true("Second paragraph." in clean, "Second paragraph should be preserved")
    assert_true("Third paragraph." in clean, "Third paragraph should be preserved")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_condense_repairs_markdown_table(opts):
    """Markdown table with blank lines between rows should be repaired."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Here are the results:

| ID | Name |
| --- | --- |
| 1 | Alice |

| 2 | Bob |

| 3 | Carol |

That's the list."""

    clean, blocks = _parse_blocks(text)
    # All three data rows should be contiguous
    assert_true("| 1 | Alice |" in clean, "Row 1 should be present")
    assert_true("| 2 | Bob |" in clean, "Row 2 should be present")
    assert_true("| 3 | Carol |" in clean, "Row 3 should be present")
    # Check no blank lines between pipe rows
    lines = clean.split("\n")
    in_table = False
    for i, line in enumerate(lines):
        if line.strip().startswith("|"):
            in_table = True
        elif in_table and line.strip() == "":
            # Next non-blank line should not start with |
            next_lines = [l for l in lines[i+1:] if l.strip()]
            if next_lines and next_lines[0].strip().startswith("|"):
                assert_true(False, f"Found blank line between table rows at line {i}")
            in_table = False


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_condense_strips_duplicate_table_by_columns(opts):
    """Markdown table matching a block's columns should be stripped."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Here are the failed jobs:

```assistant_block
{"type": "table", "title": "Failed Jobs", "columns": ["ID", "Function", "Error"], "rows": [["abc", "send_email", "timeout"], ["def", "process_payment", "refused"]]}
```

| ID | Function | Error |
| --- | --- | --- |
| abc | send_email | timeout |
| def | process_payment | refused |

Check the logs for details."""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, f"Expected 1 block, got {len(blocks)}")
    assert_eq(blocks[0]["type"], "table", "Block should be a table")
    # The markdown table should be stripped since columns match
    assert_true("| abc |" not in clean,
                "Duplicate markdown table rows should be removed")
    assert_true("| --- |" not in clean,
                "Duplicate markdown table separator should be removed")
    assert_true("Check the logs" in clean,
                "Non-table text should be preserved")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_condense_strips_duplicate_table_by_title(opts):
    """Markdown table with heading matching block title should be stripped."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Overview:

```assistant_block
{"type": "table", "title": "Open Incidents", "columns": ["ID", "Category"], "rows": [[1, "auth"], [2, "ddos"]]}
```

### Open Incidents

| ID | Category |
| --- | --- |
| 1 | auth |
| 2 | ddos |

That covers the incidents."""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, f"Expected 1 block, got {len(blocks)}")
    # Both the heading and markdown table should be stripped
    assert_true("| 1 | auth |" not in clean,
                "Duplicate markdown table should be removed")
    assert_true("That covers the incidents" in clean,
                "Non-table text should be preserved")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_condense_preserves_non_duplicate_table(opts):
    """Markdown table with different columns should NOT be stripped."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Two different tables:

```assistant_block
{"type": "table", "title": "Jobs", "columns": ["ID", "Function", "Status"], "rows": [["j1", "send", "ok"]]}
```

| Name | Email |
| --- | --- |
| Alice | alice@test.com |
| Bob | bob@test.com |

Done."""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, f"Expected 1 block, got {len(blocks)}")
    # The markdown table has different columns (Name, Email vs ID, Function, Status)
    # Only 0 columns overlap, so it should be preserved
    assert_true("| Alice | alice@test.com |" in clean,
                "Non-duplicate markdown table should be preserved")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_condense_preserves_code_blocks(opts):
    """Code blocks with pipes and blank lines should not be modified."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Here's a bash example:

```bash
echo "hello" | grep "h"

echo "world" | wc -l
```

And some text after."""

    clean, blocks = _parse_blocks(text)
    assert_true('echo "hello" | grep "h"' in clean,
                "Code block content with pipes should be preserved")
    assert_true('echo "world" | wc -l' in clean,
                "Code block content should be preserved")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_condense_preserves_blockquotes(opts):
    """Blockquotes should not be mangled by the condenser."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Important note:

> This is a blockquote.
> It has multiple lines.

Some text after."""

    clean, blocks = _parse_blocks(text)
    assert_true("> This is a blockquote." in clean,
                "Blockquote should be preserved")
    assert_true("> It has multiple lines." in clean,
                "Blockquote continuation should be preserved")
