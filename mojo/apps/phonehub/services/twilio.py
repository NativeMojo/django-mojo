from mojo.helpers.settings import settings
from objict import objict

PROVIDER = "twilio"


def get_from_number():
    return settings.get('TWILIO_NUMBER')


def resolve_credentials(config, from_number=None):
    """Resolve the (from_number, account_sid, auth_token) triple for a send.

    Anchored on the credential pair (maestro #2189, D2) — the mix that breaks
    is a foreign number sent with the default account's credentials (Twilio
    21606), so the pair decides who owns the whole triple:

    * config supplies BOTH credentials -> the config owns the triple; the
      number must also come from the config (or the caller's explicit
      from_number), else config_error.
    * config supplies NEITHER credential -> settings own the credentials
      (returned as None so send_sms fills them); the number falls back
      caller -> config.twilio_from_number -> settings.TWILIO_NUMBER.
    * config supplies EXACTLY ONE credential -> config_error. Never mixed.

    Returns an objict with from_number/account_sid/auth_token and
    error/error_code (both None on success).
    """
    account_sid = config.get_twilio_account_sid() if config else ''
    auth_token = config.get_twilio_auth_token() if config else ''
    if bool(account_sid) != bool(auth_token):
        return objict(
            from_number=None, account_sid=None, auth_token=None,
            error_code='config_error',
            error='Twilio config supplies only half a credential pair — set '
                  'both twilio_account_sid and twilio_auth_token, or neither')
    if account_sid:
        number = from_number or config.twilio_from_number
        if not number or not isinstance(number, str):
            return objict(
                from_number=None, account_sid=None, auth_token=None,
                error_code='config_error',
                error='Twilio config supplies credentials but no sender — '
                      'set PhoneConfig.twilio_from_number')
        return objict(from_number=number, account_sid=account_sid,
                      auth_token=auth_token, error=None, error_code=None)
    number = from_number or (config.twilio_from_number if config else None) \
        or get_from_number()
    if not number or not isinstance(number, str):
        return objict(
            from_number=None, account_sid=None, auth_token=None,
            error_code='config_error',
            error='No from_number configured (set '
                  'PhoneConfig.twilio_from_number or TWILIO_NUMBER in settings)')
    return objict(from_number=number, account_sid=None, auth_token=None,
                  error=None, error_code=None)


def lookup(phone_number):
    try:
        resp = _lookup(phone_number, settings.get('TWILIO_ACCOUNT_SID'), settings.get('TWILIO_AUTH_TOKEN'))
    except Exception as e:
        resp = objict(error=str(e))
    return resp


def send_sms(body, to_number, from_number=None, account_sid=None, auth_token=None):
    if account_sid is None:
        account_sid = settings.get('TWILIO_ACCOUNT_SID')
    if auth_token is None:
        auth_token = settings.get('TWILIO_AUTH_TOKEN')
    return _send_sms(body, to_number, from_number, account_sid, auth_token)


def validate_webhook_signature(request):
    """
    Validate a Twilio webhook request signature.
    Returns True if valid, False otherwise.
    See: https://www.twilio.com/docs/usage/webhooks/webhooks-security
    """
    auth_token = settings.get('TWILIO_AUTH_TOKEN')
    if not auth_token:
        return False
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(auth_token)
        url = request.build_absolute_uri()
        signature = request.META.get('HTTP_X_TWILIO_SIGNATURE', '')
        params = dict(request.POST)
        flat_params = {k: v[0] if isinstance(v, list) else v for k, v in params.items()}
        return validator.validate(url, flat_params, signature)
    except Exception:
        return False


def _lookup(phone_number, account_sid, auth_token):
    """
    Lookup phone using Twilio with caller name information.

    Uses Twilio Lookup v2 API with:
    - line_type_intelligence: Carrier, line type (mobile/voip)
    - caller_name: Registered owner/caller name (CNAM)
    """
    from twilio.rest import Client
    client = Client(account_sid, auth_token)

    # Lookup phone number with line_type_intelligence and caller_name
    # Note: caller_name is an add-on and may incur additional charges
    lookup = client.lookups.v2.phone_numbers(phone_number).fetch(
        fields='line_type_intelligence,caller_name'
    )
    carrier = None
    line_type = None
    is_mobile = False
    is_voip = False
    if hasattr(lookup, 'line_type_intelligence') and lookup.line_type_intelligence:
        line_type_data = lookup.line_type_intelligence
        carrier = line_type_data.get('carrier_name')
        line_type = line_type_data.get('type', '').lower()
        is_mobile = line_type in ['mobile', 'wireless']
        is_voip = line_type == 'voip'
        caller_name = None
        caller_type = None

    if hasattr(lookup, 'caller_name') and lookup.caller_name:
        caller_data = lookup.caller_name
        caller_name = caller_data.get('caller_name')
        caller_type = caller_data.get('caller_type')  # BUSINESS or CONSUMER

    return objict({
        'country_code': lookup.country_code,
        'carrier': carrier,
        'line_type': line_type,
        'is_mobile': is_mobile,
        'is_voip': is_voip,
        'is_valid': True,
        'caller_name': caller_name,
        'caller_type': caller_type,
        'lookup_provider': 'twilio'
    })


def _send_sms(body, to_number, from_number, account_sid, auth_token):
    """
    Send SMS via Twilio.

    Returns:
        dict: {
            'sent': bool,
            'id': str or None,
            'status': str or None,
            'code': int or None,
            'error': str or None
        }
    """
    from twilio.rest import Client
    from twilio.base.exceptions import TwilioRestException

    if from_number is None:
        from_number = get_from_number()

    client = Client(account_sid, auth_token)

    try:
        # Send message
        message = client.messages.create(
            body=body,
            from_=from_number,
            to=to_number
        )

        # Check message status
        if message.status in ['failed', 'undelivered']:
            return objict({
                'sent': False,
                'id': message.sid,
                'status': message.status,
                'code': message.error_code,
                'error': message.error_message
            })

        # Successfully queued/sent
        return objict({
            'sent': True,
            'id': message.sid,
            'status': message.status,
            'code': None,
            'error': None
        })

    except TwilioRestException as e:
        return objict({
            'sent': False,
            'id': None,
            'status': 'failed',
            'code': e.code,
            'error': e.msg
        })
    except Exception as e:
        return objict({
            'sent': False,
            'id': None,
            'status': 'failed',
            'code': None,
            'error': str(e)
        })
