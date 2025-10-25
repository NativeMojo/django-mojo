import re
from objict import objict
from . import international_codes


def normalize(phone_number, country_code='US'):
    """
    Normalize phone number to E.164 format (+1234567890).
    Only handles NANP (USA/Canada/Caribbean) numbers.

    Args:
        phone_number: Phone number string (various formats accepted)
        country_code: ISO country code for default country (default: US)

    Returns:
        Normalized phone number in E.164 format or None if invalid/international
    """
    if not phone_number:
        return None

    phone_str = str(phone_number)

    # Check if it starts with + (E.164 format indicator)
    has_plus = phone_str.startswith('+')

    # Remove all non-digit characters
    digits = re.sub(r'\D', '', phone_str)

    # Handle different cases
    if not digits:
        return None

    # If original had +, check if it's NANP (country code 1)
    if has_plus:
        # E.164 format with explicit country code
        if digits.startswith('1') and len(digits) == 11:
            # Valid NANP: +14155551234
            return f'+{digits}'
        elif not digits.startswith('1'):
            # International number (not NANP)
            return None
        else:
            # Starts with 1 but wrong length
            return None

    # No + prefix - assume NANP if reasonable length
    if digits.startswith('1') and len(digits) == 11:
        # 11 digits starting with 1: 14155551234
        return f'+{digits}'
    elif len(digits) == 10:
        # 10 digits: 4155551234 - assume NANP, add country code
        return f'+1{digits}'
    elif digits.startswith('1') and len(digits) > 11:
        # Too long, invalid
        return None
    elif len(digits) > 10:
        # Ambiguous - could be international, reject
        return None
    else:
        # Less than 10 digits, invalid
        return None


def validate(phone_number, country_code='US', detailed=False):
    resp = objict.fromdict(_validate(phone_number, country_code))
    if detailed:
        return resp
    return resp.valid


def _validate(phone_number, country_code='US'):
    """
    Validate if phone number is valid for USA/Canada (NANP - North American Numbering Plan).

    Args:
        phone_number: Phone number string (various formats)
        country_code: ISO country code (default: US)

    Returns:
        dict: {
            'valid': bool,
            'normalized': str or None (E.164 format if valid),
            'error': str or None,
            'area_code_info': dict or None (info about the area code)
        }
    """
    from . import area_codes
    # First normalize
    normalized = normalize(phone_number, country_code)

    if not normalized:
        # Check if it's an international number
        country_info = international_codes.detect_country_code(phone_number)
        if country_info and not country_info['is_nanp']:
            return {
                'valid': False,
                'normalized': None,
                'error': f"International number detected: {country_info['country']} (+{country_info['country_code']}) - only USA/Canada/Caribbean supported",
                'area_code_info': None,
                'international': country_info
            }

        return {
            'valid': False,
            'normalized': None,
            'error': 'Invalid phone number format',
            'area_code_info': None
        }

    # Extract digits (remove +)
    digits = normalized[1:] if normalized.startswith('+') else normalized

    # Check if it's NANP (USA/Canada) - must start with 1
    if not digits.startswith('1') or len(digits) != 11:
        return {
            'valid': False,
            'normalized': normalized,
            'error': 'Not a valid USA/Canada number (NANP)',
            'area_code_info': None
        }

    # Extract NPA (area code) and NXX (exchange)
    npa = digits[1:4]  # Area code (positions 1-3 after country code)
    nxx = digits[4:7]  # Exchange (positions 4-6)

    # NANP validation rules:
    # 1. Check if area code exists in NANP database
    if not area_codes.is_valid_area_code(npa):
        area_code_info = area_codes.get_area_code_info(npa)
        return {
            'valid': False,
            'normalized': normalized,
            'error': f'Invalid area code: {npa} (not assigned in NANP)',
            'area_code_info': area_code_info
        }

    # 2. NPA (area code) cannot start with 0 or 1 (already validated by database, but double-check)
    if npa[0] in ['0', '1']:
        return {
            'valid': False,
            'normalized': normalized,
            'error': f'Invalid area code: {npa} (cannot start with 0 or 1)',
            'area_code_info': None
        }

    # 3. NXX (exchange) cannot start with 0 or 1
    if nxx[0] in ['0', '1']:
        return {
            'valid': False,
            'normalized': normalized,
            'error': f'Invalid exchange: {nxx} (cannot start with 0 or 1)',
            'area_code_info': None
        }

    # 4. Check for invalid patterns (N11 codes in area code position)
    if npa[1:3] == '11':
        return {
            'valid': False,
            'normalized': normalized,
            'error': f'Invalid area code: {npa} (N11 codes not allowed)',
            'area_code_info': None
        }

    # 5. Check for service codes in exchange position (N11)
    if nxx[1:3] == '11':
        # 211, 311, 411, 511, 611, 711, 811, 911 are service codes
        return {
            'valid': False,
            'normalized': normalized,
            'error': f'Invalid exchange: {nxx} (N11 service codes not allowed)',
            'area_code_info': None
        }

    # 6. Check for all same digits (likely invalid)
    if len(set(digits[1:])) == 1:  # Skip country code
        return {
            'valid': False,
            'normalized': normalized,
            'error': 'Invalid number (all same digits)',
            'area_code_info': None
        }

    # Valid NANP number
    return {
        'valid': True,
        'normalized': normalized,
        'area_code': npa,
        'area_code_info': area_codes.get_area_code_info(npa),
        'error': None
    }
