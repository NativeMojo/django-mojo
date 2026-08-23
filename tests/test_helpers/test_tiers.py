"""Named tiers, presets, and the budget ratchet (maestro #2790).

Covers the tier mechanism added in Phase 1: TESTIT `tier` tag resolution and
the legacy mapping, `--tier` preset selection, per-file/per-function tier
extraction, the strict (core) scanner grammar, the tier-aware package-state and
cold-budget evaluation, and the wall-clock budget check.

Every scanner fixture is source text scanned in isolation — the engine never
imports or executes what it scans.
"""
import textwrap

from objict import objict
from testit import helpers as th


# ---------------------------------------------------------------------------
# _resolve_tags — the legacy → tier mapping
# ---------------------------------------------------------------------------
@th.unit_test("tiers: default_core maps to the framework bucket")
def test_resolve_tags_default_core(opts):
    from testit import runner
    assert runner._resolve_tags({"default_core": True}) == {"framework"}, (
        "a default_core package must land in the framework bucket")


@th.unit_test("tiers: requires_extra maps to its opt-in tags")
def test_resolve_tags_requires_extra(opts):
    from testit import runner
    assert runner._resolve_tags({"requires_extra": ["slow"]}) == {"slow"}, (
        "a requires_extra package keeps its opt-in tags")


@th.unit_test("tiers: an empty/permissive config defaults to framework")
def test_resolve_tags_empty(opts):
    from testit import runner
    assert runner._resolve_tags({}) == {"framework"}, (
        "a package with no tier and no requires_extra ran in the default tier "
        "historically — it must map to framework so it still runs by default")


@th.unit_test("tiers: explicit tier plus requires_extra unions both")
def test_resolve_tags_tier_and_extra(opts):
    from testit import runner
    assert runner._resolve_tags({"tier": "admin", "requires_extra": ["slow"]}) \
        == {"admin", "slow"}, "tier and requires_extra tags must union"


# ---------------------------------------------------------------------------
# _selected_tags / _selected_preset_label — preset resolution
# ---------------------------------------------------------------------------
def _opts(**kw):
    base = dict(tiers=[], all=False, extra_list=[])
    base.update(kw)
    return objict(base)


@th.unit_test("tiers: the default preset is framework = {core, framework, bug}")
def test_selected_default_framework(opts):
    from testit import runner
    tags, select_all = runner._selected_tags(_opts())
    assert tags == {"core", "framework", "bug"} and not select_all, (
        f"bare run must select the framework preset, got {tags} all={select_all}")


@th.unit_test("tiers: --tier core selects only the core bucket")
def test_selected_core(opts):
    from testit import runner
    tags, select_all = runner._selected_tags(_opts(tiers=["core"]))
    assert tags == {"core"} and not select_all, f"got {tags} all={select_all}"


@th.unit_test("tiers: --all / --tier all selects everything")
def test_selected_all(opts):
    from testit import runner
    _t, sa1 = runner._selected_tags(_opts(all=True))
    _t2, sa2 = runner._selected_tags(_opts(tiers=["all"]))
    assert sa1 and sa2, "both --all and --tier all must select every package"


@th.unit_test("tiers: unknown names are literal buckets")
def test_selected_literal_buckets(opts):
    from testit import runner
    tags, select_all = runner._selected_tags(_opts(tiers=["admin", "edge"]))
    assert tags == {"admin", "edge"} and not select_all, f"got {tags}"


@th.unit_test("tiers: --extra adds ad-hoc tags on top of the preset")
def test_selected_extra_additive(opts):
    from testit import runner
    tags, _sa = runner._selected_tags(_opts(extra_list=["slow"]))
    assert tags == {"core", "framework", "bug", "slow"}, (
        f"--extra slow must add slow to the default framework preset, got {tags}")


@th.unit_test("tiers: preset label names core/framework/all/literal")
def test_preset_label(opts):
    from testit import runner
    assert runner._selected_preset_label(_opts()) == "framework"
    assert runner._selected_preset_label(_opts(tiers=["core"])) == "core"
    assert runner._selected_preset_label(_opts(all=True)) == "all"
    assert runner._selected_preset_label(_opts(tiers=["edge", "admin"])) == "admin+edge", (
        "multiple literal buckets sort into a stable label")


