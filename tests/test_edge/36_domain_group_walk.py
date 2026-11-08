"""
A tenant DNS caller can still filter certificates and vhosts by their domain's
group (#10421, follow-up to #8680).

#8680 drops a list filter that walks into a related model the caller cannot
list in full. `domain__group` walks into Domain, which a tenant cannot list in
full, so the only thing keeping it working is that it is the model's own
RestMeta.GROUP_FIELD path. These tests call the list endpoints over HTTP.

The proof that the filter is APPLIED rather than dropped is the non-matching
value: scoped to group A, `?domain__group=<B>` must return nothing. A dropped
filter would return A's rows.

The file lives in test_edge because a Vhost needs the declared edge pools,
which this package's files set up and run sequentially for.
"""

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, login, make_certificate, make_domain,
    make_group, make_group_member, make_vhost,
)


@th.django_unit_setup()
def setup_domain_group_walk(opts):
    cleanup()
    declare_pools()

    opts.group = make_group("edgewalk")
    opts.other_group = make_group("edgewalkother")
    _, opts.tenant_email, opts.tenant_pw, _ = make_group_member(
        ["view_dns"], group=opts.group)

    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="www")

    opts.other_domain = make_domain(group=opts.other_group)
    opts.other_cert = make_certificate(opts.other_domain)
    opts.other_vhost = make_vhost(opts.other_domain, opts.other_cert, label="www")


def _ids(opts, path, **params):
    params = dict({"group": opts.group.pk, "size": 50}, **params)
    resp = opts.client.get(path, params=params)
    assert resp.status_code == 200, (
        f"a tenant DNS caller could not list {path} with {params}: "
        f"{resp.status_code} {resp.body}")
    return {row.get("id") for row in (resp.json.get("data") or [])}


def _check_walk(opts, path, own_pk, other_pk, what):
    login(opts, opts.tenant_email, opts.tenant_pw)

    ids = _ids(opts, path, domain__group=opts.group.pk)
    assert own_pk in ids, \
        f"?domain__group=<own group> dropped the tenant's own {what}"
    assert other_pk not in ids, \
        f"?domain__group=<own group> returned another tenant's {what}"

    ids = _ids(opts, path, domain__group=opts.other_group.pk)
    assert not ids, (
        f"?domain__group=<other group> returned {what} rows {sorted(ids)}: the "
        f"group-scoping walk was dropped instead of applied")

    ids = _ids(opts, path, domain__group__in=f"{opts.group.pk},{opts.other_group.pk}")
    assert own_pk in ids and other_pk not in ids, (
        f"?domain__group__in=<own,other> must return only the tenant's own "
        f"{what}, got {sorted(ids)}")
    ids = _ids(opts, path, domain__group__in=str(opts.other_group.pk))
    assert not ids, (
        f"?domain__group__in=<other group> returned {what} rows {sorted(ids)}: "
        f"the group-scoping walk was dropped instead of applied")


@th.django_unit_test("a tenant DNS caller filters certificates by domain__group")
def test_certificate_domain_group_filter(opts):
    _check_walk(opts, "/api/dnsman/certificate",
                opts.certificate.pk, opts.other_cert.pk, "certificate")


@th.django_unit_test("a tenant DNS caller filters vhosts by domain__group")
def test_vhost_domain_group_filter(opts):
    _check_walk(opts, "/api/edge/vhost",
                opts.vhost.pk, opts.other_vhost.pk, "vhost")
