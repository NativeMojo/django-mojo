"""
The two endpoints a serving node calls. Everything else in this app is for
humans.

**Both are gated with `requires_global_perms`, never `requires_perms`, and the
difference is the whole security story of this file.**

`requires_perms` falls back to `request.group.user_has_permission(...)` using a
**client-supplied `?group=` param** (`mojo/decorators/auth.py`,
`REQUIRES_PERMS_IS_GROUP` defaults True). And unlike the ApiKey floor, the
GroupMember side has no framework protection map — `_member_perms_protection()`
returns `{}` by default, so an unlisted perm falls through to "is this caller a
group admin?", which any tenant admin is inside their own group.

Under `requires_perms` that composes into a three-call escalation: grant
yourself `edge_node` as a member of your own group, call
`desired_state?group=<your group>`, receive every vhost in the pool, then call
`material` the same way and receive every referenced certificate's private key —
including every house certificate. `requires_global_perms` resolves on the
caller's GLOBAL `User.permissions` (or superuser) only and never consults the
member/group fallback. `allow_api_keys=True` because the intended caller IS a
machine holding an ApiKey, exactly like the geoip federation receiver.

Item #1435 extends `desired_state` with a `webapps` key. It does NOT add a
second endpoint: two convergence loops that can disagree about what a node
should serve is the failure this design exists to avoid.
"""

import mojo.decorators as md
from mojo import errors as me
from mojo.helpers import logit

from mojo.apps.dnsman.models import Certificate
from mojo.apps.dnsman.services import certs
from mojo.apps.edge.models import Vhost
from mojo.apps.edge.services import render


def enabled_vhosts(pool):
    """Every vhost a node in `pool` should be serving.

    `select_related` on the three FKs the payload reads — without it this is an
    N+1 across every vhost in the fleet, on an endpoint every node polls.
    """
    return list(
        Vhost.objects.filter(is_enabled=True, pool=pool)
        .select_related("domain", "certificate", "upstream")
        .prefetch_related("routes__upstream")
        .order_by("pk"))


@md.GET('desired_state')
@md.requires_global_perms('edge_node', allow_api_keys=True)
def on_desired_state(request):
    """What this pool's nodes should be serving, and the hash that identifies it.

    Carries non-secret certificate identity/revision metadata only — never
    material. A node that needs a key pulls it from `material` below, which is
    one gated, access-logged call per certificate rather than a fleet-wide key
    broadcast on every poll.
    """
    from mojo.apps.edge.services import releases

    pool = request.DATA.get("pool", None) or "default"
    vhosts = enabled_vhosts(pool)

    # The generation hash covers the webapps key too — a promote that did not
    # move the hash would never reach a node.
    payload = render.desired_state(vhosts, webapps=releases.desired_webapps(vhosts))
    payload["pool"] = pool
    payload["certificates"] = [
        dict(id=v.certificate_id,
             common_name=v.certificate.common_name,
             not_after=v.certificate.not_after)
        for v in {v.certificate_id: v for v in vhosts}.values()
    ]
    return payload


# Dynamic segment last: mid-path pk segments are forbidden by the repo's REST
# conventions (see .claude/rules/rest.md).
@md.GET('material/<int:pk>')
@md.requires_global_perms('edge_node', allow_api_keys=True)
def on_node_material(request, pk=None):
    """Certificate material for a node, scoped to what it must install.

    **Why this exists rather than reusing dnsman's material endpoint.**
    `GET /api/dnsman/certificate/material/<pk>` runs `_guard_house_certificate`
    -> `require_platform_admin`, and that gate refuses key-backed sessions
    outright (`mojo/apps/dnsman/rest/gates.py`). Every platform vhost sits on a
    house (group-less) domain, so a node's ApiKey is refused on exactly the
    certificates it exists to install. Weakening that gate was not an option —
    it is what keeps a global `manage_dns` grant away from platform keys.

    This endpoint is STRICTLY TIGHTER than the one it works around: it serves
    material only for a certificate an enabled `Vhost` actually references, so
    a node key can read what it must install and nothing else. A certificate
    that no vhost uses is a 404 here even though the caller is authorized.
    """
    certificate = Certificate.objects.filter(pk=pk).first()
    referenced = certificate is not None and Vhost.objects.filter(
        certificate_id=pk, is_enabled=True).exists()
    if not referenced:
        # One answer for "no such certificate" and "nothing serves it": a node
        # key must not be able to enumerate the certificate table.
        raise me.ValueException(
            "No enabled vhost references that certificate", code=404, status=404)

    if certificate.status != "active" and not (
            certificate.status == "issuing"
            and certs.has_still_valid_material(certificate)):
        raise me.ValueException(
            f"Certificate is {certificate.status}, not active")

    private_key_pem = certificate.private_key_pem
    if not private_key_pem or not certificate.cert_pem:
        # KSMSecrets returns an empty mapping when KMS decryption fails, so an
        # empty key on an active certificate means the custody layer is
        # unavailable — not that the certificate has no key. Reporting it as
        # "no key" would send the installer off to reissue for no reason.
        logit.error(
            f"edge: certificate {certificate.pk} material unavailable (KMS?)")
        raise me.ValueException(
            "Certificate material temporarily unavailable", code=503)

    logit.info(
        f"edge: node pulled material for {certificate.common_name} "
        f"(cert={certificate.pk}, key={getattr(request, 'api_key', None) and request.api_key.pk}, "
        f"ip={request.ip})")

    return dict(
        id=certificate.pk,
        common_name=certificate.common_name,
        sans=certificate.sans,
        not_after=certificate.not_after,
        cert_pem=certificate.cert_pem,
        chain_pem=certificate.chain_pem,
        private_key_pem=private_key_pem,
    )
