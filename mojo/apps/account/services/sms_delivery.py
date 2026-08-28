"""
Truthful reporting for phone-verification SMS sends.

`phonehub.send_sms()` is deliberately forgiving — `SMS.send()` never raises
for a refused message. It *persists and returns* an `SMS` row with
`status="failed"`, the provider's code in `error_code` and its text in
`error_message`, for every failure class alike: missing/half-configured
credentials (`config_error`), a Twilio refusal, or a mojo remote-provider
error. A truthy result is therefore NOT proof of a send, and the old
`if sms and sms.status == "failed"` guard answered every one of those — an
outage as much as a bad number — with a misleading
"check your phone number" 400 that carried no retry hint and blamed the
person for the deployment's configuration.

`was_accepted(sms)` is the one predicate that answers "did the SMS transport
take custody of this message?"; `recipient_rejected(sms)` isolates the one
sanctioned failure class a caller may honestly blame on the number itself;
`send_unavailable_response()` / `number_unreachable_response()` are the fixed
answers an endpoint gives for each.

**Acceptance is transport custody, not handset delivery.** A True result
means the provider (or the test-mode short-circuit) took the message. It can
still fail on the carrier side afterwards; that arrives later over the status
webhook and updates the `SMS` row. "We sent it" is the strongest honest claim
an endpoint can make at request time.

**Deliberate deviation from email_delivery: no provider_message_id
requirement.** The mojo remote provider can report an accepted send with
`resp.sent == True` and `resp.id == None`, and `SMS.mark_sent()` stores an id
only when one came back — so requiring an id here would misreport an accepted
send as a failure. `status` alone is the custody signal for SMS.

**Recipient rejections are an allowlist, and unknown codes fail retryable.**
Only the Twilio codes that indict the 'To' number itself count (see
`RECIPIENT_REJECTED_CODES`). Every other or absent code — config errors,
timeouts, codes this module has never seen — answers with the retryable 503:
a false "this number cannot receive texts" strands a person on a working
number, while a false "try again" merely costs a retry.
"""
from django.http import JsonResponse

from mojo.helpers.settings import settings


# The only messages a caller may show. Provider error text
# (`SMS.error_message`) must never reach a client — it can name credential
# state, PhoneConfig internals and remote-provider URLs. The "few minutes"
# matches the 300s rate-limit window on the send endpoint.
SMS_SEND_UNAVAILABLE = (
    "Unable to send the text message right now. Please try again in a few minutes.")
SMS_NUMBER_UNREACHABLE = "This phone number cannot receive text messages."

# SMS states that mean the transport took custody. `queued`, `sending`,
# `failed`, `undelivered`, `received` and anything unrecognised all mean it
# did not — `SMS.send()` always returns a terminal `sent` or `failed` row for
# an outbound send, so a non-terminal state here is a shape that does not
# exist yet and is reported as retryable rather than falsely "sent".
ACCEPTED_STATES = ("sent", "delivered")

# Twilio error codes that indict the recipient number itself: 21211 (the 'To'
# number is invalid), 21610 (the recipient has blocked/opted out of messages
# from this sender), 21614 (the 'To' number is not SMS-capable, e.g. a
# landline). Compared as strings — `SMS.error_code` is a CharField, but a
# provider adapter may hand back an int.
RECIPIENT_REJECTED_CODES = ("21211", "21610", "21614")


def was_accepted(sms):
    """
    True only when the SMS transport accepted the message.

    `sms` is whatever `phonehub.send_sms()` returned: an `SMS` row, or `None`
    when the caller caught a transport exception. Duck-typed on purpose —
    reading the attribute with `getattr` keeps `account` free of an
    import-time dependency on `mojo.apps.phonehub`.

    Never raises: a caller is asking this question on a path that must not 500.
    """
    try:
        if sms is None:
            return False
        return getattr(sms, "status", None) in ACCEPTED_STATES
    except Exception:
        return False


def recipient_rejected(sms):
    """
    True only for a failed send whose provider error code indicts the
    recipient number itself — the one failure class the caller may honestly
    answer with "this number cannot receive text messages".

    Everything else — unknown codes, absent codes, non-failed rows, `None` —
    is False, failing toward the retryable 503.

    Never raises, for the same reason as `was_accepted`.
    """
    try:
        if sms is None:
            return False
        if getattr(sms, "status", None) != "failed":
            return False
        code = getattr(sms, "error_code", None)
        if code is None:
            return False
        return str(code) in RECIPIENT_REJECTED_CODES
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
        {"status": False, "code": 503, "error": SMS_SEND_UNAVAILABLE},
        status=status)


def number_unreachable_response():
    """
    The response an endpoint returns for a sanctioned recipient rejection.

    HTTP 400 with the fixed body `{"status": false, "code": 400, "error": ...}`
    — the number itself is the problem, so a retry with the same number will
    not help. Same mechanics as `send_unavailable_response()`: returned
    directly, fixed copy only, and the wire status folds to 200 under the
    legacy `MOJO_APP_STATUS_200_ON_ERROR` shim while the body keeps
    `"code": 400`.
    """
    status = 200 if settings.get_static("MOJO_APP_STATUS_200_ON_ERROR", False) else 400
    return JsonResponse(
        {"status": False, "code": 400, "error": SMS_NUMBER_UNREACHABLE},
        status=status)