# ---------------------------------------------------------------------------
# Per-file / per-function tier extraction
# ---------------------------------------------------------------------------
@th.unit_test("tiers: TESTIT_TIER and @th.tier decorators are both extracted")
def test_tier_extraction(opts):
    import ast
    from testit import isolation
    tree = ast.parse(textwrap.dedent("""
        TESTIT_TIER = "bug"
        from testit import helpers as th

        @th.tier("core")
        def test_a(opts):
            pass

        class Group:
            @th.tier("admin")
            def test_b(self):
                pass
    """))
    tiers = isolation._extract_tiers_from_tree(tree)
    assert tiers == {"bug", "core", "admin"}, (
        f"file-level and function-level (incl. in-class) tags must be found, got {tiers}")


@th.unit_test("tiers: @th.tier marks and propagates through unit_test")
def test_tier_decorator_propagates(opts):
    @th.tier("bug")
    @th.unit_test("inner")
    def outer(o):
        pass
    assert getattr(outer, "_tier", None) == "bug", (
        "@th.tier outermost must leave _tier on the final object")

    @th.django_unit_test("named")
    @th.tier("bug")
    def inner(o):
        pass
    assert getattr(inner, "_tier", None) == "bug", (
        "@th.tier innermost must be propagated up by django_unit_test")


# ---------------------------------------------------------------------------
# Strict (core-tier) scanner grammar
# ---------------------------------------------------------------------------
def _codes(source, strict):
    from testit import isolation
    return sorted({v.code for v in isolation.scan_source(
        textwrap.dedent(source), filename="<fixture>", strict=strict)})


@th.unit_test("tiers: Setting.set of a protected key is hot only in strict mode")
def test_setting_set_protected(opts):
    src = """
        from mojo.apps.account.models import Setting
        def test_x(opts):
            Setting.set("SECRET_KEY", "x")
    """
    assert "protected_setting_write" not in _codes(src, strict=False), (
        "framework scan must not see Setting.set — it stays byte-identical")
    assert "protected_setting_write" in _codes(src, strict=True), (
        "core scan must flag Setting.set of a protected key")


@th.unit_test("tiers: Setting.remove of a protected-prefix key is hot in strict")
def test_setting_remove_protected_prefix(opts):
    src = """
        from mojo.apps.account.models import Setting
        def test_x(opts):
            Setting.remove("EDGE_POOLS")
    """
    assert "protected_setting_write" in _codes(src, strict=True), (
        "EDGE_ is a protected prefix — Setting.remove of it must be flagged")


@th.unit_test("tiers: Setting.set of a TESTIT_ key is always allowed")
def test_setting_set_reserved_key(opts):
    src = """
        from mojo.apps.account.models import Setting
        def test_x(opts):
            Setting.set("TESTIT_FIXTURE", "x")
    """
    assert "protected_setting_write" not in _codes(src, strict=True), (
        "a TESTIT_-reserved key can never touch production config")


@th.unit_test("tiers: a dynamic Setting.set key is unresolved (fail-closed) in strict")
def test_setting_set_dynamic_key(opts):
    src = """
        from mojo.apps.account.models import Setting
        def test_x(opts, key):
            Setting.set(key, "x")
    """
    assert "protected_setting_unresolved" in _codes(src, strict=True), (
        "a dynamic key cannot be proven outside the protected roster")


@th.unit_test("tiers: server_settings() is a server_reload violation only in strict")
def test_server_settings_reload(opts):
    src = """
        from testit import helpers as th
        def test_x(opts):
            th.server_settings(FOO=1)
    """
    assert "server_reload" not in _codes(src, strict=False), (
        "framework scan must not flag server_settings")
    assert "server_reload" in _codes(src, strict=True), (
        "core tier forbids server_settings — it freezes every parallel worker")


# ---------------------------------------------------------------------------
# Tier-aware package-state evaluation
# ---------------------------------------------------------------------------
def _v():
    from testit import isolation
    return isolation.violation("django_settings_mutation", "<f>", 1, "x")


