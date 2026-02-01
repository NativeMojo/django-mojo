from testit import helpers as th
from mojo.apps import phonehub


@th.django_unit_test()
def test_normalize_10_digit_number(opts):
    """Test normalizing 10-digit USA phone number"""
    result = phonehub.normalize('4155551234', 'US')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"


@th.django_unit_test()
def test_normalize_11_digit_number_with_1(opts):
    """Test normalizing 11-digit number starting with 1"""
    result = phonehub.normalize('14155551234', 'US')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"


@th.django_unit_test()
def test_normalize_formatted_number(opts):
    """Test normalizing formatted phone number with punctuation"""
    result = phonehub.normalize('(415) 555-1234', 'US')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"

    result = phonehub.normalize('415-555-1234', 'US')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"

    result = phonehub.normalize('415.555.1234', 'US')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"


@th.django_unit_test()
def test_normalize_e164_format(opts):
    """Test normalizing phone number already in E.164 format"""
    result = phonehub.normalize('+14155551234')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"


@th.django_unit_test()
def test_normalize_e164_international(opts):
    """Test normalizing international E.164 numbers with auto-detect"""
    # UK number
    result = phonehub.normalize('+447700900123')
    assert result == '+447700900123', f"Expected +447700900123, got {result}"

    # France number
    result = phonehub.normalize('+33123456789')
    assert result == '+33123456789', f"Expected +33123456789, got {result}"

    # Germany number
    result = phonehub.normalize('+4915112345678')
    assert result == '+4915112345678', f"Expected +4915112345678, got {result}"


@th.django_unit_test()
def test_normalize_canada_number(opts):
    """Test normalizing Canadian phone number"""
    result = phonehub.normalize('6135551234', 'US')
    assert result == '+16135551234', f"Expected +16135551234, got {result}"

    result = phonehub.normalize('+16135551234')
    assert result == '+16135551234', f"Expected +16135551234, got {result}"


@th.django_unit_test()
def test_normalize_international_rejected(opts):
    """Test that international numbers are rejected"""
    # France
    result = phonehub.normalize('+3322312111', 'US')
    assert result is None, f"Expected None for French number, got {result}"

    # UK
    result = phonehub.normalize('+442071234567', 'US')
    assert result is None, f"Expected None for UK number, got {result}"

    # Germany
    result = phonehub.normalize('+4915112345678', 'US')
    assert result is None, f"Expected None for German number, got {result}"


@th.django_unit_test()
def test_normalize_invalid_formats(opts):
    """Test that invalid formats return None"""
    # Too short
    result = phonehub.normalize('123', 'US')
    assert result is None, f"Expected None for too short number, got {result}"

    # Too long (not starting with 1)
    result = phonehub.normalize('41555512345678', 'US')
    assert result is None, f"Expected None for too long number, got {result}"

    # Empty string
    result = phonehub.normalize('', 'US')
    assert result is None, f"Expected None for empty string, got {result}"

    # None
    result = phonehub.normalize(None, 'US')
    assert result is None, f"Expected None for None input, got {result}"

    # Number without + and country_code=None now assumes NANP (10 digits)
    result = phonehub.normalize('4155551234')
    assert result == '+14155551234', f"Expected +14155551234 (auto-detected NANP), got {result}"

    # 11 digits starting with 1 also works
    result = phonehub.normalize('14155551234')
    assert result == '+14155551234', f"Expected +14155551234 (auto-detected NANP), got {result}"


@th.django_unit_test()
def test_normalize_with_spaces(opts):
    """Test normalizing numbers with various spacing"""
    result = phonehub.normalize('+1 415 555 1234')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"

    result = phonehub.normalize('1 (415) 555-1234', 'US')
    assert result == '+14155551234', f"Expected +14155551234, got {result}"


@th.django_unit_test()
def test_validate_simple_valid_number(opts):
    """Test simple boolean validation for valid number"""
    result = phonehub.validate('4155551234', 'US')
    assert result is True, f"Expected True for valid number, got {result}"


@th.django_unit_test()
def test_validate_simple_invalid_number(opts):
    """Test simple boolean validation for invalid number"""
    result = phonehub.validate('123', 'US')
    assert result is False, f"Expected False for invalid number, got {result}"


@th.django_unit_test()
def test_validate_detailed_valid_number(opts):
    """Test detailed validation response for valid number"""
    result = phonehub.validate('4155551234', 'US', detailed=True)

    assert result.valid is True, f"Expected valid=True, got {result.valid}"
    assert result.normalized == '+14155551234', f"Expected +14155551234, got {result.normalized}"
    assert result.area_code == '415', f"Expected area_code=415, got {result.area_code}"
    assert result.error is None, f"Expected no error, got {result.error}"
    assert result.area_code_info is not None, f"Expected area_code_info"
    assert result.area_code_info.valid is True, f"Expected valid area code"


