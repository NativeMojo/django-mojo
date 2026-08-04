"""
Permanent account closure, and delegation of it to the product.

The framework owns the closure entry point (`POST account/deactivate/confirm`)
but cannot know everywhere a product stores personal data. `ACCOUNT_CLOSURE_HANDLER`
hands the whole closure to a product callable, which purges what it owns while the
User identity and its GroupMember rows are still intact, and calls
`user.pii_anonymize()` as its final act.

Unset, the framework anonymizes directly — the behavior that predates delegation.

Handler contract:

- Configured in the settings FILE only, never a DB-backed ``Setting`` row — it
  chooses which code the worker imports and runs.
- Called as ``handler(user)`` with the account still active and memberships intact.
- Owns the final ``user.pii_anonymize()``. This module never calls it for you.
  Returning without closing the account is treated as a failed closure, not a
  success — otherwise a no-op handler would report erasure that never happened.
- Must not deactivate the account or revoke its credentials before that final
  anonymize. A partial purge that breaks re-authentication strands the closure:
  the token is already spent and an inactive account cannot re-initiate.
- Must be idempotent. The deactivate token is consumed at verification, so a
  failed run is recovered by re-initiating deactivation, and the first run may
  already have purged part of what it owns.
- Fails closed. Raising aborts the closure and leaves the account active; the
  handler's own exception message is never surfaced to the caller, since this
  endpoint is public and a closure failure can carry the data it was purging.

This is permanent erasure. Reversible disable/enable is a different mechanism —
see `mojo/apps/account/services/disable.py`.
"""
import importlib

from django.core.exceptions import ObjectDoesNotExist

from mojo import errors as merrors
from mojo.helpers import logit
from mojo.helpers.settings import settings


# Every closure failure answers with this, whatever went wrong. Naming the cause
# would leak product internals through a public, token-authenticated endpoint.
CLOSURE_FAILED_MESSAGE = (
    "Account closure could not be completed. Please restart deactivation.")


def run_account_closure(user):
    """
    Permanently close `user`.

    Delegates to `ACCOUNT_CLOSURE_HANDLER` when the deployment configures one,
    otherwise anonymizes directly. Raises ValueException when a configured
    handler cannot be resolved or fails, leaving the account active so the user
    can re-initiate deactivation.
    """
    # get_static, NOT get: this key selects which code the worker imports and
    # calls. settings.get resolves the DB/Redis Setting store first, so reading
    # it that way would let anyone holding manage_settings turn a config write
    # into arbitrary code execution. Deployment plumbing belongs in the settings
    # file. Same reasoning as AUTH_PHONE_VERIFY_DEV_BYPASS_CODE (item 50).
    handler_path = settings.get_static("ACCOUNT_CLOSURE_HANDLER", None)
    if not handler_path:
        user.pii_anonymize()
        return

    # import_module runs the target module's top-level code, which can raise
    # anything at all — a narrower except would let those escape as a 500 with
    # no closure incident recorded.
    try:
        module_path, func_name = handler_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        handler = getattr(module, func_name)
    except Exception as err:
        _report_failure(user, handler_path, "unresolved", err)
        raise merrors.ValueException(CLOSURE_FAILED_MESSAGE) from None

    try:
        handler(user)
    except Exception as err:
        _report_failure(user, handler_path, "failed", err)
        raise merrors.ValueException(CLOSURE_FAILED_MESSAGE) from None

    if not _closure_landed(user):
        _report_failure(user, handler_path, "incomplete")
        raise merrors.ValueException(CLOSURE_FAILED_MESSAGE) from None


def _closure_landed(user):
    """Did the handler actually close the account?

    A handler that quietly returns without anonymizing would otherwise earn a
    success response for an untouched account — telling someone their erasure
    completed when nothing happened, with no incident and a spent token. The
    framework still does not do the anonymizing; it insists that it happened.

    A handler that deleted the row outright counts as closed.
    """
    try:
        user.refresh_from_db()
    except ObjectDoesNotExist:
        return True
    return not user.is_active


def _report_failure(user, handler_path, outcome, err=None):
    # The incident carries the dotted path and the outcome only. The exception
    # text is deliberately kept out of it: a handler that dies mid-purge can put
    # the very PII it was deleting into its own error message, and incidents are
    # readable well outside the closure flow. `raise ... from None` at the call
    # sites is part of the same guarantee — without it the dispatcher files a
    # second incident carrying traceback.format_exc(), chained context and all.
    # The traceback still reaches the server log, which is the operator's domain.
    message = f"account closure handler {outcome}: {handler_path}"
    if err is None:
        logit.error(message)
    else:
        logit.exception(message, err)
    user.report_incident(
        message,
        "account:closure_failed",
        level=8,
        uid=user.pk)
