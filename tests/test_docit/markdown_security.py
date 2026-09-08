"""Security regressions for DocIt's Markdown renderer."""

TESTIT_TIER = "core"

from testit import helpers as th


RECURSIVE_EMPHASIS_PAYLOAD = "*" * 1000 + "a" + "*" * 1000


@th.django_unit_test()
def test_recursive_emphasis_nesting_is_bounded(opts):
    """Deep emphasis must not exhaust Python's recursion limit."""
    from mojo.apps.docit.services.markdown import MarkdownRenderer

    renderer = MarkdownRenderer()
    for mode in ("render_safe", "render"):
        try:
            html = getattr(renderer, mode)(RECURSIVE_EMPHASIS_PAYLOAD)
        except RecursionError as exc:
            assert False, f"{mode} must bound recursive emphasis nesting: {exc}"

        opening_tags = html.count("<em>") + html.count("<strong>")
        closing_tags = html.count("</em>") + html.count("</strong>")
        assert "a" in html, f"{mode} must preserve the payload text: {html!r}"
        assert opening_tags == closing_tags, \
            f"{mode} produced unbalanced emphasis tags: {html!r}"
        assert 0 < opening_tags <= 20, \
            f"{mode} must cap emphasis nesting at Mistune's safe depth: {opening_tags}"
        assert html.count("*") >= 1900, \
            f"{mode} must preserve delimiters beyond the nesting cap: {html!r}"
