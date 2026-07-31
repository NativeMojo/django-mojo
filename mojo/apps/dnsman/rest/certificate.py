import mojo.decorators as md
import mojo.errors as me
from mojo.helpers import logit
from mojo.apps.dnsman.models import Certificate, Domain
from mojo.apps.dnsman.rest.gates import require_platform_admin
from mojo.apps.dnsman.services import certs


def _guard_house_certificate(request, certificate, what):
    """
    A certificate on a group-less (house) domain is platform property.

    Certificate scopes through RestMeta.GROUP_FIELD = "domain__group". For a
    house domain that resolution yields None, and the permission check falls
    through to the caller's GLOBAL permissions — so any holder of a global
    manage_dns grant passes a check that was never about their group at all.
    This guard is LOAD-BEARING, not redundant with the framework check.

    Before maestro item 953 it was load-bearing for a second, worse reason: a
    null tenant left request.group at whatever `?group=` the caller supplied, so
    a tenant-level manage_dns grant passed too. The framework now binds the
    row's tenant unconditionally (mojo/models/rest.py) and that hole is closed —
    but the global-permission fall-through above remains, which is why this
    still runs.

    The LIST path is unaffected (`domain__group=<group>` cannot match a null),
    so this guards the per-instance paths, where it is the only thing standing
    between a global manage_dns holder and a platform certificate.
    """
    if certificate.domain.group_id is None:
        require_platform_admin(request, what)


@md.URL('certificate')
@md.URL('certificate/<int:pk>')
@md.uses_model_security(Certificate)
def on_certificate(request, pk=None):
    """Status and renewal state. The graphs carry no PEM and no key — material
    comes only from the material endpoint below."""
    if pk is not None:
        # The default graph carries the owning domain (name, provider, status,
        # expires) and certificate pks are sequential, so an ungated detail
        # fetch is an enumeration oracle over the house inventory that
        # registrar/discover keeps superuser-only.
        #
        # The model check runs FIRST — the opposite of the registrar endpoints,
        # and deliberately. Here it is a real gate (instance-scoped, and it
        # correctly refuses anonymous and foreign-tenant callers), so putting
        # the house guard ahead of it would let an unauthenticated caller tell a
        # house certificate (403 "platform administrators") from a tenant one
        # (401) — a free classification oracle over sequential pks. Same
        # ordering as certificate/material below.
        certificate = Certificate.get_instance_or_404(pk)
        Certificate.rest_check_permission_or_raise(
            request, ["VIEW_PERMS"], certificate)
        _guard_house_certificate(request, certificate, "House certificates")
    return Certificate.on_rest_request(request, pk)


@md.POST('certificate/request')
@md.requires_params("domain")
def on_certificate_request(request):
    """Queue issuance for a domain. Issuance itself runs as a job — it takes
    minutes and must never occupy a request."""
    domain = Domain.get_instance_or_404(request.DATA.get("domain"))
    Domain.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"], domain)

    names = request.DATA.get("names", None)
    if names is not None and not isinstance(names, list):
        names = [names]

    certificate = certs.request_certificate(domain, names=names)
    return certificate.on_rest_get(request)


@md.POST('certificate/revoke')
@md.requires_params("certificate")
def on_certificate_revoke(request):
    """Revocation is irreversible and reaches the CA — the house guard below is
    the destructive twin of the read guard on the detail route."""
    certificate = Certificate.get_instance_or_404(request.DATA.get("certificate"))
    # Model check first, then the house guard — same ordering and same reason as
    # the detail route above: leading with the guard would classify house vs.
    # tenant certificates for a caller who cannot read either.
    Certificate.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"], certificate)
    _guard_house_certificate(request, certificate, "Revoking a house certificate")
    certs.revoke(certificate)
    return certificate.on_rest_get(request)


# Dynamic segment last: mid-path pk segments are forbidden by the repo's REST
# conventions (see .claude/rules/rest.md).
@md.GET('certificate/material/<int:pk>')
def on_certificate_material(request, pk=None):
    """
    The ONLY path by which a private key leaves this database.

    Gated on SAVE_PERMS rather than VIEW_PERMS on purpose: being allowed to see
    that a certificate exists is not the same as being allowed to hold its key.
    A serving host calls this with its own API key after hearing the
    cert-updated broadcast, which itself carries no material.
    """
    certificate = Certificate.get_instance_or_404(pk)
    Certificate.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"], certificate)

    # Group scoping resolves through GROUP_FIELD = "domain__group". When the
    # owning domain has NO group (a house/platform domain) that resolution
    # yields None, and the permission check falls through to the caller's
    # GLOBAL permissions — so any holder of a global manage_dns grant passes.
    # For a house certificate that means handing over a private key, so require
    # a platform admin explicitly. This guard is LOAD-BEARING, not redundant
    # with the framework check.
    #
    # (Before maestro item 953 it was load-bearing for a second, worse reason:
    # a null tenant left request.group at whatever ?group= the caller supplied,
    # so a tenant-level manage_dns grant passed too. The framework now binds
    # the row's tenant unconditionally and that hole is closed.)
    #
    # Shared with the detail and revoke routes so the rule lives in one place.
    _guard_house_certificate(request, certificate, "House certificate material")

    if certificate.status != "active":
        raise me.ValueException(f"Certificate is {certificate.status}, not active")

    private_key_pem = certificate.private_key_pem
    if not private_key_pem or not certificate.cert_pem:
        # KSMSecrets returns an empty mapping when KMS decryption fails, so an
        # empty key on an active certificate means the custody layer is
        # unavailable — not that the certificate has no key. Reporting this as
        # "no key" would send a consumer off to reissue for no reason.
        logit.error(f"dnsman: certificate {certificate.pk} material unavailable (KMS?)")
        raise me.ValueException("Certificate material temporarily unavailable", code=503)

    logit.info(
        f"dnsman: certificate material released for {certificate.common_name} "
        f"(cert={certificate.pk}, user={getattr(request.user, 'pk', None)}, ip={request.ip})")

    return dict(
        id=certificate.pk,
        common_name=certificate.common_name,
        sans=certificate.sans,
        not_after=certificate.not_after,
        cert_pem=certificate.cert_pem,
        chain_pem=certificate.chain_pem,
        private_key_pem=private_key_pem,
    )