@th.unit_test("tiers: a core package may not be serial")
def test_eval_core_forbids_serial(opts):
    from testit import isolation
    problems = isolation.evaluate_package_state(
        {"tier": "core", "serial": True}, [], origin="django_mojo", has_config=True)
    assert any("may not be serial" in p for p in problems), (
        f"core forbids serial, got {problems}")


@th.unit_test("tiers: a core package with any violation fails")
def test_eval_core_forbids_violations(opts):
    from testit import isolation
    problems = isolation.evaluate_package_state(
        {"tier": "core"}, [_v()], origin="django_mojo", has_config=True)
    assert any("strictest bucket" in p for p in problems), (
        f"core forbids all violations, got {problems}")


@th.unit_test("tiers: a framework package with a violation fails (parallel ring)")
def test_eval_framework_forbids_violations(opts):
    from testit import isolation
    problems = isolation.evaluate_package_state(
        {"tier": "framework"}, [_v()], origin="django_mojo", has_config=True)
    assert any("parallel ring" in p for p in problems), (
        f"framework forbids mutation, got {problems}")


@th.unit_test("tiers: an opt-in bucket tolerates violations only when serial")
def test_eval_opt_in_serial(opts):
    from testit import isolation
    ok = isolation.evaluate_package_state(
        {"tier": "extended", "serial": True}, [_v()],
        origin="django_mojo", has_config=True)
    assert ok == [], f"a serial extended package may mutate, got {ok}"
    bad = isolation.evaluate_package_state(
        {"tier": "extended"}, [_v()], origin="django_mojo", has_config=True)
    assert any("must be\n" in p or "must be serial" in p for p in bad), (
        f"a non-serial extended package that mutates must fail, got {bad}")


@th.unit_test("tiers: declaring both tier and default_core is an error")
def test_eval_one_vocabulary(opts):
    from testit import isolation
    problems = isolation.evaluate_package_state(
        {"tier": "framework", "default_core": True}, [],
        origin="django_mojo", has_config=True)
    assert any("one vocabulary" in p for p in problems), (
        f"tier + default_core must be rejected, got {problems}")


@th.unit_test("tiers: a core package may not also declare requires_extra")
def test_eval_core_forbids_requires_extra(opts):
    from testit import isolation
    problems = isolation.evaluate_package_state(
        {"tier": "core", "requires_extra": ["slow"]}, [],
        origin="django_mojo", has_config=True)
    assert any("cannot also declare requires_extra" in p for p in problems), (
        f"core + requires_extra is contradictory, got {problems}")


# ---------------------------------------------------------------------------
# Per-file core-tag lifts the whole package to strict scanning (fail-open fix)
# ---------------------------------------------------------------------------
@th.unit_test("tiers: a core-tagged file makes its whole package strict-scanned")
def test_core_tag_lifts_package_to_strict(opts):
    import os
    import tempfile
    from testit import isolation
    with tempfile.TemporaryDirectory() as d:
        # A test file tagged into core, plus a helper that mutates shared state.
        with open(os.path.join(d, "1_core_test.py"), "w") as fh:
            fh.write('from testit import helpers as th\n'
                     '@th.tier("core")\n'
                     'def test_a(opts):\n    pass\n')
        with open(os.path.join(d, "_helper.py"), "w") as fh:
            fh.write('from mojo.apps.account.models import Setting\n'
                     'def prime():\n    Setting.set("SECRET_KEY", "x")\n')
        assert isolation._package_has_core_tag(d), (
            "the package carries a core tag and must be detected")
        scanned = isolation.scan_package(d)   # strict NOT passed explicitly
        codes = sorted({v.code for v in scanned.violations})
        assert "protected_setting_write" in codes, (
            "a core tag must lift the whole package (helpers included) to strict "
            f"scanning so the helper's Setting.set is caught, got {codes}")


