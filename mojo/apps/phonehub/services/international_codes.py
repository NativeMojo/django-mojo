"""
International dialing codes for common countries.
Used to detect and provide helpful information about non-NANP numbers.

Source: ITU-T E.164 international calling codes
Last Updated: January 2025
"""

# Common international country codes
INTERNATIONAL_CODES = {
    # Europe
    33: {'country': 'France', 'region': 'Europe'},
    34: {'country': 'Spain', 'region': 'Europe'},
    39: {'country': 'Italy', 'region': 'Europe'},
    41: {'country': 'Switzerland', 'region': 'Europe'},
    43: {'country': 'Austria', 'region': 'Europe'},
    44: {'country': 'United Kingdom', 'region': 'Europe'},
    45: {'country': 'Denmark', 'region': 'Europe'},
    46: {'country': 'Sweden', 'region': 'Europe'},
    47: {'country': 'Norway', 'region': 'Europe'},
    48: {'country': 'Poland', 'region': 'Europe'},
    49: {'country': 'Germany', 'region': 'Europe'},

    # Asia-Pacific
    61: {'country': 'Australia', 'region': 'Asia-Pacific'},
    62: {'country': 'Indonesia', 'region': 'Asia-Pacific'},
    63: {'country': 'Philippines', 'region': 'Asia-Pacific'},
    64: {'country': 'New Zealand', 'region': 'Asia-Pacific'},
    65: {'country': 'Singapore', 'region': 'Asia-Pacific'},
    66: {'country': 'Thailand', 'region': 'Asia-Pacific'},
    81: {'country': 'Japan', 'region': 'Asia-Pacific'},
    82: {'country': 'South Korea', 'region': 'Asia-Pacific'},
    84: {'country': 'Vietnam', 'region': 'Asia-Pacific'},
    86: {'country': 'China', 'region': 'Asia-Pacific'},
    91: {'country': 'India', 'region': 'Asia-Pacific'},
    92: {'country': 'Pakistan', 'region': 'Asia-Pacific'},
    93: {'country': 'Afghanistan', 'region': 'Asia-Pacific'},
    94: {'country': 'Sri Lanka', 'region': 'Asia-Pacific'},
    95: {'country': 'Myanmar', 'region': 'Asia-Pacific'},
    98: {'country': 'Iran', 'region': 'Asia-Pacific'},

    # Middle East
    20: {'country': 'Egypt', 'region': 'Middle East/Africa'},
    27: {'country': 'South Africa', 'region': 'Middle East/Africa'},
    30: {'country': 'Greece', 'region': 'Europe'},
    31: {'country': 'Netherlands', 'region': 'Europe'},
    32: {'country': 'Belgium', 'region': 'Europe'},
    90: {'country': 'Turkey', 'region': 'Middle East'},
    971: {'country': 'United Arab Emirates', 'region': 'Middle East'},
    972: {'country': 'Israel', 'region': 'Middle East'},
    973: {'country': 'Bahrain', 'region': 'Middle East'},
    974: {'country': 'Qatar', 'region': 'Middle East'},

    # Latin America (3-digit codes)
    51: {'country': 'Peru', 'region': 'South America'},
    52: {'country': 'Mexico', 'region': 'North America'},
    53: {'country': 'Cuba', 'region': 'Caribbean'},
    54: {'country': 'Argentina', 'region': 'South America'},
    55: {'country': 'Brazil', 'region': 'South America'},
    56: {'country': 'Chile', 'region': 'South America'},
    57: {'country': 'Colombia', 'region': 'South America'},
    58: {'country': 'Venezuela', 'region': 'South America'},

    # More Europe
    351: {'country': 'Portugal', 'region': 'Europe'},
    352: {'country': 'Luxembourg', 'region': 'Europe'},
    353: {'country': 'Ireland', 'region': 'Europe'},
    354: {'country': 'Iceland', 'region': 'Europe'},
    358: {'country': 'Finland', 'region': 'Europe'},
    359: {'country': 'Bulgaria', 'region': 'Europe'},
    370: {'country': 'Lithuania', 'region': 'Europe'},
    371: {'country': 'Latvia', 'region': 'Europe'},
    372: {'country': 'Estonia', 'region': 'Europe'},
    380: {'country': 'Ukraine', 'region': 'Europe'},

    # Russia
    7: {'country': 'Russia/Kazakhstan', 'region': 'Europe/Asia'},
}


def detect_country_code(phone_number):
    """
    Detect international country code from a phone number.

    Args:
        phone_number: Phone number string (with or without +)

    Returns:
        dict or None: {
            'country_code': str,
            'country': str,
            'region': str,
            'is_nanp': bool
        }
    """
    import re

    if not phone_number:
        return None

    # Extract digits
    digits = re.sub(r'\D', '', str(phone_number))

    if not digits:
        return None

    # Check for NANP (country code 1)
    if digits.startswith('1'):
        return {
            'country_code': '1',
            'country': 'USA/Canada/Caribbean (NANP)',
            'region': 'North America/Caribbean',
            'is_nanp': True
        }

    # Try 3-digit country codes first
    if len(digits) >= 3:
        code_3 = int(digits[:3])
        if code_3 in INTERNATIONAL_CODES:
            info = INTERNATIONAL_CODES[code_3]
            return {
                'country_code': str(code_3),
                'country': info['country'],
                'region': info['region'],
                'is_nanp': False
            }

    # Try 2-digit country codes
    if len(digits) >= 2:
        code_2 = int(digits[:2])
        if code_2 in INTERNATIONAL_CODES:
            info = INTERNATIONAL_CODES[code_2]
            return {
                'country_code': str(code_2),
                'country': info['country'],
                'region': info['region'],
                'is_nanp': False
            }

    # Try 1-digit country codes (like 7 for Russia)
    if len(digits) >= 1:
        code_1 = int(digits[:1])
        if code_1 in INTERNATIONAL_CODES:
            info = INTERNATIONAL_CODES[code_1]
            return {
                'country_code': str(code_1),
                'country': info['country'],
                'region': info['region'],
                'is_nanp': False
            }

    # Unknown country code
    return {
        'country_code': 'unknown',
        'country': 'Unknown',
        'region': 'Unknown',
        'is_nanp': False
    }


def is_international(phone_number):
    """
    Check if phone number is international (non-NANP).

    Args:
        phone_number: Phone number string

    Returns:
        bool: True if international (non-NANP)
    """
    info = detect_country_code(phone_number)
    return info and not info['is_nanp']
