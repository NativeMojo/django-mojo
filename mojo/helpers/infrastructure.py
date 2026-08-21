"""Whether this installation's infrastructure is the portal's to mutate.

Some installations own their AWS estate through the Admin portal; others have
it declared and applied by an external IaC pipeline (Terraform/OpenTofu,
CloudFormation, Pulumi, a configuration-management run). On the second kind,
a portal mutation is not merely unwanted — it is a change the next IaC apply
will revert or, worse, replace with something different. ``INFRASTRUCTURE_MODE``
is the one switch that says which kind this is, and the mutating endpoints
refuse when it says ``external``.

Two values, and nothing else is a value:

* ``managed`` — unset, empty, or the literal string. Every existing install.
* ``external`` — the portal answers 403 on the gated mutations.

**File-only** (``settings.get_static``). A DB/Redis-backed ``Setting`` row is
writable through the generic settings REST plane, so a remotely-writable mode
would let settings-write access silently re-arm every mutation this switch
exists to disable.

**Unrecognized values fail closed to ``external``**, with one logged error
naming the setting and the rejected value — same reasoning as
``AUTH_HANDOFF_GROUP_TOKEN_MODE``: a typo in a switch whose whole job is to
refuse must not quietly turn the refusal off. A settings read that raises is
treated the same way; a broken read is not a licence to mutate.

The contract other code depends on is small and stable: ``infrastructure_mode``
returns exactly ``"managed"`` or ``"external"`` and never raises, ``is_external``
is the boolean form, ``refusal_message`` is the sentence a human reads, and
``refuse`` is the ready-made 403 for a REST handler.
"""

from mojo.helpers import logit
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings


SETTING = "INFRASTRUCTURE_MODE"
MANAGED = "managed"
EXTERNAL = "external"
ERROR_CODE = "infrastructure_external"

DEFAULT_ACTION = "This change"


def infrastructure_mode(*, reader=None):
    """Return exactly ``MANAGED`` or ``EXTERNAL``. Never raises.

    Unset/empty/``"managed"`` is managed. ``"external"`` is external. Anything
    else present — a typo, a non-string, a settings backend that blew up — is
    external, and says so once in the error log.

    ``reader`` is a test seam for the file-only settings read; ``None`` means
    the real ``settings.get_static``.
    """
    if reader is None:
        reader = settings.get_static
    try:
        raw = reader(SETTING, "")
    except Exception as err:
        logit.error(
            f"{SETTING} could not be read ({err.__class__.__name__}) — "
            f"treating this installation as {EXTERNAL!r}")
        return EXTERNAL
    if raw is None:
        return MANAGED
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in ("", MANAGED):
            return MANAGED
        if value == EXTERNAL:
            return EXTERNAL
    logit.error(
        f"{SETTING} {raw!r} is not one of {(MANAGED, EXTERNAL)} — "
        f"treating this installation as {EXTERNAL!r}")
    return EXTERNAL


def is_external():
    """True when this installation's infrastructure is not the portal's."""
    return infrastructure_mode() == EXTERNAL


def refusal_message(action=""):
    """The one sentence a refused caller reads: mode, setting, and whose job it is."""
    label = str(action or "").strip() or DEFAULT_ACTION
    return (f"{label} is disabled: {SETTING} is {EXTERNAL}, so AWS "
            f"infrastructure is managed by your infrastructure team, not this "
            f"portal.")


def refuse(action=""):
    """``None`` when managed; the ready-made 403 response when external.

    Callers make this the FIRST statement in the handler body so the answer is
    about the installation, not about the caller's grants.
    """
    if not is_external():
        return None
    return JsonResponse({
        "status": False,
        "error": refusal_message(action),
        "error_code": ERROR_CODE,
        "data": {"mode": EXTERNAL, "setting": SETTING},
    }, status=403)