@th.unit_test("tiers: a package with no core tag stays non-strict (byte-identical)")
def test_no_core_tag_stays_non_strict(opts):
    import os
    import tempfile
    from testit import isolation
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "1_fw_test.py"), "w") as fh:
            fh.write('from mojo.apps.account.models import Setting\n'
                     'def test_a(opts):\n    Setting.set("SECRET_KEY", "x")\n')
        assert not isolation._package_has_core_tag(d), "no core tag present"
        scanned = isolation.scan_package(d)
        codes = sorted({v.code for v in scanned.violations})
        assert "protected_setting_write" not in codes, (
            "without a core tag the Setting.set classmethod grammar must not "
            f"fire — framework scan stays byte-identical, got {codes}")


# ---------------------------------------------------------------------------
# Tier-aware cold-budget evaluation
# ---------------------------------------------------------------------------
@th.unit_test("tiers: a core package may not declare a cold_budget")
def test_cold_core_no_budget(opts):
    from testit import isolation
    problems = isolation.evaluate_cold_budget(
        {"tier": "core", "cold_budget": 3}, [], origin="django_mojo", has_config=True)
    assert any("may not declare a cold_budget" in p for p in problems), (
        f"core forbids a cold_budget, got {problems}")


@th.unit_test("tiers: a framework package uses the two-sided ratchet")
def test_cold_framework_ratchet(opts):
    from testit import isolation
    cold = [isolation.violation("patch_shared", "<f>", 1, "mojo.apps.x")]
    over = isolation.evaluate_cold_budget(
        {"tier": "framework", "cold_budget": 0}, cold,
        origin="django_mojo", has_config=True)
    assert any("exceed" in p for p in over), f"over-budget must fail, got {over}"
    exact = isolation.evaluate_cold_budget(
        {"tier": "framework", "cold_budget": 1}, cold,
        origin="django_mojo", has_config=True)
    assert exact == [], f"exact budget must pass, got {exact}"


@th.unit_test("tiers: opt-in buckets are exempt from the cold ratchet")
def test_cold_opt_in_exempt(opts):
    from testit import isolation
    cold = [isolation.violation("patch_shared", "<f>", 1, "mojo.apps.x")]
    problems = isolation.evaluate_cold_budget(
        {"tier": "extended"}, cold, origin="django_mojo", has_config=True)
    assert problems == [], f"opt-in buckets are serial, not budgeted, got {problems}"


# ---------------------------------------------------------------------------
# Wall-clock budget check
# ---------------------------------------------------------------------------
@th.unit_test("tiers: the preset budget flags over and stale, tolerating the band")
def test_budget_over_and_stale(opts):
    from testit import runner
    o = objict(selected_preset="core", _time_budgets={},
               test_modules=[], ignore_modules=[])
    # core budget 30; over at >37.5, stale at <18.
    over = runner._compute_budget_violations(o, {"duration": 45, "modules": {}}, None)
    assert any(v["kind"] == "over" for v in over), f"45s must be over 30s, got {over}"
    stale = runner._compute_budget_violations(o, {"duration": 10, "modules": {}}, None)
    assert any(v["kind"] == "stale" for v in stale), f"10s must be stale, got {stale}"
    ok = runner._compute_budget_violations(o, {"duration": 25, "modules": {}}, None)
    assert ok == [], f"25s is within the tolerance band, got {ok}"


@th.unit_test("tiers: a partial (-t) run does not evaluate the whole-suite budget")
def test_budget_skips_partial_runs(opts):
    from testit import runner
    o = objict(selected_preset="core", _time_budgets={},
               test_modules=["test_helpers"], ignore_modules=[])
    result = runner._compute_budget_violations(o, {"duration": 999, "modules": {}}, None)
    assert result == [], f"a -t run must not fail the preset budget, got {result}"


@th.unit_test("tiers: a per-package time_budget flags an over-budget module")
def test_budget_per_package(opts):
    from testit import runner
    o = objict(selected_preset="framework", _time_budgets={"test_slowpkg": 5},
               test_modules=[], ignore_modules=[])
    report = {"duration": 30, "modules": {"test_slowpkg": {"duration": 20}}}
    result = runner._compute_budget_violations(o, report, None)
    assert any(v["scope"] == "package" and v["name"] == "test_slowpkg"
               for v in result), f"per-package time_budget must flag, got {result}"
