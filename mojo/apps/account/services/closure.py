"""
Permanent account closure, and delegation of it to the product.

The framework owns the closure entry point (`POST account/deactivate/confirm`)
but cannot know everywhere a product stores personal data. `ACCOUNT_CLOSURE_HANDLER`
hands the whole closure to a product callable, which purges what it owns while the
User identity and its GroupMember rows are still intact, and calls
`user.pii_anonymize()` as its final act.

Unset, the framework anonymizes directly — the behavior that predates delegation.

Handler contract:

- Called as ``handler(user)`` with the account still active and memberships intact.
- Owns the final ``user.pii_anonymize()``. This module never calls it after a
  handler runs, so a handler that skips it leaves the account un-anonymized.
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
    handler_path = settings.get("ACCOUNT_CLOSURE_HANDLER", None)
    if not handler_path:
        user.pii_anonymize()
        return

    try:
        module_path, func_name = handler_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        handler = getattr(module, func_name)
    except (ImportError, AttributeError, ValueError) as err:
        _report_failure(user, handler_path, "unresolved", err)
        raise merrors.ValueException(CLOSURE_FAILED_MESSAGE)

    try:
        handler(user)
    except Exception as err:
        _report_failure(user, handler_path, "failed", err)
        raise merrors.ValueException(CLOSURE_FAILED_MESSAGE)


def _report_failure(user, handler_path, outcome, err):
    # The incident carries the dotted path and the outcome only. The exception
    # text is deliberately kept out of it: a handler that dies mid-purge can put
    # the very PII it was deleting into its own error message, and incidents are
    # readable well outside the closure flow. The traceback still goes to the
    # server log, which is the operator's own domain.
    logit.exception(f"account closure handler {outcome}: {handler_path}", err)
    user.report_incident(
        f"account closure handler {outcome}: {handler_path}",
        "account:closure_failed",
        level=8,
        uid=user.pk)
