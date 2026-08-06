import mojo.decorators as md
from mojo import errors as me

from mojo.apps.dnsman.models import AcmeDelegation, Domain
from mojo.apps.dnsman.models.acme_delegation import STATE_RETIRED
from mojo.apps.dnsman.services import delegation


def _payload(row):
    """The complete public shape.  No hub credential or cleanup internals."""
    return dict(
        id=row.pk,
        created=row.created.isoformat() if row.created else None,
        modified=row.modified.isoformat() if row.modified else None,
        domain=row.domain_id,
        domain_name=row.domain_name,
        source=row.source_name,
        target=row.target_name,
        state=row.state,
        verified_at=row.verified_at.isoformat() if row.verified_at else None,
        last_error_code=row.last_error_code,
    )


def _row(request, pk, perms="VIEW_PERMS"):
    not_found = lambda: me.ValueException(
        "ACME delegation not found", code=404, status=404)
    row = AcmeDelegation.objects.filter(pk=pk).exclude(state=STATE_RETIRED).first()
    if row is None:
        raise not_found()
    try:
        AcmeDelegation.rest_check_permission_or_raise(request, perms, row)
    except me.PermissionDeniedException:
        # Sequential ids must not classify a live foreign row versus an
        # unknown/retired row.
        raise not_found()
    return row


@md.POST("delegation/initiate")
@md.custom_security(
    "Explicit Domain/AcmeDelegation model checks bind every operation to its tenant")
def on_delegation_initiate(request):
    domain_pk = request.DATA.get("domain", None)
    group_pk = request.DATA.get("group", None)
    name = request.DATA.get("name", None)

    if domain_pk:
        if group_pk or name:
            raise me.ValueException("Provide either domain, or group and name")
    elif not group_pk or not name:
        raise me.ValueException("Provide either domain, or group and name")

    if domain_pk:
        domain = Domain.get_instance_or_404(domain_pk)
        Domain.rest_check_permission_or_raise(
            request, ["SAVE_PERMS", "VIEW_PERMS"], domain)
        if domain.group_id is None:
            raise me.ValueException("Delegated ACME requires a tenant domain")
        group = domain.group
    else:
        # The dispatcher resolves client-supplied group ids through
        # Group.get_active.  None therefore uniformly means inactive/missing.
        group = request.group
        if group is None or str(group.pk) != str(group_pk):
            raise me.ValueException("An active group is required")
        Domain.rest_check_permission_or_raise(
            request, ["SAVE_PERMS", "VIEW_PERMS"])
        domain = None

    row = delegation.initiate(
        group, request.user, domain=domain, name=name)
    return _payload(row)


@md.GET("delegation")
@md.requires_params("domain")
@md.custom_security(
    "Domain permission is checked before tenant-scoped delegation status is returned")
def on_delegation_list(request):
    domain = Domain.get_instance_or_404(request.DATA.get("domain"))
    Domain.rest_check_permission_or_raise(request, "VIEW_PERMS", domain)
    rows = AcmeDelegation.objects.filter(domain=domain).exclude(
        state=STATE_RETIRED).order_by("-created")[:100]
    return [_payload(row) for row in rows]


@md.GET("delegation/<int:pk>")
@md.custom_security(
    "The resolved AcmeDelegation is checked with tenant-scoped VIEW permissions")
def on_delegation_detail(request, pk=None):
    return _payload(_row(request, pk))


@md.POST("delegation/verify")
@md.requires_params("delegation")
@md.custom_security(
    "The resolved AcmeDelegation is checked with SAVE and VIEW permissions")
def on_delegation_verify(request):
    row = _row(
        request, request.DATA.get("delegation"),
        ["SAVE_PERMS", "VIEW_PERMS"])
    return _payload(delegation.verify(row))
