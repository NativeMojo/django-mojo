from testit import helpers as th
from mojo.apps import phonehub


@th.django_unit_test()
def test_normalize_10_digit_number(opts):
    """Test normalizing 10-digit USA phone number"""
    result = phonehub.normalize('4155551234')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"


@th.django_unit_test()
def test_normalize_11_digit_number_with_1(opts):
    """Test normalizing 11-digit number starting with 1"""
    result = phonehub.normalize('14155551234')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"


@th.django_unit_test()
def test_normalize_formatted_number(opts):
    """Test normalizing formatted phone number with punctuation"""
    result = phonehub.normalize('(415) 555-1234')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"

    result = phonehub.normalize('415-555-1234')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"

    result = phonehub.normalize('415.555.1234')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"


@th.django_unit_test()
def test_normalize_e164_format(opts):
    """Test normalizing phone number already in E.164 format"""
    result = phonehub.normalize('+14155551234')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"


@th.django_unit_test()
def test_normalize_canada_number(opts):
    """Test normalizing Canadian phone number"""
    result = phonehub.normalize('6135551234')
    assert result == '+16135551234', f"Expected +16135551234, got {result}"

    result = phonehub.normalize('+16135551234')
    assert result == '+16135551234', f"Expected +16135551234, got {result}"


@th.django_unit_test()
def test_normalize_international_rejected(opts):
    """Test that international numbers are rejected"""
    # France
    result = phonehub.normalize('+3322312111')
    assert result is None, f"Expected None for French number, got {result}"

    # UK
    result = phonehub.normalize('+442071234567')
    assert result is None, f"Expected None for UK number, got {result}"

    # Germany
    result = phonehub.normalize('+4915112345678')
    assert result is None, f"Expected None for German number, got {result}"


@th.django_unit_test()
def test_normalize_invalid_formats(opts):
    """Test that invalid formats return None"""
    # Too short
    result = phonehub.normalize('123')
    assert result is None, f"Expected None for too short number, got {result}"

    # Too long (not starting with 1)
    result = phonehub.normalize('41555512345678')
    assert result is None, f"Expected None for too long number, got {result}"

    # Empty string
    result = phonehub.normalize('')
    assert result is None, f"Expected None for empty string, got {result}"

    # None
    result = phonehub.normalize(None)
    assert result is None, f"Expected None for None input, got {result}"


@th.django_unit_test()
def test_normalize_with_spaces(opts):
    """Test normalizing numbers with various spacing"""
    result = phonehub.normalize('+1 415 555 1234')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"

    result = phonehub.normalize('1 (415) 555-1234')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"


@th.django_unit_test()
def test_validate_simple_valid_number(opts):
    """Test simple boolean validation for valid number"""
    result = phonehub.validate('4155551234')
    assert result is True, f"Expected True for valid number, got {result}"


@th.django_unit_test()
def test_validate_simple_invalid_number(opts):
    """Test simple boolean validation for invalid number"""
    result = phonehub.validate('123')
    assert result is False, f"Expected False for invalid number, got {result}"


@th.django_unit_test()
def test_validate_detailed_valid_number(opts):
    """Test detailed validation response for valid number"""
    result = phonehub.validate('4155551234', detailed=True)

    assert result.valid is True, f"Expected valid=True, got {result.valid}"
    assert result.normalized == '+14155551234', f"Expected +14155551234, got {result.normalized}"
    assert result.area_code == '415', f"Expected area_code=415, got {result.area_code}"
    assert result.error is None, f"Expected no error, got {result.error}"
    assert result.area_code_info is not None, f"Expected area_code_info"
    assert result.area_code_info.valid is True, f"Expected valid area code"


@th.django_unit_test()
def test_validate_detailed_canada_number(opts):
    """Test detailed validation for Canadian number"""
    result = phonehub.validate('+16135551234', detailed=True)

    assert result.valid is True, f"Expected valid=True, got {result.valid}"
    assert result.normalized == '+16135551234', f"Expected +16135551234, got {result.normalized}"
    assert result.area_code == '613', f"Expected area_code=613, got {result.area_code}"
    assert result.area_code_info.location.country == 'CA', f"Expected Canada"


@th.django_unit_test()
def test_validate_invalid_area_code(opts):
    """Test validation rejects invalid area codes"""
    # 111 is not a valid area code (N11 codes not allowed)
    result = phonehub.validate('1115551234', detailed=True)

    assert result.valid is False, f"Expected valid=False, got {result.valid}"
    assert 'Invalid area code' in result.error, f"Expected area code error, got {result.error}"


@th.django_unit_test()
def test_validate_invalid_exchange(opts):
    """Test validation rejects invalid exchange codes"""
    # Exchange cannot start with 0 or 1
    result = phonehub.validate('4150551234', detailed=True)

    assert result.valid is False, f"Expected valid=False, got {result.valid}"
    assert 'Invalid exchange' in result.error, f"Expected exchange error, got {result.error}"

    result = phonehub.validate('4151551234', detailed=True)
    assert result.valid is False, f"Expected valid=False for exchange starting with 1"


@th.django_unit_test()
def test_validate_n11_service_codes(opts):
    """Test validation rejects N11 service codes in exchange"""
    # 411, 911, etc. are service codes
    result = phonehub.validate('4154111234', detailed=True)

    assert result.valid is False, f"Expected valid=False, got {result.valid}"
    assert 'N11 service codes' in result.error, f"Expected N11 error, got {result.error}"


