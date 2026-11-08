"""#6446: a foreign key declared sensitive is refused under every name the ORM
accepts for it — the link name, the column name, a path through other
records, and the reverse direction from the linked record — on every list
surface.

Here because it swaps filevault.VaultData's shared RestMeta to declare its
`user` foreign key sensitive, once as `user` and once as `user_id`: unsafe
under the parallel default tier. The swap is in this process only, so the
surfaces are called in-process rather than through opts.client (the testit
server would not see it; same as fk_attach_audit).
"""
import contextlib

from testit import helpers as th

LOOKUPS = ("", "__in", "__not", "__not_in", "__gt", "__gte", "__lt", "__lte",
           "__exact", "__isnull", "__startswith", "__contains")
DECLARATIONS = ("user", "user_id")


@contextlib.contextmanager
def _vault_declares(*names, **attrs):
    """VaultData with `names` added to SENSITIVE_FIELDS, restored after."""
    from mojo.apps.filevault.models import VaultData

    original = VaultData.RestMeta
    attrs["SENSITIVE_FIELDS"] = list(original.SENSITIVE_FIELDS) + list(names)
    VaultData.RestMeta = type("RestMeta", (original,), attrs)
    try:
        yield VaultData
    finally:
        VaultData.RestMeta = original


def _request(**data):
    from objict import objict
    from testit.helpers import get_mock_request

    request = get_mock_request()
    request.DATA = objict(data)
    return request


@th.django_unit_test("forward paths are refused under both spellings and every lookup")
@th.requires_app("mojo.apps.filevault")
def test_forward_paths_both_spellings(opts):
    from mojo.models.rest import is_sensitive_filter_path

    for declared in DECLARATIONS:
        with _vault_declares(declared) as VaultData:
            for base in ("user", "user_id", "user__id", "user__username"):
                for lookup in LOOKUPS:
                    key = base + lookup
                    assert is_sensitive_filter_path(VaultData, key), \
                        f"declared {declared!r}: VaultData?{key} must be refused"


@th.django_unit_test("multi-hop paths are refused under both spellings")
@th.requires_app("mojo.apps.filevault")
def test_multi_hop_paths(opts):
    from mojo.apps.filevault.models import VaultAccessLog
    from mojo.models.rest import is_sensitive_filter_path

    for declared in DECLARATIONS:
        with _vault_declares(declared):
            for key in ("vault_data__user", "vault_data__user_id", "vault_data__user__id",
                        "vault_data__user_id__in", "vault_data__user__isnull"):
                assert is_sensitive_filter_path(VaultAccessLog, key), \
                    f"declared {declared!r}: VaultAccessLog?{key} must be refused"


@th.django_unit_test("every path through the reverse of a sensitive FK is refused")
@th.requires_app("mojo.apps.filevault")
def test_reverse_paths(opts):
    from mojo.apps.account.models import User
    from mojo.models.rest import is_sensitive_filter_path

    for declared in DECLARATIONS:
        with _vault_declares(declared):
            for key in ("vault_data", "vault_data__id", "vault_data__isnull",
                        "vault_data__in", "vault_data__created__gte", "vault_data__name"):
                assert is_sensitive_filter_path(User, key), \
                    f"declared {declared!r}: User?{key} must be refused"


@th.django_unit_test("pk is refused when the primary key is declared")
@th.requires_app("mojo.apps.filevault")
def test_pk_alias(opts):
    from mojo.models.rest import is_sensitive_filter_path

    with _vault_declares("id") as VaultData:
        for key in ("pk", "pk__in", "pk__gt"):
            assert is_sensitive_filter_path(VaultData, key), \
                f"declared 'id': VaultData?{key} must be refused"


@th.django_unit_test("list, count and _stats parser drops the probe keys")
@th.requires_app("mojo.apps.filevault")
def test_build_rest_filters_drops(opts):
    from mojo.apps.account.models import User

    for declared in DECLARATIONS:
        with _vault_declares(declared) as VaultData:
            probes = {"user": "1", "user_id": "1", "user_id__gt": "0",
                      "user__isnull": "false", "user_id__not": "1"}
            filters, excludes = VaultData.build_rest_filters(None, probes)
            combined = list(filters) + list(excludes)
            assert not combined, f"declared {declared!r}: probe keys reached the queryset: {combined}"

            filters, excludes = User.build_rest_filters(
                None, {"vault_data__id": "1", "vault_data__isnull": "false"})
            combined = list(filters) + list(excludes)
            assert not combined, f"declared {declared!r}: reverse keys reached the queryset: {combined}"


@th.django_unit_test("sort by either spelling or the reverse name is ignored")
@th.requires_app("mojo.apps.filevault")
def test_sort_ignored(opts):
    from mojo.apps.account.models import User

    for declared in DECLARATIONS:
        with _vault_declares(declared) as VaultData:
            for sort in ("user", "-user", "user_id"):
                qs = VaultData.on_rest_list_sort(_request(sort=sort), VaultData.objects.all())
                assert not qs.query.order_by, \
                    f"declared {declared!r}: sort={sort} applied {qs.query.order_by}"
            qs = User.on_rest_list_sort(_request(sort="vault_data"), User.objects.all())
            assert not qs.query.order_by, \
                f"declared {declared!r}: sort=vault_data applied {qs.query.order_by}"


@th.django_unit_test("a SEARCH_FIELDS entry naming either spelling is dropped")
@th.requires_app("mojo.apps.filevault")
def test_search_field_dropped(opts):
    for declared in DECLARATIONS:
        for listed in ("user", "user_id"):
            with _vault_declares(declared, SEARCH_FIELDS=[listed]) as VaultData:
                qs = VaultData.on_rest_list_search(_request(search="1"), VaultData.objects.all())
                assert qs.query.is_empty(), \
                    f"declared {declared!r}: search over SEARCH_FIELDS=[{listed!r}] still ran"


@th.django_unit_test("_agg refuses fk__id under either declaration; histogram refuses a declared date")
@th.requires_app("mojo.apps.filevault")
def test_aggregation_refused(opts):
    from mojo import errors as me
    from mojo.models import rest_aggregation

    for declared in DECLARATIONS:
        with _vault_declares(declared) as VaultData:
            try:
                rest_aggregation._validate_field(VaultData, "user__id")
            except me.ValueException as err:
                assert "sensitive" in str(err), f"declared {declared!r}: wrong refusal: {err}"
            else:
                raise AssertionError(f"declared {declared!r}: _field=user__id was accepted")

    with _vault_declares("created") as VaultData:
        request = _request(_field="created", _bucket="day")
        try:
            rest_aggregation._agg_histogram(VaultData, request, VaultData.objects.all())
        except me.ValueException as err:
            assert "sensitive" in str(err), f"histogram: wrong refusal: {err}"
        else:
            raise AssertionError("_mode=histogram ran on a declared sensitive date field")


@th.django_unit_test("an undeclared FK still filters, sorts and aggregates")
@th.requires_app("mojo.apps.filevault")
def test_undeclared_unchanged(opts):
    from mojo.apps.filevault.models import VaultData
    from mojo.models import rest_aggregation

    filters, _ = VaultData.build_rest_filters(None, {"user": "1", "user_id__in": "1,2"})
    assert set(filters) == {"user", "user_id__in"}, f"undeclared FK filters were dropped: {filters}"
    qs = VaultData.on_rest_list_sort(_request(sort="user"), VaultData.objects.all())
    assert qs.query.order_by, "sort by an undeclared FK must still apply"
    assert rest_aggregation._validate_field(VaultData, "user__id") == "user__id", \
        "_field=user__id on an undeclared FK must be accepted"