@th.django_unit_test()
def test_validate_detailed_canada_number(opts):
    """Test detailed validation for Canadian number"""
    result = phonehub.validate('+16135551234', 'US', detailed=True)

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
    """Test validation accepts valid French number with auto-detect"""
    result = phonehub.validate('+33123456789', detailed=True)

    assert result.valid is True, f"Expected valid=True, got {result.valid}"
    assert result.normalized == '+33123456789', f"Expected +33123456789, got {result.normalized}"
    assert result.error is None, f"Expected no error, got {result.error}"
    assert result.country_info is not None, f"Expected country_info"
    assert result.country_info.country == 'France', f"Expected France country"
    assert result.country_info.country_code == '33', f"Expected country code 33"

    # With explicit US country_code, should be rejected with helpful error
    result_us = phonehub.validate('+33123456789', 'US', detailed=True)
    assert result_us.valid is False, f"Expected valid=False with US country_code, got {result_us.valid}"
    assert 'France' in result_us.error, f"Expected France in error message, got {result_us.error}"
    assert result_us.country_info is not None, f"Expected country_info in error response"
    assert result_us.country_info['country'] == 'France', f"Expected France in country_info"


@th.django_unit_test()
def test_validate_international_uk(opts):
    """Test validation accepts valid UK number with auto-detect"""
    result = phonehub.validate('+447700900123', detailed=True)

    assert result.valid is True, f"Expected valid=True"
    assert result.normalized == '+447700900123', f"Expected +447700900123"
    assert result.country_info.country == 'United Kingdom', f"Expected UK country"
    assert result.country_info.country_code == '44', f"Expected country code 44"


@th.django_unit_test()
def test_validate_international_germany(opts):
    """Test validation accepts valid German number with auto-detect"""
    result = phonehub.validate('+4915112345678', detailed=True)

    assert result.valid is True, f"Expected valid=True"
    assert result.normalized == '+4915112345678', f"Expected +4915112345678"
    assert result.country_info.country == 'Germany', f"Expected Germany country"


@th.django_unit_test()
def test_validate_international_japan(opts):
    """Test validation accepts valid Japanese number with auto-detect"""
    result = phonehub.validate('+81312345678', detailed=True)

    assert result.valid is True, f"Expected valid=True"
    assert result.normalized == '+81312345678', f"Expected +81312345678"
    assert result.country_info.country == 'Japan', f"Expected Japan"
    assert result.country_info.region == 'Asia-Pacific', f"Expected Asia-Pacific region"


@th.django_unit_test()
def test_validate_toll_free_numbers(opts):
    """Test validation accepts toll-free numbers"""
    toll_free_codes = ['800', '888', '877', '866', '855', '844', '833']

    for code in toll_free_codes:
        result = phonehub.validate(f'{code}5551234', detailed=True)
        assert result.valid is True, f"Expected {code} toll-free to be valid"
        assert result.area_code_info.type == 'toll_free', f"Expected toll_free type for {code}"


@th.django_unit_test()
def test_detect_country_us(opts):
    """Test detecting US/NANP numbers"""
    country = phonehub.detect_country('+14155551234')
    assert country is not None, f"Expected country info"
    assert country.country_code == '1', f"Expected country code 1"
    assert country.is_nanp is True, f"Expected NANP"
    assert 'USA' in country.country or 'Canada' in country.country, f"Expected USA/Canada in country name"


@th.django_unit_test()
def test_detect_country_uk(opts):
    """Test detecting UK numbers"""
    country = phonehub.detect_country('+447700900123')
    assert country is not None, f"Expected country info"
    assert country.country_code == '44', f"Expected country code 44"
    assert country.country == 'United Kingdom', f"Expected United Kingdom"
    assert country.is_nanp is False, f"Expected not NANP"


@th.django_unit_test()
def test_detect_country_france(opts):
    """Test detecting French numbers"""
    country = phonehub.detect_country('+33123456789')
    assert country is not None, f"Expected country info"
    assert country.country_code == '33', f"Expected country code 33"
    assert country.country == 'France', f"Expected France"
    assert country.region == 'Europe', f"Expected Europe region"


@th.django_unit_test()
def test_detect_country_formatted(opts):
    """Test detecting country from formatted numbers"""
    country = phonehub.detect_country('+1 (415) 555-1234')
    assert country is not None, f"Expected country info"
    assert country.country_code == '1', f"Expected country code 1"

    country = phonehub.detect_country('+44 77 0090 0123')
    assert country.country_code == '44', f"Expected country code 44"


@th.django_unit_test()
def test_detect_country_without_plus(opts):
    """Test detecting country from numbers without + (should still work)"""
    country = phonehub.detect_country('14155551234')
    assert country is not None, f"Expected country info even without +"
    assert country.country_code == '1', f"Expected country code 1"


@th.django_unit_test()
def test_detect_country_invalid(opts):
    """Test detect_country with invalid input"""
    assert phonehub.detect_country('') is None, f"Expected None for empty string"
    assert phonehub.detect_country(None) is None, f"Expected None for None"
    assert phonehub.detect_country('abc') is None, f"Expected None for non-numeric"


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
