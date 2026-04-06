"""
Tests for rich block types: action, list, alert.

Verifies that _parse_blocks correctly parses, validates, and rejects
the new block types alongside the existing table/chart/stat types.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_action_block(opts):
    """Valid action block should be parsed with an action_id added."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """I can block that IP for you.

```assistant_block
{"type": "action", "title": "Block IP", "description": "Block 1.2.3.4 for 24h", "actions": [{"label": "Confirm", "value": "confirm"}, {"label": "Cancel", "value": "cancel"}]}
```

Let me know your choice."""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, f"Expected 1 block, got {len(blocks)}")
    assert_eq(blocks[0]["type"], "action", f"Expected action type, got {blocks[0]['type']}")
    assert_eq(blocks[0]["title"], "Block IP", f"Expected title 'Block IP', got {blocks[0]['title']}")
    assert_true("action_id" in blocks[0], "Action block should have an action_id assigned")
    assert_true(len(blocks[0]["action_id"]) == 36, f"action_id should be a UUID, got {blocks[0]['action_id']}")
    assert_eq(len(blocks[0]["actions"]), 2, f"Expected 2 actions, got {len(blocks[0]['actions'])}")
    assert_true("assistant_block" not in clean, "Block fence should be removed from clean text")
    assert_true("I can block that IP" in clean, "Narrative text should be preserved")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_action_block_unique_ids(opts):
    """Each action block should get a unique action_id."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Two actions needed.

```assistant_block
{"type": "action", "title": "Action 1", "actions": [{"label": "Go", "value": "go"}]}
```

```assistant_block
{"type": "action", "title": "Action 2", "actions": [{"label": "Go", "value": "go"}]}
```
"""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 2, f"Expected 2 action blocks, got {len(blocks)}")
    assert_true(blocks[0]["action_id"] != blocks[1]["action_id"],
                "Each action block should have a unique action_id")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_action_block_missing_actions_rejected(opts):
    """Action block without actions array should be rejected."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Here's an action.

```assistant_block
{"type": "action", "title": "Block IP", "description": "Block 1.2.3.4"}
```

Done."""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Action block without actions should be rejected")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_action_block_empty_actions_rejected(opts):
    """Action block with empty actions array should be rejected."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Here's an action.

```assistant_block
{"type": "action", "title": "Block IP", "actions": []}
```

Done."""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Action block with empty actions should be rejected")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_list_block(opts):
    """Valid list block should be parsed."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Here are the user details:

```assistant_block
{"type": "list", "title": "User Detail", "items": [{"label": "Email", "value": "admin@example.com"}, {"label": "Role", "value": "Admin"}, {"label": "Last Login", "value": "2026-04-06 09:30 UTC"}]}
```

That's the user info."""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, f"Expected 1 block, got {len(blocks)}")
    assert_eq(blocks[0]["type"], "list", f"Expected list type, got {blocks[0]['type']}")
    assert_eq(blocks[0]["title"], "User Detail", f"Expected title 'User Detail', got {blocks[0]['title']}")
    assert_eq(len(blocks[0]["items"]), 3, f"Expected 3 items, got {len(blocks[0]['items'])}")
    assert_eq(blocks[0]["items"][0]["label"], "Email", "First item label should be Email")
    assert_true("assistant_block" not in clean, "Block fence should be removed")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_list_block_no_title(opts):
    """List block without title should still be valid."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Details:

```assistant_block
{"type": "list", "items": [{"label": "Status", "value": "active"}]}
```
"""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, "List block without title should be valid")
    assert_true("title" not in blocks[0] or blocks[0].get("title") is None,
                "Title should be absent or None")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_list_block_empty_items_rejected(opts):
    """List block with empty items should be rejected."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Details:

```assistant_block
{"type": "list", "title": "Empty", "items": []}
```
"""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "List block with empty items should be rejected")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_alert_block(opts):
    """Valid alert block should be parsed."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Watch out:

```assistant_block
{"type": "alert", "level": "warning", "title": "Rate Limited", "message": "User exceeded 100 req/min threshold."}
```

You may want to investigate."""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, f"Expected 1 block, got {len(blocks)}")
    assert_eq(blocks[0]["type"], "alert", f"Expected alert type, got {blocks[0]['type']}")
    assert_eq(blocks[0]["level"], "warning", f"Expected warning level, got {blocks[0]['level']}")
    assert_eq(blocks[0]["title"], "Rate Limited", f"Expected title 'Rate Limited', got {blocks[0]['title']}")
    assert_true("assistant_block" not in clean, "Block fence should be removed")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_alert_all_levels(opts):
    """All four alert levels should be accepted."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    for level in ["info", "success", "warning", "error"]:
        text = f"""Note:

