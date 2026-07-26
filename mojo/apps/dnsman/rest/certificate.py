import mojo.decorators as md
import mojo.errors as me
from mojo.helpers import logit
from mojo.apps.dnsman.models import Certificate, Domain
from mojo.apps.dnsman.services import certs


@md.URL('certificate')
@md.URL('certificate/<int:pk>')
@md.uses_model_security(Certificate)
def on_certificate(request, pk=None):
    """Status and renewal state. The graphs carry no PEM and no key — material
    comes only from the material endpoint below."""
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
    certificate = Certificate.get_instance_or_404(request.DATA.get("certificate"))
    Certificate.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"], certificate)
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
