"""
NANP (North American Numbering Plan) Area Codes
Valid area codes for USA, Canada, and Caribbean territories.

Source: North American Numbering Plan Administrator (NANPA)
Last Updated: January 2025
"""

# Valid NANP Area Codes (USA, Canada, Caribbean)
VALID_AREA_CODES = {
    # 200s
    201, 202, 203, 204, 205, 206, 207, 208, 209,
    210, 212, 213, 214, 215, 216, 217, 218, 219,
    220, 223, 224, 225, 226, 227, 228, 229,
    231, 234, 235, 236, 239,
    240, 242, 246, 248, 249,
    250, 251, 252, 253, 254, 256, 257,
    260, 262, 263, 264, 267, 268, 269,
    270, 272, 274, 276, 279,
    281, 283, 284, 289,

    # 300s
    301, 302, 303, 304, 305, 306, 307, 308, 309,
    310, 312, 313, 314, 315, 316, 317, 318, 319,
    320, 321, 323, 324, 325, 326, 327, 329,
    330, 331, 332, 334, 336, 337, 339,
    340, 341, 343, 345, 346, 347,
    350, 351, 352, 353, 354, 357,
    360, 361, 363, 364, 365, 367, 368, 369,

    # 400s
    401, 402, 403, 404, 405, 406, 407, 408, 409,
    410, 412, 413, 414, 415, 416, 417, 418, 419,
    423, 424, 425, 428,
    430, 431, 432, 434, 435, 436, 437, 438,
    440, 441, 442, 443, 445, 447, 448,
    450, 457, 458,
    463, 464, 468, 469,
    470, 472, 473, 474, 475, 478, 479,
    480, 484,

    # 500s
    500, 501, 502, 503, 504, 505, 506, 507, 508, 509,
    510, 512, 513, 514, 515, 516, 517, 518, 519,

    # 600s
    601, 602, 603, 604, 605, 606, 607, 608, 609,
    610, 612, 613, 614, 615, 616, 617, 618, 619,
    620, 621, 623, 626, 628, 629,
    630, 631, 636, 639,
    641, 645, 647,
    650, 659,
    661, 662, 667, 669,
    670, 671, 672, 673, 678, 679,
    680, 681, 682, 683, 684, 689,

    # 700s
    701, 702, 703, 704, 705, 706, 707, 708, 709,
    710, 712, 713, 714, 715, 716, 717, 718, 719,
    720, 721, 724, 725, 726, 727, 728, 729,
    731, 732, 734, 737, 738,
    740, 742, 743, 747,
    751, 752, 753, 754, 757,
    760, 762, 763, 765,
    770, 771, 772, 773, 774, 775, 778, 779,
    781, 782, 785, 786,

    # 800s
    801, 802, 803, 804, 805, 806, 807, 808,
    810, 812, 813, 814, 815, 816, 817, 818, 819,
    825,
    830, 831, 832, 835, 838,
    845, 847, 848,
    850,
    860, 862, 864, 865, 867,
    870, 872, 873, 878,

    # 900s
    901, 902, 903, 904, 905, 906, 907, 908, 909,
    910, 912, 913, 914, 915, 916, 917, 918, 919,
    920, 925, 929,
    931, 936, 937, 938,
    941, 942, 945, 947, 949,
    951, 952, 954, 956, 959,
    970, 971, 972, 973, 978, 979,
    980, 983, 984, 985, 986, 989,
}

# Toll-free area codes (not assigned to geographic locations)
TOLL_FREE_AREA_CODES = {
    800, 833, 844, 855, 866, 877, 888,
}

# Special service codes (N11 codes)
SERVICE_CODES = {
    211,  # Community information and referral services
    311,  # Non-emergency municipal services
    411,  # Directory assistance
    511,  # Traffic and travel information
    611,  # Telephone company repair service
    711,  # Telecommunications Relay Service (TRS)
    811,  # Underground public utility location
    911,  # Emergency services
}

