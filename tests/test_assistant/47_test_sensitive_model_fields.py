"""Maestro item #1552 — the model tools honor RestMeta.SENSITIVE_FIELDS on input.

#1059 decides what a row SHOWS (the server-selected graph). This file is about
what a caller may ASK BY: filters, ordering, aggregate sources, group_by and
export `fields`. A column the model declares sensitive must not be usable as
any of them, or its value can be read one comparison at a time.

The handlers are called in-process. The two fixtures that change a RestMeta
restore it in `finally`, and only this package's thread can see the change.
"""
import contextlib

from testit import helpers as th

TESTIT_TIER = "bug"

EMAIL = "senstest_admin@test.com"
EDATA = "SENSTEST-EDATA-MARKER"
EKEY = "SENSTEST-EKEY-MARKER"
PROBE_VALUE = "SENSTEST-PROBE-VALUE"
CATEGORY = "assistant_sensitive_field"

VAULT = {"app_name": "filevault", "model_name": "VaultData"}
IPSET = {"app_name": "incident", "model_name": "IPSet"}
COUNT_ROWS = [{"field": "id", "func": "count", "alias": "total"}]


@contextlib.contextmanager
def _vault_rest_meta(**attrs):
    """Temporarily replace attributes on filevault.VaultData's RestMeta."""
    from mojo.apps.filevault.models import VaultData

    original = VaultData.RestMeta
    VaultData.RestMeta = type("RestMeta", (original,), attrs)
    try:
        yield VaultData
    finally:
        VaultData.RestMeta = original


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_app("mojo.apps.filevault")
@th.requires_app("mojo.apps.fileman")
@th.requires_app("mojo.apps.incident")
def setup_sensitive_fields(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.filevault.models import VaultData
    from mojo.apps.fileman.models import FileManager, File

    User.objects.filter(email=EMAIL).delete()
    opts.admin = User.objects.create_user(username=EMAIL, email=EMAIL, password="pass123")
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ("view_admin", "view_security", "manage_vault", "manage_files", "manage_users"):
        opts.admin.add_permission(perm)

    Group.objects.filter(name="senstest_group").delete()
    opts.group = Group.objects.create(name="senstest_group", kind="org")

    VaultData.objects.filter(name__startswith="senstest_").delete()
    for i in range(3):
        VaultData.objects.create(
            user=opts.admin, group=opts.group, name=f"senstest_{i}",
            ekey=f"{EKEY}-{i}", edata=f"{EDATA}-{i}", metadata={"n": i},
        )
    opts.vault_filters = {"name__startswith": "senstest_"}

    FileManager.objects.filter(name="senstest_fm").delete()
    opts.fm = FileManager.objects.create(
        name="senstest_fm", backend_type="file",
        backend_url="filesystem:///tmp/senstest_files",
        is_default=True, is_active=True, user=opts.admin,
    )
    File.objects.filter(filename__startswith="export_filevault_VaultData_").delete()


def _describe(params, user):
    from mojo.apps.assistant.services.tools.models import _tool_describe_model
    return _tool_describe_model(dict(params), user)


def _query(params, user, **extra):
    from mojo.apps.assistant.services.tools.models import _tool_query_model
    return _tool_query_model(dict(params, **extra), user)


def _aggregate(params, user, **extra):
    from mojo.apps.assistant.services.tools.models import _tool_aggregate_model
    extra.setdefault("aggregations", COUNT_ROWS)
    return _tool_aggregate_model(dict(params, **extra), user)


def _export(params, user, **extra):
    from mojo.apps.assistant.services.tools.models import _tool_export_data
    return _tool_export_data(dict(params, **extra), user)


def _vault_files(opts):
    from mojo.apps.fileman.models import File
    return File.objects.filter(filename__startswith="export_filevault_VaultData_", user=opts.admin)


def _file_text(f):
    with f.file_manager.backend.open(f.storage_file_path) as fh:
        return fh.read().decode("utf-8")


def _events(opts):
    from mojo.apps.incident.models import Event
    return Event.objects.filter(uid=opts.admin.pk, category=CATEGORY)


def _refused(result, what):
    assert "error" in result, f"{what} must be refused, got: {result}"
    assert "not allowed" in result["error"] or "sensitive" in result["error"], \
        f"{what}: the refusal should say it is not allowed, got: {result['error']}"
    assert PROBE_VALUE not in result["error"], f"{what}: the refusal repeats the attempted value: {result['error']}"


# ---------------------------------------------------------------------------
# Declared fields, on each input surface
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_declared_field_cannot_be_filtered(opts):
    """A declared sensitive column is refused as a filter by all three tools."""
    cases = [
        (VAULT, "edata__startswith"), (VAULT, "edata"), (VAULT, "ekey__icontains"),
        (IPSET, "source_key"), (IPSET, "source_key__startswith"),
    ]
    for params, key in cases:
        label = f"{params['model_name']}.{key}"
        filters = {key: PROBE_VALUE}
        _refused(_query(params, opts.admin, filters=filters), f"query_model filter on {label}")
        _refused(_query(params, opts.admin, filters=filters, count_only=True),
                 f"query_model count_only filter on {label}")
        _refused(_aggregate(params, opts.admin, filters=filters), f"aggregate_model filter on {label}")
        _refused(_export(params, opts.admin, filters=filters), f"export_data filter on {label}")


@th.django_unit_test()
def test_declared_field_cannot_be_ordered(opts):
    """Ordering by a secret column reveals the order of values the caller cannot read."""
    for params, field in ((VAULT, "edata"), (VAULT, "-ekey"), (IPSET, "source_key")):
        label = f"{params['model_name']} by {field}"
        _refused(_query(params, opts.admin, ordering=field), f"query_model ordering {label}")
        _refused(_export(params, opts.admin, ordering=field), f"export_data ordering {label}")


@th.django_unit_test()
def test_declared_field_cannot_be_aggregated_or_grouped(opts):
    """min/max return a secret value outright; count and group_by reveal it by parts."""
    for params, field in ((VAULT, "edata"), (VAULT, "ekey"), (IPSET, "source_key")):
        label = f"{params['model_name']}.{field}"
        for func in ("count", "count_distinct", "min", "max"):
            result = _aggregate(params, opts.admin, aggregations=[{"field": field, "func": func}])
            _refused(result, f"aggregate_model {func} of {label}")
            assert EDATA not in str(result) and EKEY not in str(result), \
                f"aggregate_model {func} of {label} returned a stored value: {result}"
        _refused(_aggregate(params, opts.admin, group_by=[field]), f"aggregate_model group_by {label}")


@th.django_unit_test()
def test_safe_inputs_and_aliases_still_work(opts):
    """The rule names sensitive model paths. Everything else behaves as before."""
    rows = _query(VAULT, opts.admin, filters=opts.vault_filters, ordering="name")
    assert "error" not in rows, f"A filter and ordering on 'name' should work: {rows.get('error')}"
    assert rows["count"] == 3, f"Expected the 3 vault rows, got: {rows['count']}"
    assert EDATA not in str(rows) and EKEY not in str(rows), "A vault row returned a stored secret"

    flat = _aggregate(VAULT, opts.admin, filters=opts.vault_filters,
                      aggregations=[{"field": "id", "func": "count", "alias": "ekey"}])
    assert "error" not in flat, f"An alias is an output name, not a model path: {flat.get('error')}"
    assert flat["results"]["ekey"] == 3, f"Expected 3 under the alias, got: {flat['results']}"

    grouped = _aggregate(VAULT, opts.admin, filters=opts.vault_filters, group_by=["name"],
                         having={"total__gte": 1}, ordering="-total")
    assert "error" not in grouped, f"group_by, having and ordering by alias should work: {grouped.get('error')}"
    assert grouped["count"] == 3, f"Expected 3 groups, got: {grouped['count']}"

    by_owner = _aggregate(VAULT, opts.admin, filters=opts.vault_filters, group_by=["user"], ordering="user_id")
    assert "error" not in by_owner, f"Grouping by an ordinary foreign key should work: {by_owner.get('error')}"
    assert by_owner["results"][0]["total"] == 3, f"Expected one group of 3, got: {by_owner['results']}"


# ---------------------------------------------------------------------------
# Relations, foreign-key spellings, the baseline, JSON columns
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_related_sensitive_paths_use_the_related_models_declaration(opts):
    """Reached forward or in reverse, VaultData's own declaration decides."""
    forward = {"app_name": "filevault", "model_name": "VaultAccessLog"}
    reverse = {"app_name": "account", "model_name": "User"}
    for params, key in ((forward, "vault_data__edata__startswith"), (forward, "vault_data__ekey"),
                        (forward, "vault_data_id__edata"),
                        (reverse, "vault_data__edata__startswith"), (reverse, "vault_data__ekey")):
        label = f"{params['model_name']} {key}"
        _refused(_query(params, opts.admin, filters={key: PROBE_VALUE}), f"query_model filter {label}")
        _refused(_aggregate(params, opts.admin, filters={key: PROBE_VALUE}), f"aggregate_model filter {label}")

    ok = _query(forward, opts.admin, filters={"vault_data__name": "senstest_0"}, count_only=True)
    assert "error" not in ok, f"A related field nobody declares sensitive should still filter: {ok.get('error')}"


@th.django_unit_test()
def test_foreign_key_is_refused_under_both_spellings(opts):
    """`user` and `user_id` are one column. Declaring either refuses both."""
    from mojo.apps.filevault.models import VaultData

    declared = list(VaultData.RestMeta.SENSITIVE_FIELDS)
    for name in ("user", "user_id"):
        with _vault_rest_meta(SENSITIVE_FIELDS=declared + [name]):
            for key in ("user", "user_id", "user__in", "user_id__in", "user__username", "user_id__gte"):
                _refused(_query(VAULT, opts.admin, filters={key: opts.admin.pk}),
                         f"declared {name!r}: query_model filter on {key}")
                _refused(_aggregate(VAULT, opts.admin, filters={key: opts.admin.pk}),
                         f"declared {name!r}: aggregate_model filter on {key}")
            for field in ("user", "user_id"):
                _refused(_query(VAULT, opts.admin, ordering=field), f"declared {name!r}: ordering by {field}")
                _refused(_aggregate(VAULT, opts.admin, group_by=[field]), f"declared {name!r}: group_by {field}")
                _refused(_aggregate(VAULT, opts.admin, aggregations=[{"field": field, "func": "count"}]),
                         f"declared {name!r}: count of {field}")
            names = [f["name"] for f in _describe(VAULT, opts.admin)["fields"]]
            assert "user" not in names, f"declared {name!r}: describe_model still lists the key: {names}"
    assert VaultData.RestMeta.SENSITIVE_FIELDS == declared, "The fixture did not restore RestMeta"
    ok = _query(VAULT, opts.admin, filters={"user": opts.admin.pk}, count_only=True)
    assert ok.get("count") == 3, f"With the fixture gone, a filter on the owner works again: {ok}"


@th.django_unit_test()
def test_secrets_blob_is_refused_on_a_model_that_declares_nothing(opts):
    """`mojo_secrets` is sensitive on every model, by the framework's baseline."""
    from mojo.apps.fileman.models import FileManager

    assert not getattr(FileManager.RestMeta, "SENSITIVE_FIELDS", None), \
        "This test needs a model with no SENSITIVE_FIELDS of its own"
    params = {"app_name": "fileman", "model_name": "FileManager"}
    _refused(_query(params, opts.admin, filters={"mojo_secrets__startswith": PROBE_VALUE}),
             "query_model filter on FileManager.mojo_secrets")
    _refused(_query(params, opts.admin, ordering="mojo_secrets"), "ordering by FileManager.mojo_secrets")
    _refused(_aggregate(params, opts.admin, group_by=["mojo_secrets"]), "group_by FileManager.mojo_secrets")
    names = [f["name"] for f in _describe(params, opts.admin)["fields"]]
    assert "mojo_secrets" not in names, f"describe_model lists the secrets blob: {names}"
    assert "name" in names, f"describe_model should still list ordinary fields: {names}"


@th.django_unit_test()
def test_json_column_is_not_a_lookup_surface(opts):
    """A JSON column takes any key the caller invents, so it cannot be asked by."""
    for key in ("metadata", "metadata__n", "metadata__anything__startswith"):
        _refused(_query(VAULT, opts.admin, filters={key: PROBE_VALUE}), f"query_model filter on {key}")
    _refused(_query(VAULT, opts.admin, ordering="metadata"), "ordering by a JSON column")
    _refused(_aggregate(VAULT, opts.admin, group_by=["metadata"]), "group_by a JSON column")


# ---------------------------------------------------------------------------
# describe_model
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_describe_lists_only_what_may_be_asked_by(opts):
    """`fields` drops what cannot be queried. `serialization` is #1059's and is untouched."""
    before = _events(opts).count()
    result = _describe(VAULT, opts.admin)
    assert "error" not in result, f"describe_model should succeed: {result.get('error')}"
    names = [f["name"] for f in result["fields"]]
    for hidden in ("ekey", "edata", "hashed_password", "metadata"):
        assert hidden not in names, f"describe_model advertises '{hidden}' as queryable: {names}"
    for shown in ("id", "name", "description", "created", "user", "group"):
        assert shown in names, f"describe_model should still list '{shown}': {names}"
    assert result["serialization"]["graph"] == "default", \
        f"VaultData has no ai graph, so default is selected: {result['serialization']}"
    assert "metadata" in result["serialization"]["fields"], \
        f"The row shape is the graph's and must not be filtered here: {result['serialization']['fields']}"

    ipset = [f["name"] for f in _describe(IPSET, opts.admin)["fields"]]
    assert "source_key" not in ipset, f"describe_model advertises IPSet.source_key: {ipset}"
    assert _events(opts).count() == before, "Reading a description is not a probe and must report nothing"


# ---------------------------------------------------------------------------
# Export fields
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_export_fields_refuse_a_sensitive_name_before_any_file(opts):
    """Even where the model's own ai graph shows a column, `fields` cannot name it."""
    before = _vault_files(opts).count()
    cases = (["name", "edata"], ["ekey"], ["vault_data.edata"], ["user.auth_key"], ["edata__startswith"])
    for fields in cases:
        result = _export(VAULT, opts.admin, filters=opts.vault_filters, fields=fields)
        assert "error" in result, f"export_data must refuse fields={fields}, got: {result}"

    graphs = {"default": {"fields": ["id", "name"]}, "ai": {"fields": ["id", "name", "edata"]}}
    with _vault_rest_meta(GRAPHS=graphs):
        for fields in (["edata"], ["name", "edata"]):
            _refused(_export(VAULT, opts.admin, filters=opts.vault_filters, fields=fields),
                     f"export_data fields={fields} with edata in the ai graph")
    assert _vault_files(opts).count() == before, "A refused export must not create a File row"


@th.django_unit_test()
def test_authored_ai_graph_is_not_redacted(opts):
    """The graph is the row authority (#1059). This item does not edit what it returns."""
    graphs = {"default": {"fields": ["id", "name"]}, "ai": {"fields": ["id", "name", "edata"]}}
    with _vault_rest_meta(GRAPHS=graphs):
        rows = _query(VAULT, opts.admin, filters=opts.vault_filters, ordering="name")
        assert "error" not in rows, f"query_model should succeed: {rows.get('error')}"
        assert rows["results"][0].get("edata") == f"{EDATA}-0", \
            f"A column the author put in the ai graph is returned as authored, got: {rows['results'][0]}"

        result = _export(VAULT, opts.admin, filters=opts.vault_filters, ordering="name")
        assert "error" not in result, f"export_data with no fields should succeed: {result.get('error')}"
        text = _file_text(_vault_files(opts).order_by("-pk").first())
        assert f"{EDATA}-0" in text, "The export should carry the ai graph's columns as authored"


@th.django_unit_test()
def test_export_can_narrow_to_a_json_column_the_graph_returns(opts):
    """`fields` are output names. The lookup rule for JSON columns does not apply to them."""
    result = _export(VAULT, opts.admin, filters=opts.vault_filters, ordering="name",
                     fields=["name", "metadata"])
    assert "error" not in result, f"Narrowing to name and metadata should work: {result.get('error')}"
    assert result["row_count"] == 3, f"Expected 3 rows, got: {result['row_count']}"
    header = _file_text(_vault_files(opts).order_by("-pk").first()).splitlines()[0]
    assert header == "Name,Metadata", f"Unexpected header: {header}"


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_each_active_probe_reports_once_and_never_the_value(opts):
    """One level-7 event per refused call, naming the model and the path only."""
    probes = [
        ("query filter", lambda: _query(VAULT, opts.admin, filters={"edata__startswith": PROBE_VALUE}), "edata__startswith"),
        ("query ordering", lambda: _query(VAULT, opts.admin, ordering="-edata"), "edata"),
        ("aggregate filter", lambda: _aggregate(VAULT, opts.admin, filters={"ekey": PROBE_VALUE}), "ekey"),
        ("aggregate source", lambda: _aggregate(VAULT, opts.admin, aggregations=[{"field": "edata", "func": "max"}]), "edata"),
        ("aggregate group_by", lambda: _aggregate(VAULT, opts.admin, group_by=["ekey"]), "ekey"),
        ("export filter", lambda: _export(VAULT, opts.admin, filters={"edata": PROBE_VALUE}), "edata"),
        ("export ordering", lambda: _export(VAULT, opts.admin, ordering="ekey"), "ekey"),
        ("export fields", lambda: _export(VAULT, opts.admin, fields=["edata"]), "edata"),
        ("related filter", lambda: _query({"app_name": "account", "model_name": "User"}, opts.admin,
                                          filters={"vault_data__edata": PROBE_VALUE}), "vault_data__edata"),
    ]
    for label, probe, path in probes:
        before = _events(opts).count()
        result = probe()
        assert "error" in result, f"{label} must be refused, got: {result}"
        assert _events(opts).count() == before + 1, \
            f"{label} must report exactly one event, before={before} after={_events(opts).count()}"
        event = _events(opts).latest("pk")
        assert event.level == 7, f"{label}: the event should be level 7, got {event.level}"
        text = f"{event.title} {event.details}"
        assert path in text, f"{label}: the event should name the path '{path}', got: {text}"
        assert PROBE_VALUE not in text and PROBE_VALUE not in str(event.metadata), \
            f"{label}: the event carries the attempted value: {text} {event.metadata}"
