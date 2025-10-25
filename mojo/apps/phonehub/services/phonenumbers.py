import re


def normalize(phone_number, country_code='US'):
    """
    Normalize phone number to E.164 format (+1234567890).

    Args:
        phone_number: Phone number string (various formats accepted)
        country_code: ISO country code for default country (default: US)

    Returns:
        Normalized phone number in E.164 format or None if invalid
    """
    if not phone_number:
        return None

    # Remove all non-digit characters
    digits = re.sub(r'\D', '', str(phone_number))

    # Handle different cases
    if not digits:
        return None

    # If starts with country code
    if digits.startswith('1') and len(digits) == 11:
        return f'+{digits}'
    elif len(digits) == 10:
        # US/Canada number without country code
        return f'+1{digits}'
    elif digits.startswith('1') and len(digits) > 11:
        # Invalid
        return None
    else:
        # Try to prepend + if not there
        if not phone_number.startswith('+'):
            return f'+{digits}'
        return phone_number

    return None
