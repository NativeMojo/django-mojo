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


# ---------------------------------------------------------------------------
# chart block validation — new SeriesChart / PieChart options
# ---------------------------------------------------------------------------

def _wrap_block(json_str):
    """Build minimal LLM-style text containing a single assistant_block fence."""
    return (
        "Here is your chart:\n\n"
        "```assistant_block\n"
        f"{json_str}\n"
        "```\n"
    )


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_minimal_backward_compat(opts):
    """Original minimal chart block (no new fields) must still pass."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "line", "title": "Events (24h)", '
        '"labels": ["00:00","06:00","12:00","18:00"], '
        '"series": [{"name": "events", "values": [12, 45, 32, 18]}]}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, f"Minimal chart must still parse, got {len(blocks)}")
    assert_eq(blocks[0]["chart_type"], "line", "chart_type preserved")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_with_all_new_fields_passes(opts):
    """A chart with every new option populated must validate and pass through."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "bar", "title": "By Severity", '
        '"labels": ["Mon","Tue","Wed"], '
        '"series": ['
        '{"name": "low", "values": [1,2,3], "color": "#22c55e"},'
        '{"name": "medium", "values": [4,5,6]},'
        '{"name": "high", "values": [7,8,9]}'
        '], '
        '"stacked": "auto", "colors": ["#22c55e","#f59e0b","#ef4444"], '
        '"show_legend": true, "legend_position": "bottom"}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, f"Fully-populated chart must parse, got {len(blocks)}")
    b = blocks[0]
    assert_eq(b["stacked"], "auto", "stacked='auto' preserved")
    assert_eq(
        b["colors"], ["#22c55e", "#f59e0b", "#ef4444"],
        f"chart-level colors preserved, got {b.get('colors')}",
    )
    assert_eq(
        b["series"][0].get("color"), "#22c55e",
        "per-series color preserved on series[0]",
    )
    assert_eq(b["legend_position"], "bottom", "legend_position preserved")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_invalid_chart_type_dropped(opts):
    """chart_type not in the allowlist must drop the block."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "donut", "labels": ["A"], '
        '"series": [{"name": "x", "values": [1]}]}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Unknown chart_type must drop the block")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_label_value_length_mismatch_dropped(opts):
    """series.values length must equal labels length."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "line", '
        '"labels": ["A","B","C","D","E"], '
        '"series": [{"name": "x", "values": [1,2,3]}]}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Length-mismatched chart must be dropped")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_series_missing_name_dropped(opts):
    """series entry without a name must drop the chart."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "line", "labels": ["A","B"], '
        '"series": [{"values": [1,2]}]}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Series without name must drop the chart")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_series_missing_values_dropped(opts):
    """series entry without values must drop the chart."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "line", "labels": ["A","B"], '
        '"series": [{"name": "x"}]}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Series without values must drop the chart")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_empty_series_dropped(opts):
    """Empty series array must drop the chart."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "line", "labels": ["A","B"], "series": []}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Empty series must drop the chart")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_empty_labels_dropped(opts):
    """Empty labels array must drop the chart."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "line", "labels": [], '
        '"series": [{"name": "x", "values": []}]}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 0, "Empty labels must drop the chart")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_unknown_top_level_field_passes_through(opts):
    """Future top-level fields the validator doesn't know about must pass through."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "line", "labels": ["A","B"], '
        '"series": [{"name": "x", "values": [1,2]}], '
        '"future_option": "experimental_value", "another_new_field": 42}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, "Chart with unknown fields must still pass")
    assert_eq(
        blocks[0].get("future_option"), "experimental_value",
        "Unknown top-level fields must be preserved verbatim",
    )
    assert_eq(
        blocks[0].get("another_new_field"), 42,
        "Unknown top-level numeric field must be preserved",
    )


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_cutout_clamped_to_range(opts):
    """cutout outside [0, 1] must be clamped, not dropped."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    high = _wrap_block(
        '{"type": "chart", "chart_type": "pie", "labels": ["A","B"], '
        '"series": [{"name": "x", "values": [1,2]}], "cutout": 1.5}'
    )
    _, blocks = _parse_blocks(high)
    assert_eq(len(blocks), 1, "Pie with cutout=1.5 must still pass (clamped)")
    assert_eq(blocks[0]["cutout"], 1.0, f"cutout 1.5 must clamp to 1.0, got {blocks[0]['cutout']}")

    low = _wrap_block(
        '{"type": "chart", "chart_type": "pie", "labels": ["A","B"], '
        '"series": [{"name": "x", "values": [1,2]}], "cutout": -0.2}'
    )
    _, blocks = _parse_blocks(low)
    assert_eq(len(blocks), 1, "Pie with cutout=-0.2 must still pass (clamped)")
    assert_eq(blocks[0]["cutout"], 0.0, f"cutout -0.2 must clamp to 0.0, got {blocks[0]['cutout']}")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_cutout_in_range_preserved(opts):
    """cutout already inside [0, 1] must be preserved unchanged."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "pie", "labels": ["A","B"], '
        '"series": [{"name": "x", "values": [1,2]}], "cutout": 0.55}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, "Pie with valid cutout must pass")
    assert_eq(blocks[0]["cutout"], 0.55, f"In-range cutout must survive verbatim, got {blocks[0]['cutout']}")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_cutout_non_numeric_stripped(opts):
    """cutout that is not numeric must be stripped (chart still passes)."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "pie", "labels": ["A","B"], '
        '"series": [{"name": "x", "values": [1,2]}], "cutout": "half"}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, "Chart with non-numeric cutout must still pass")
    assert_true(
        "cutout" not in blocks[0],
        f"Non-numeric cutout must be stripped, got block: {blocks[0]}",
    )


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_stacked_unrecognized_value_stripped(opts):
    """stacked with an unrecognized value must be stripped (chart still passes)."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "bar", "labels": ["A","B"], '
        '"series": [{"name": "x", "values": [1,2]}], "stacked": "weird"}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, "Bar chart with bad stacked value must still pass")
    assert_true(
        "stacked" not in blocks[0],
        f"Unrecognized stacked value must be stripped, got block: {blocks[0]}",
    )


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_stacked_recognized_values_preserved(opts):
    """stacked: True/False/'auto' must all pass through preserved."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    for value, json_literal in [(True, "true"), (False, "false"), ("auto", '"auto"')]:
        text = _wrap_block(
            '{"type": "chart", "chart_type": "bar", "labels": ["A","B"], '
            '"series": [{"name": "x", "values": [1,2]}], "stacked": ' + json_literal + '}'
        )
        _, blocks = _parse_blocks(text)
        assert_eq(len(blocks), 1, f"stacked={value!r} must pass")
        assert_eq(
            blocks[0].get("stacked"), value,
            f"stacked={value!r} must be preserved, got {blocks[0].get('stacked')!r}",
        )


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_per_series_passthrough_fields_preserved(opts):
    """Per-series color / fill / smoothing must pass through unchanged."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "line", "labels": ["A","B"], '
        '"series": [{"name": "x", "values": [1,2], "color": "#ff0000", '
        '"fill": true, "smoothing": 0.4}]}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, "Chart with per-series passthrough fields must pass")
    s = blocks[0]["series"][0]
    assert_eq(s.get("color"), "#ff0000", f"per-series color preserved, got {s.get('color')!r}")
    assert_eq(s.get("fill"), True, "per-series fill preserved")
    assert_eq(s.get("smoothing"), 0.4, f"per-series smoothing preserved, got {s.get('smoothing')!r}")


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_colors_non_list_stripped(opts):
    """colors that is not a list (or null) must be stripped, chart still passes."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "pie", "labels": ["A","B"], '
        '"series": [{"name": "x", "values": [1,2]}], "colors": "red"}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, "Chart with non-list colors must still pass")
    assert_true(
        "colors" not in blocks[0],
        f"Non-list colors must be stripped, got block: {blocks[0]}",
    )


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_colors_null_preserved(opts):
    """colors=null is a valid 'use default palette' marker — preserved."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "pie", "labels": ["A","B"], '
        '"series": [{"name": "x", "values": [1,2]}], "colors": null}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, "Chart with colors=null must pass")
    assert_true(
        "colors" in blocks[0] and blocks[0]["colors"] is None,
        f"colors=null must survive verbatim, got block: {blocks[0]}",
    )


@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_chart_crosshair_tracking_coerced_to_bool(opts):
    """crosshair_tracking must be coerced to a python bool."""
    from mojo.apps.assistant.services.agent import _parse_blocks

    text = _wrap_block(
        '{"type": "chart", "chart_type": "line", "labels": ["A","B"], '
        '"series": [{"name": "x", "values": [1,2]}], "crosshair_tracking": 1}'
    )
    _, blocks = _parse_blocks(text)
    assert_eq(len(blocks), 1, "Chart with crosshair_tracking=1 must pass")
    assert_true(
        blocks[0].get("crosshair_tracking") is True,
        f"crosshair_tracking=1 must coerce to True, got {blocks[0].get('crosshair_tracking')!r}",
    )
