"""
Truthful reporting for transactional email sends.

`User.send_template_email()` is deliberately forgiving — it swallows failures
so a send can never 500 an endpoint. That leaves the caller unable to tell a
delivered message from a dropped one unless it inspects the result, and there
are three distinct ways a send can fail while looking fine:

1. No mailbox is configured for the user's org or as the system default —
   `send_template_email` files an `email:no_mailbox` incident and returns
   `None` (mojo/apps/account/models/user.py, the `if not mailbox:` branch).
2. The provider call raised — it files an `email:send_failed` incident and
   returns `None` (same file, the `except Exception` around
   `mailbox.send_template_email`).
3. **The provider refused the message.** `mojo/apps/aws/services/email.py`
   `send_with_template` *persists and returns* a `SentMessage` with
   `status="failed"`, no `ses_message_id`, and the provider's error text in
   `status_reason`. A truthy result is therefore NOT proof of a send, and a
   bare `if not sent:` check reports a refusal as success.

`was_accepted(sent)` is the one predicate that answers "did the provider take
custody of this message?", and `send_unavailable_response()` is the one answer
an endpoint gives when it did not.

**Acceptance is provider custody, not inbox delivery.** A True result means SES
accepted the message and returned a message id. The message can still bounce or
be rejected by the recipient's mail server afterwards; that arrives later over
SNS and updates the `SentMessage` row. "We sent it" is the strongest honest
claim an endpoint can make at request time.

**Why the strict form.** Every send path in the repo today produces either an
accepted state (`sending`, set explicitly when the row is created) plus a
message id, or `failed` with no id — so this predicate and the looser
"status != failed" form are extensionally identical at this checkout. The
strict form is kept because it fails closed on shapes that do not exist yet: a
hypothetical `queued`-without-id row is reported as retryable rather than
falsely "sent". A false "couldn't send" costs a duplicate email; a false "sent"
costs a person waiting forever for a message that will never arrive.

**Only for sends that pass no `kind=`.** A `kind`-tagged send returns `None`
when the user's notification preferences suppress it — a deliberate,
successful outcome that must never be reported as a delivery failure. System
and transactional email (verification, password reset, email change) does not
pass `kind`, which is exactly the traffic this module is for.
"""
from django.http import JsonResponse

from mojo.helpers.settings import settings


# The one message a caller may show when a send was not accepted. Provider
# error text (`SentMessage.status_reason`) must never reach a client — it can
# name internal addresses, mailbox configuration and SES account state. The
# "few minutes" matches the 300s rate-limit window on the send endpoints.
EMAIL_SEND_UNAVAILABLE = (
    "Unable to send the email right now. Please try again in a few minutes.")

# SentMessage states that mean the provider took custody. `queued`, `bounced`,
# `complained`, `failed` and `unknown` all mean it did not (or no longer has).
ACCEPTED_STATES = ("sending", "delivered")


def was_accepted(sent):
    """
    True only when the email provider accepted the message.

    `sent` is whatever `User.send_template_email()` / `Mailbox.send_template_email()`
    returned: a `SentMessage`, or `None`. Duck-typed on purpose — reading the
    attributes with `getattr` keeps `account` free of an import-time dependency
    on `mojo.apps.aws`, and when the aws app is absent the send already
    returned `None` anyway.

    Never raises: a caller is asking this question on a path that must not 500.
    """
    try:
        if sent is None:
            return False
        if getattr(sent, "status", None) not in ACCEPTED_STATES:
            return False
        return bool(getattr(sent, "ses_message_id", None))
    except Exception:
        return False


def send_unavailable_response():
    """
    The response an endpoint returns when the send was not accepted.

    HTTP 503 with the fixed body `{"status": false, "code": 503, "error": ...}`
    — safe, retryable, and carrying no provider detail.

    Returned directly rather than raised. Raising a MojoException would make
    `dispatch_error_handler` file a second, raw `mojo_rest_error` event
    carrying the request body and a stack trace, whose group is auto-stamped
    from `request.group`; the dispatcher returns any `HttpResponse` from a view
    untouched, so a plain response gets the wire contract with none of that.

    `MOJO_APP_STATUS_200_ON_ERROR` is honored the way the dispatcher honors it
    — the wire status folds to 200 while the body keeps `"code": 503` — so
    deployments running the legacy API shim see no change in shape. It is read
    statically (conf file / env) so a DB-backed Setting row cannot change how a
    failure path answers.
    """
    status = 200 if settings.get_static("MOJO_APP_STATUS_200_ON_ERROR", False) else 503
    return JsonResponse(
        {"status": False, "code": 503, "error": EMAIL_SEND_UNAVAILABLE},
        status=status)
