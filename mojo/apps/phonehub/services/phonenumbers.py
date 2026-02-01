import re
from objict import objict
from . import international_codes


def detect_country(phone_number):
    """
    Detect country information from a phone number.

    Args:
        phone_number: Phone number string (with or without +)

    Returns:
        objict with country info or None if cannot detect:
        {
            'country_code': str (e.g., '1', '44', '86'),
            'country': str (e.g., 'USA/Canada/Caribbean (NANP)', 'United Kingdom'),
            'region': str (e.g., 'North America/Caribbean', 'Europe'),
            'is_nanp': bool
        }
    """
    if not phone_number:
        return None

    # Try to normalize to get clean E.164 format
    phone_str = str(phone_number)
    has_plus = phone_str.startswith('+')
    digits = re.sub(r'\D', '', phone_str)

    if not digits:
        return None

    # Build E.164 format for detection
    e164 = f'+{digits}' if has_plus or len(digits) >= 7 else None
    if not e164:
        return None

    # Use international_codes to detect
    country_info = international_codes.detect_country_code(e164)
    if country_info:
        return objict.fromdict(country_info)

    return None


def normalize(phone_number, country_code=None):
    """
    Normalize phone number to E.164 format (+1234567890).

    Handles NANP (USA/Canada/Caribbean) numbers based on country_code.
    - If country_code is US/CA: validates and normalizes NANP format
    - If country_code is None: auto-detects country from + prefix and validates
    - Returns normalized E.164 format or None if invalid for the country_code

    Args:
        phone_number: Phone number string (various formats accepted)
        country_code: ISO country code (default: None for auto-detect, 'US' for NANP)

    Returns:
        Normalized phone number in E.164 format (+...) or None if invalid
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

    # If country_code is None, detect country and validate accordingly
    if country_code is None:
        if not has_plus:
            # Without +, try to detect if it's NANP format (most common)
            if digits.startswith('1') and len(digits) == 11:
                # 11 digits starting with 1: likely NANP (14155551234)
                return f'+{digits}'
            elif len(digits) == 10:
                # 10 digits: likely NANP, add +1 (4155551234)
                return f'+1{digits}'
            else:
                # Can't reliably detect country without + for other formats
                return None

        # Has + prefix - detect country from the number
        country_info = detect_country(f'+{digits}')
        if not country_info:
            # Unknown country code, but valid E.164 format
            if 7 <= len(digits) <= 15:
                return f'+{digits}'
            return None

        # Validate based on detected country
        if country_info.is_nanp:
            # It's NANP, validate as such
            if digits.startswith('1') and len(digits) == 11:
                return f'+{digits}'
            else:
                return None
        else:
            # International number - validate E.164 format
            if 7 <= len(digits) <= 15:
                return f'+{digits}'
            else:
                return None

    # Country-specific normalization (NANP for US/CA)
    if country_code in ['US', 'CA']:
        # NANP validation
        if has_plus:
            # Already has +, must start with 1 and be 11 digits
            if digits.startswith('1') and len(digits) == 11:
                return f'+{digits}'
            else:
                # Not a valid NANP number
                return None
        else:
            # No + prefix
            if digits.startswith('1') and len(digits) == 11:
                # 11 digits starting with 1: 14155551234 (NANP with country code)
                return f'+{digits}'
            elif len(digits) == 10:
                # 10 digits: 4155551234 - add +1 for NANP
                return f'+1{digits}'
            else:
                # Invalid length for NANP
                return None
    else:
        # Other country codes - require + prefix for safety
        if has_plus and 7 <= len(digits) <= 15:
            return f'+{digits}'
        else:
            return None


def validate(phone_number, country_code=None, detailed=False):
    resp = objict.fromdict(_validate(phone_number, country_code))
    if detailed:
        return resp
    return resp.valid


def _validate(phone_number, country_code=None):
    """
    Validate phone number format.

    - For NANP (US/CA): performs full NANP validation
    - For international: validates E.164 format and detects country

    Args:
        phone_number: Phone number string (various formats)
        country_code: ISO country code (default: None for auto-detect, 'US' for NANP only)

    Returns:
        dict: {
            'valid': bool,
            'normalized': str or None (E.164 format if valid),
            'error': str or None,
            'area_code_info': dict or None (NANP only),
            'country_info': dict or None (international only)
        }
    """
    from . import area_codes

    # First try to normalize with the given country code
    normalized = normalize(phone_number, country_code)

    if not normalized:
        # If normalize failed and country_code is None, try auto-detect
        # If country_code was explicitly set (US/CA), don't fall back to international
        if country_code is None:
            # Already tried with None, so it's invalid
            return {
                'valid': False,
                'normalized': None,
                'error': 'Invalid phone number format',
                'area_code_info': None
            }
        else:
            # Explicit country_code failed, check if it's international for helpful error
            normalized_intl = normalize(phone_number, country_code=None)
            if normalized_intl:
                country_info = international_codes.detect_country_code(normalized_intl)
                if country_info and not country_info['is_nanp']:
                    # It's a valid international number, but not the requested country
                    return {
                        'valid': False,
                        'normalized': None,
                        'error': f"International number detected: {country_info['country']} (+{country_info['country_code']}) - expected {country_code} number",
                        'area_code_info': None,
                        'country_info': country_info
                    }

            return {
                'valid': False,
                'normalized': None,
                'error': f'Invalid {country_code} phone number format',
                'area_code_info': None
            }

    # Extract digits (remove +)
    digits = normalized[1:] if normalized.startswith('+') else normalized

    # Check if it's NANP (USA/Canada) - must start with 1
    if not digits.startswith('1') or len(digits) != 11:
        # Not NANP - check if it's a valid international number
        country_info = international_codes.detect_country_code(normalized)
        if country_info and not country_info['is_nanp']:
            # Valid international number
            return {
                'valid': True,
                'normalized': normalized,
                'error': None,
                'area_code_info': None,
                'country_info': country_info
            }

        return {
            'valid': False,
            'normalized': normalized,
            'error': 'Not a valid phone number',
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
