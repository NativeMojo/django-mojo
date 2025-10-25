from mojo.helpers.settings import settings
from objict import objict

ACCOUNT_SID = settings.get('TWILIO_ACCOUNT_SID')
AUTH_TOKEN = settings.get('TWILIO_AUTH_TOKEN')
FROM_NUMBER = settings.get('TWILIO_FROM_NUMBER')
PROVIDER = "twilio"

def lookup(phone_number):
    try:
        resp = _lookup(phone_number, ACCOUNT_SID, AUTH_TOKEN)
    except Exception as e:
        resp = objict(error=str(e))
    return resp


def send_sms(body, to_number, from_number=FROM_NUMBER, account_sid=ACCOUNT_SID, auth_token=AUTH_TOKEN):
    return _send_sms(body, to_number, from_number, account_sid, auth_token)


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
