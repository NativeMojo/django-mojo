"""
Mojo provider for outbound SMS — delegates to another django-mojo instance
over HTTP using an account.ApiKey for authentication.

Mirrors mojo/helpers/geoip/mojo.py: same `Authorization: apikey <token>` scheme,
bounded `requests` timeout, `logit` for failures, and a normalized return
shape that matches mojo.apps.phonehub.services.twilio.send_sms so the
SMS.send() dispatcher can stay uniform across providers.
"""
import requests
from objict import objict

from mojo.helpers import logit
from mojo.helpers.settings import settings


PROVIDER = "mojo"


def send_sms(body, to_number, from_number=None, base_url=None, api_key=None, timeout=None):
    """
    POST to a remote django-mojo instance's /api/phonehub/sms/send endpoint
    and translate its response into the twilio-style objict the dispatcher
    expects.

    Returns objict({
        sent:     bool,
        id:       str or None,   # remote SMS id (or provider_message_id) on success
        status:   str or None,   # remote SMS status if echoed
        code:     str or None,   # error code on failure
        error:    str or None,   # error message on failure
        remote:   dict or None,  # full remote payload on success (for metadata['remote'])
        from_number: str or None # resolved sender echoed by the remote, if any
    })
    """
    if not base_url or not api_key:
        return objict({
            'sent': False, 'id': None, 'status': None,
            'code': 'config_error',
            'error': 'mojo provider requires base_url and api_key',
            'remote': None, 'from_number': None,
        })

    if timeout is None:
        timeout = settings.get_static('SMS_REMOTE_TIMEOUT', 10)

    url = f"{base_url.rstrip('/')}/api/phonehub/sms/send"
    headers = {"Authorization": f"apikey {api_key}"}
    payload = {"to_number": to_number, "body": body}
    if from_number:
        payload["from_number"] = from_number

    try:
        response = requests.post(
            url, json=payload, headers=headers,
            timeout=timeout, allow_redirects=False,
        )
    except requests.Timeout:
        logit.warning("[phonehub] mojo provider send timed out after %ss to %s", timeout, url)
        return objict({
            'sent': False, 'id': None, 'status': None,
            'code': 'timeout',
            'error': f'Remote SMS provider timed out after {timeout}s',
            'remote': None, 'from_number': None,
        })
    except Exception as e:
        logit.warning("[phonehub] mojo provider send failed: %s", e)
        return objict({
            'sent': False, 'id': None, 'status': None,
            'code': 'remote_error',
            'error': str(e),
            'remote': None, 'from_number': None,
        })

    if response.status_code >= 400:
        # Log the raw body for operators, but DO NOT echo it into
        # SMS.error_message — the remote could leak internal hostnames,
        # tracebacks, or other content that ends up readable by anyone
        # with view_sms permission. Surface only structured JSON error
        # fields (if any), else a generic HTTP <status> string.
        try:
            raw_body = response.text[:500]
        except Exception:
            raw_body = ''
        logit.warning(
            "[phonehub] mojo provider HTTP %s from %s: %s",
            response.status_code, url, raw_body
        )
        safe_error = f'HTTP {response.status_code}'
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                # Prefer 'error' or 'message' keys if present; cap length.
                msg = parsed.get('error') or parsed.get('message')
                if isinstance(msg, str) and msg:
                    safe_error = f'HTTP {response.status_code}: {msg[:200]}'
        except Exception:
            pass
        return objict({
            'sent': False, 'id': None, 'status': None,
            'code': f'http_{response.status_code}',
            'error': safe_error,
            'remote': None, 'from_number': None,
        })

    try:
        body_json = response.json()
    except Exception as e:
        logit.warning("[phonehub] mojo provider returned non-JSON body: %s", e)
        return objict({
            'sent': False, 'id': None, 'status': None,
            'code': 'remote_error',
            'error': f'Non-JSON response from remote: {e}',
            'remote': None, 'from_number': None,
        })

    if not isinstance(body_json, dict) or not body_json.get('status'):
        err = ''
        if isinstance(body_json, dict):
            err = str(body_json.get('error') or body_json.get('message') or body_json)[:500]
        return objict({
            'sent': False, 'id': None, 'status': None,
            'code': 'remote_failed',
            'error': err or 'Remote rejected the send',
            'remote': body_json if isinstance(body_json, dict) else None,
            'from_number': None,
        })

    data = body_json.get('data') or {}
    if not isinstance(data, dict):
        data = {}

    remote_id = data.get('provider_message_id') or data.get('id')
    remote_status = data.get('status')

    # The outer `status: true` only means the HTTP request was accepted —
    # the remote still creates an SMS row and may mark it failed (e.g. its
    # own provider rejected the number). Treat a failed/undelivered SMS as
    # a send failure so the local row is not misrecorded as sent.
    if remote_status in ('failed', 'undelivered'):
        return objict({
            'sent': False,
            'id': str(remote_id) if remote_id is not None else None,
            'status': remote_status,
            'code': 'remote_failed',
            'error': data.get('error_message') or 'Remote marked the SMS failed',
            'remote': data,
            'from_number': data.get('from_number'),
        })

    return objict({
        'sent': True,
        'id': str(remote_id) if remote_id is not None else None,
        'status': remote_status,
        'code': None,
        'error': None,
        'remote': data,
        'from_number': data.get('from_number'),
    })