@th.django_unit_test()
def test_validate_all_same_digits(opts):
    """Test validation rejects numbers with all same digits"""
    # Use 8888888888 - 888 is a valid toll-free area code but all same digits should be rejected
    result = phonehub.validate('8888888888', detailed=True)

    assert result.valid is False, f"Expected valid=False, got {result.valid}"
    assert 'all same digits' in result.error, f"Expected same digits error, got {result.error}"


@th.django_unit_test()
def test_validate_international_france(opts):
    """Test validation provides helpful error for French number"""
    result = phonehub.validate('+3322312111', detailed=True)

    assert result.valid is False, f"Expected valid=False, got {result.valid}"
    assert result.normalized is None, f"Expected None normalized, got {result.normalized}"
    assert 'France' in result.error, f"Expected France in error, got {result.error}"
    assert '+33' in result.error, f"Expected +33 in error, got {result.error}"
    assert result.international is not None, f"Expected international info"
    assert result.international.country == 'France', f"Expected France country"
    assert result.international.country_code == '33', f"Expected country code 33"


@th.django_unit_test()
def test_validate_international_uk(opts):
    """Test validation provides helpful error for UK number"""
    result = phonehub.validate('+442071234567', detailed=True)

    assert result.valid is False, f"Expected valid=False"
    assert 'United Kingdom' in result.error, f"Expected UK in error, got {result.error}"
    assert '+44' in result.error, f"Expected +44 in error"
    assert result.international.country == 'United Kingdom', f"Expected UK country"


@th.django_unit_test()
def test_validate_international_germany(opts):
    """Test validation provides helpful error for German number"""
    result = phonehub.validate('+4915112345678', detailed=True)

    assert result.valid is False, f"Expected valid=False"
    assert 'Germany' in result.error, f"Expected Germany in error, got {result.error}"
    assert result.international.country == 'Germany', f"Expected Germany country"


@th.django_unit_test()
def test_validate_international_japan(opts):
    """Test validation provides helpful error for Japanese number"""
    result = phonehub.validate('+81312345678', detailed=True)

    assert result.valid is False, f"Expected valid=False"
    assert 'Japan' in result.error, f"Expected Japan in error, got {result.error}"
    assert result.international.region == 'Asia-Pacific', f"Expected Asia-Pacific region"


@th.django_unit_test()
def test_validate_toll_free_numbers(opts):
    """Test validation accepts toll-free numbers"""
    toll_free_codes = ['800', '888', '877', '866', '855', '844', '833']

    for code in toll_free_codes:
        result = phonehub.validate(f'{code}5551234', detailed=True)
        assert result.valid is True, f"Expected {code} toll-free to be valid"
        assert result.area_code_info.type == 'toll_free', f"Expected toll_free type for {code}"


@th.django_unit_test()
def test_get_area_code_info_basic(opts):
    """Test getting area code info for basic 3-digit code"""
    info = phonehub.get_area_code_info('415')

    assert info.valid is True, f"Expected valid=True"
    assert info.area_code == '415', f"Expected area_code=415"
    assert info.location.state == 'CA', f"Expected California"
    assert info.location.region == 'San Francisco', f"Expected San Francisco"
    assert info.type == 'geographic', f"Expected geographic type"


@th.django_unit_test()
def test_get_area_code_info_from_full_number(opts):
    """Test parsing area code from full phone number"""
    # E.164 format
    info = phonehub.get_area_code_info('+14155551234')
    assert info.area_code == '415', f"Expected area_code=415 from full number"
    assert info.location.state == 'CA', f"Expected California"

    # 10 digits
    info = phonehub.get_area_code_info('6135551234')
    assert info.area_code == '613', f"Expected area_code=613"
    assert info.location.country == 'CA', f"Expected Canada"

    # 11 digits with 1
    info = phonehub.get_area_code_info('14155551234')
    assert info.area_code == '415', f"Expected area_code=415"


@th.django_unit_test()
def test_get_area_code_info_formatted_number(opts):
    """Test parsing area code from formatted phone number"""
    info = phonehub.get_area_code_info('(415) 555-1234')
    assert info.area_code == '415', f"Expected area_code=415 from formatted number"

    info = phonehub.get_area_code_info('415-555-1234')
    assert info.area_code == '415', f"Expected area_code=415"


@th.django_unit_test()
def test_get_area_code_info_invalid(opts):
    """Test getting info for invalid area code"""
    info = phonehub.get_area_code_info('999')

    assert info.valid is False, f"Expected valid=False for invalid code"
    assert info.area_code == '999', f"Expected area_code=999"
    assert info.type == 'invalid', f"Expected type=invalid"
    assert 'Not a valid NANP' in info.description, f"Expected NANP error in description"


@th.django_unit_test()
def test_get_area_code_info_multiple_states(opts):
    """Test area codes that span multiple states"""
    # 201 is New Jersey
    info = phonehub.get_area_code_info('201')
    assert info.valid is True, f"Expected valid"
    assert info.location.state == 'NJ', f"Expected New Jersey"

    # 202 is Washington DC
    info = phonehub.get_area_code_info('202')
    assert info.location.state == 'DC', f"Expected DC"

    # 212 is New York
    info = phonehub.get_area_code_info('212')
    assert info.location.state == 'NY', f"Expected New York"


@th.django_unit_test()
def test_get_area_code_info_toll_free(opts):
    """Test getting info for toll-free numbers"""
    info = phonehub.get_area_code_info('800')

    assert info.valid is True, f"Expected valid"
    assert info.type == 'toll_free', f"Expected toll_free type"
    assert 'Toll-free' in info.description, f"Expected Toll-free in description"