# Premium rate area codes
PREMIUM_RATE_AREA_CODES = {
    900,  # Premium rate services
}

# All valid codes (including toll-free and premium)
ALL_VALID_CODES = VALID_AREA_CODES | TOLL_FREE_AREA_CODES | PREMIUM_RATE_AREA_CODES


def is_valid_area_code(area_code):
    """
    Check if area code is valid in NANP.

    Args:
        area_code: 3-digit area code (int or str)

    Returns:
        bool: True if valid area code
    """
    try:
        code = int(area_code)
        return code in ALL_VALID_CODES
    except (ValueError, TypeError):
        return False


def is_toll_free(area_code):
    """
    Check if area code is toll-free (800, 833, 844, 855, 866, 877, 888).

    Args:
        area_code: 3-digit area code (int or str)

    Returns:
        bool: True if toll-free
    """
    try:
        code = int(area_code)
        return code in TOLL_FREE_AREA_CODES
    except (ValueError, TypeError):
        return False


def is_premium_rate(area_code):
    """
    Check if area code is premium rate (900).

    Args:
        area_code: 3-digit area code (int or str)

    Returns:
        bool: True if premium rate
    """
    try:
        code = int(area_code)
        return code in PREMIUM_RATE_AREA_CODES
    except (ValueError, TypeError):
        return False


def get_area_code_info(area_code):
    """
    Get information about an area code.

    Can accept:
    - 3-digit area code: '415', 415, '212'
    - Full phone number: '+14155551234', '415-555-1234', '4155551234'

    Automatically extracts area code from phone numbers.

    Args:
        area_code: 3-digit area code (int or str) OR full phone number (str)

    Returns:
        dict: {
            'valid': bool,
            'area_code': str,
            'type': str (geographic, toll_free, premium, invalid),
            'description': str,
            'location': dict or None
        }
    """
    from . import area_code_mapping
    import re

    # Convert to string
    area_code_str = str(area_code)

    # Extract just digits
    digits = re.sub(r'\D', '', area_code_str)

    # Parse area code from various formats
    code = None

    if len(digits) == 3:
        # Just area code: '415'
        code = int(digits)
    elif len(digits) == 10:
        # 10-digit number: '4155551234'
        code = int(digits[:3])
    elif len(digits) == 11 and digits.startswith('1'):
        # 11-digit with country code: '14155551234'
        code = int(digits[1:4])
    elif len(digits) > 11:
        # Could be international or NANP with extra digits
        if digits.startswith('1'):
            # Might be NANP with extra digits - extract area code
            try:
                code = int(digits[1:4])
            except (ValueError, IndexError):
                pass

        # If not NANP (doesn't start with 1, or area code invalid), it's international
        if code is None or code not in ALL_VALID_CODES:
            return {
                'valid': False,
                'area_code': area_code_str,
                'type': 'international',
                'description': 'International number (non-NANP) - only USA/Canada/Caribbean supported',
                'location': None
            }

    if code is None:
        return {
            'valid': False,
            'area_code': area_code_str,
            'type': 'invalid',
            'description': 'Invalid format - expected 3-digit area code or NANP phone number',
            'location': None
        }

    code_str = str(code).zfill(3)

    # Get location info
    location = area_code_mapping.get_location_info(code)

    if code in TOLL_FREE_AREA_CODES:
        return {
            'valid': True,
            'area_code': code_str,
            'type': 'toll_free',
            'description': 'Toll-free number',
            'location': None
        }

    if code in PREMIUM_RATE_AREA_CODES:
        return {
            'valid': True,
            'area_code': code_str,
            'type': 'premium',
            'description': 'Premium rate service',
            'location': None
        }

    if code in VALID_AREA_CODES:
        return {
            'valid': True,
            'area_code': code_str,
            'type': 'geographic',
            'description': 'Geographic area code (USA/Canada/Caribbean)',
            'location': location
        }

    return {
        'valid': False,
        'area_code': code_str,
        'type': 'invalid',
        'description': 'Not a valid NANP area code',
        'location': None
    }
