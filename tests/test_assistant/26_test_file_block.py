"""Tests for the file block type in the assistant block parser."""
from testit import helpers as th


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_file_block(opts):
    """Valid file block should be parsed."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Here's your export:

```assistant_block
{"type": "file", "filename": "export_users_2026-04-13.csv", "url": "https://example.com/s/Xk9mR2p", "size": 45230, "format": "csv", "row_count": 1250, "expires_in": "14 days"}
```

The file contains 1,250 user records."""

    clean, blocks = _parse_blocks(text)
    assert len(blocks) == 1, f"Expected 1 block, got {len(blocks)}"
    assert blocks[0]["type"] == "file", f"Expected file type, got {blocks[0]['type']}"
    assert blocks[0]["filename"] == "export_users_2026-04-13.csv", \
        f"Filename mismatch: {blocks[0]['filename']}"
    assert blocks[0]["url"] == "https://example.com/s/Xk9mR2p", \
        f"URL mismatch: {blocks[0]['url']}"
    assert blocks[0]["size"] == 45230, f"Size mismatch: {blocks[0]['size']}"
    assert blocks[0]["format"] == "csv", f"Format mismatch: {blocks[0]['format']}"
    assert blocks[0]["row_count"] == 1250, f"Row count mismatch: {blocks[0]['row_count']}"
    assert "assistant_block" not in clean, "Block fence should be removed from clean text"
    assert "Here's your export" in clean, "Narrative text should be preserved"


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_file_block_minimal(opts):
    """File block with only required fields should be valid."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Download:

```assistant_block
{"type": "file", "filename": "report.csv", "url": "https://example.com/download/abc"}
```
"""

    clean, blocks = _parse_blocks(text)
    assert len(blocks) == 1, f"Expected 1 block, got {len(blocks)}"
    assert blocks[0]["type"] == "file", f"Expected file type, got {blocks[0]['type']}"
    assert blocks[0]["filename"] == "report.csv", f"Filename: {blocks[0]['filename']}"
    assert blocks[0]["url"] == "https://example.com/download/abc", f"URL: {blocks[0]['url']}"


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_file_block_missing_url_rejected(opts):
    """File block without url should be rejected."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Download:

```assistant_block
{"type": "file", "filename": "report.csv"}
```
"""

    clean, blocks = _parse_blocks(text)
    assert len(blocks) == 0, "File block without url should be rejected"


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_file_block_missing_filename_rejected(opts):
    """File block without filename should be rejected."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Download:

```assistant_block
{"type": "file", "url": "https://example.com/download/abc"}
```
"""

    clean, blocks = _parse_blocks(text)
    assert len(blocks) == 0, "File block without filename should be rejected"


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_file_block_in_valid_block_types(opts):
    """'file' should be in the VALID_BLOCK_TYPES set."""
    from mojo.apps.assistant.services.agent import VALID_BLOCK_TYPES
    assert "file" in VALID_BLOCK_TYPES, \
        f"'file' should be in VALID_BLOCK_TYPES, got: {VALID_BLOCK_TYPES}"


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_file_block_mixed_with_other_types(opts):
    """File block should work alongside other block types."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Export complete. Here are the details:

```assistant_block
{"type": "stat", "items": [{"label": "Rows Exported", "value": 1250}]}
```

```assistant_block
{"type": "file", "filename": "export.csv", "url": "https://example.com/s/abc", "size": 45230, "format": "csv", "row_count": 1250}
```

Click above to download."""

    clean, blocks = _parse_blocks(text)
    assert len(blocks) == 2, f"Expected 2 blocks, got {len(blocks)}"
    types = [b["type"] for b in blocks]
    assert "stat" in types, "Should contain stat block"
    assert "file" in types, "Should contain file block"