def verify_credentials(base_url, api_key, timeout=None):
    """Validate the api key against a remote django-mojo without sending SMS.

    GETs the remote's `/api/group/apikey/me` whoami endpoint and reads back
    the key's granted permissions. Used by PhoneConfig.test_connection() so
    operators can check a mojo provider config with zero side effects (no
    SMS row created on the remote).

    Returns objict({
        ok:          bool,        # HTTP reachable + 2xx + status:true
        permissions: dict,        # the key's granted permissions ({} on failure)
        code:        str or None, # error code on failure
        error:       str or None, # error message on failure
    })
    """
    if not base_url or not api_key:
        return objict({
            'ok': False, 'permissions': {},
            'code': 'config_error',
            'error': 'mojo provider requires base_url and api_key',
        })

    if timeout is None:
        timeout = settings.get_static('SMS_REMOTE_TIMEOUT', 10)

    url = f"{base_url.rstrip('/')}/api/group/apikey/me"
    headers = {"Authorization": f"apikey {api_key}"}

    try:
        response = requests.get(
            url, headers=headers, timeout=timeout, allow_redirects=False,
        )
    except requests.Timeout:
        logit.warning("[phonehub] mojo provider verify timed out after %ss to %s", timeout, url)
        return objict({
            'ok': False, 'permissions': {},
            'code': 'timeout',
            'error': f'Remote timed out after {timeout}s',
        })
    except Exception as e:
        logit.warning("[phonehub] mojo provider verify failed: %s", e)
        return objict({
            'ok': False, 'permissions': {},
            'code': 'remote_error',
            'error': str(e),
        })

    if response.status_code >= 400:
        logit.warning(
            "[phonehub] mojo provider verify HTTP %s from %s",
            response.status_code, url,
        )
        return objict({
            'ok': False, 'permissions': {},
            'code': f'http_{response.status_code}',
            'error': f'HTTP {response.status_code}',
        })

    try:
        body = response.json()
    except Exception as e:
        logit.warning("[phonehub] mojo provider verify returned non-JSON body: %s", e)
        return objict({
            'ok': False, 'permissions': {},
            'code': 'remote_error',
            'error': f'Non-JSON response from remote: {e}',
        })

    if not isinstance(body, dict) or not body.get('status'):
        return objict({
            'ok': False, 'permissions': {},
            'code': 'remote_failed',
            'error': 'Remote rejected the request',
        })

    data = body.get('data') or {}
    perms = data.get('permissions') if isinstance(data, dict) else None
    if not isinstance(perms, dict):
        perms = {}

    return objict({
        'ok': True,
        'permissions': perms,
        'code': None,
        'error': None,
    })
