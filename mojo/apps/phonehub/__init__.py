"""
PhoneHub - SMS and Phone Lookup Service

Provides SMS sending/receiving and phone number lookup capabilities
via Twilio and AWS SNS/Pinpoint.

Simple Usage:
    from mojo.apps import phonehub

    # Normalize phone number
    num = phonehub.normalize('+1 (415) 555-1234')

    # Lookup phone info
    phone = phonehub.lookup('+14155551234')
    print(f"Carrier: {phone.carrier}, Mobile: {phone.is_mobile}")

    # Send SMS
    sms = phonehub.send_sms('+14155551234', 'Hello from PhoneHub!')
"""
# Import convenience functions from services
from .services.phonenumbers import normalize, validate
from objict import objict


def lookup(phone_number):
    from .models import PhoneNumber
    return PhoneNumber.lookup(phone_number)


def send_sms(phone_number, message):
    from .models import SMS
    return SMS.send(body=message, to_number=phone_number)

def get_area_code_info(phone_number):
    from .services.area_codes import get_area_code_info
    return objict.fromdict(get_area_code_info(phone_number))
