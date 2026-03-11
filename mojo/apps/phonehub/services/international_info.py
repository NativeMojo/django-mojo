"""
International phone number type detection from national number prefixes.
Covers major countries already in INTERNATIONAL_CODES.

Rules are applied to the national significant number (digits after the country code).
Prefix matching is tried in order: mobile → freephone → premium → geographic → landline.
"""

# {country_code_str: {type: [prefixes, ...]}}
NUMBER_TYPE_RULES = {
    '44': {  # United Kingdom
        'mobile':    ['7'],
        'freephone': ['800', '808'],
        'premium':   ['90'],
        'geographic': ['1', '2'],
    },
    '33': {  # France
        'mobile':    ['6', '7'],
        'freephone': ['800', '801', '802', '803', '804', '805'],
        'premium':   ['89'],
        'geographic': ['1', '2', '3', '4', '5'],
    },
    '49': {  # Germany
        'mobile':    ['15', '16', '17'],
        'freephone': ['800'],
        'premium':   ['900'],
    },
    '34': {  # Spain
        'mobile':    ['6', '7'],
        'freephone': ['800', '900'],
        'premium':   ['803', '806', '807'],
        'geographic': ['9'],
    },
    '39': {  # Italy
        'mobile':    ['3'],
        'freephone': ['800'],
        'premium':   ['899'],
        'geographic': ['0'],
    },
    '31': {  # Netherlands
        'mobile':    ['6'],
        'freephone': ['800'],
        'premium':   ['900', '906', '909'],
    },
    '32': {  # Belgium
        'mobile':    ['4'],
        'freephone': ['800'],
        'premium':   ['900'],
    },
    '41': {  # Switzerland
        'mobile':    ['7'],
        'freephone': ['800'],
        'premium':   ['900'],
    },
    '46': {  # Sweden
        'mobile':    ['7'],
        'freephone': ['20'],
        'premium':   ['900', '939', '944'],
    },
    '47': {  # Norway
        'mobile':    ['4', '9'],
        'freephone': ['800'],
        'premium':   ['820'],
    },
    '45': {  # Denmark
        'mobile':    ['2', '3', '4', '5', '6', '7', '8', '9'],
        'freephone': ['80'],
        'premium':   ['90'],
    },
    '358': {  # Finland
        'mobile':    ['4', '5'],
        'freephone': ['800'],
        'premium':   ['600', '700'],
    },
    '353': {  # Ireland
        'mobile':    ['8'],
        'freephone': ['1800'],
        'premium':   ['15', '19'],
    },
    '48': {  # Poland
        'mobile':    ['5', '6', '7'],
        'freephone': ['800'],
        'premium':   ['700'],
    },
    '351': {  # Portugal
        'mobile':    ['9'],
        'freephone': ['800'],
        'premium':   ['760', '761'],
    },
    '30': {  # Greece
        'mobile':    ['6'],
        'freephone': ['800'],
        'premium':   ['90'],
    },
    '61': {  # Australia
        'mobile':    ['4'],
        'freephone': ['1800'],
        'premium':   ['1900'],
        'geographic': ['2', '3', '7', '8'],
    },
    '64': {  # New Zealand
        'mobile':    ['2'],
        'freephone': ['800', '508'],
        'premium':   ['900'],
    },
    '81': {  # Japan
        'mobile':    ['70', '80', '90'],
        'freephone': ['120', '800'],
        'premium':   ['990'],
    },
    '82': {  # South Korea
        'mobile':    ['10'],
        'freephone': ['80'],
    },
    '86': {  # China
        'mobile':    ['13', '14', '15', '16', '17', '18', '19'],
    },
    '91': {  # India
        'mobile':    ['6', '7', '8', '9'],
        'freephone': ['1800'],
    },
    '52': {  # Mexico
        'mobile':    ['1'],
        'freephone': ['800'],
        'premium':   ['900'],
    },
    '55': {  # Brazil
        'freephone': ['800', '0800'],
        'premium':   ['900'],
    },
    '7': {   # Russia/Kazakhstan
        'mobile':    ['9'],
        'freephone': ['800'],
    },
}

# Check order — more specific types first
_TYPE_ORDER = ('mobile', 'freephone', 'premium', 'geographic')


def get_international_type(country_code, national_number):
    """
    Detect the line type for an international number using known prefix rules.

    Args:
        country_code:    Country code string (e.g. "44", "33")
        national_number: Digits after the country code (e.g. "7398178096")

    Returns:
        str: "mobile", "freephone", "premium", "geographic", "landline", or None
             None means the country has no rules defined.
             "landline" is returned when the country has rules but no prefix matched.
    """
    rules = NUMBER_TYPE_RULES.get(str(country_code))
    if not rules:
        return None
    for type_name in _TYPE_ORDER:
        for prefix in rules.get(type_name, []):
            if national_number.startswith(prefix):
                return type_name
    return 'landline'