```assistant_block
{{"type": "alert", "level": "{level}", "message": "Test {level} alert."}}
```
"""
        clean, blocks = _parse_blocks(text)
        assert_eq(len(blocks), 1, f"Alert with level '{level}' should be accepted, got {len(blocks)} blocks")
        assert_eq(blocks[0]["level"], level, f"Expected level '{level}', got {blocks[0]['level']}")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_alert_invalid_level_rejected(opts):
    """Alert block with invalid level should be rejected."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Alert:

```assistant_block
{"type": "alert", "level": "critical", "message": "Something bad happened."}
```
"""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Alert with invalid level 'critical' should be rejected")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_parse_alert_missing_message_rejected(opts):
    """Alert block without message should be rejected."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Alert:

```assistant_block
{"type": "alert", "level": "error", "title": "Error"}
```
"""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Alert without message should be rejected")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_mixed_block_types(opts):
    """Multiple block types in one response should all be parsed."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Here's the overview:

```assistant_block
{"type": "alert", "level": "warning", "message": "3 critical incidents detected."}
```

```assistant_block
{"type": "table", "title": "Critical Incidents", "columns": ["ID", "Category"], "rows": [[1, "auth"], [2, "ddos"], [3, "brute_force"]]}
```

```assistant_block
{"type": "list", "title": "Top Incident", "items": [{"label": "ID", "value": 1}, {"label": "Category", "value": "auth"}, {"label": "Priority", "value": 9}]}
```

```assistant_block
{"type": "action", "title": "Investigate All", "actions": [{"label": "Yes", "value": "investigate"}, {"label": "Skip", "value": "skip"}]}
```

Let me know how to proceed."""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 4, f"Expected 4 blocks, got {len(blocks)}")
    types = [b["type"] for b in blocks]
    assert_true("alert" in types, "Should contain alert block")
    assert_true("table" in types, "Should contain table block")
    assert_true("list" in types, "Should contain list block")
    assert_true("action" in types, "Should contain action block")
    assert_true("assistant_block" not in clean, "All block fences should be removed")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_existing_block_types_still_work(opts):
    """Existing table, chart, stat blocks should continue to parse correctly."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Dashboard:

```assistant_block
{"type": "stat", "items": [{"label": "Users", "value": 42}]}
```

```assistant_block
{"type": "chart", "chart_type": "bar", "title": "Trend", "labels": ["Mon"], "series": [{"name": "hits", "values": [100]}]}
```

```assistant_block
{"type": "table", "title": "Jobs", "columns": ["ID"], "rows": [["j1"]]}
```
"""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 3, f"Expected 3 blocks, got {len(blocks)}")
    types = [b["type"] for b in blocks]
    assert_true("stat" in types, "stat block should parse")
    assert_true("chart" in types, "chart block should parse")
    assert_true("table" in types, "table block should parse")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_invalid_block_type_still_rejected(opts):
    """Unknown block types should still be silently dropped."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = """Bad block:

```assistant_block
{"type": "unknown_widget", "data": "test"}
```
"""

    clean, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Unknown block type should be rejected")
