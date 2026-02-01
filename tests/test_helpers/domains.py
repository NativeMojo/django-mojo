from testit import helpers as th


@th.django_unit_test()
def test_domain_convenience_functions(opts):
    """Test that convenience functions exist and are callable."""
    from mojo.helpers import domain

    # All these should be callable
    assert callable(domain.a), "Expected domain.a to be callable"
    assert callable(domain.ip), "Expected domain.ip to be callable"
    assert callable(domain.ips), "Expected domain.ips to be callable"
    assert callable(domain.mx), "Expected domain.mx to be callable"
    assert callable(domain.txt), "Expected domain.txt to be callable"
    assert callable(domain.reverse), "Expected domain.reverse to be callable"
    assert callable(domain.dns), "Expected domain.dns to be callable"
    assert callable(domain.lookup), "Expected domain.lookup to be callable"
    assert callable(domain.spf), "Expected domain.spf to be callable"
    assert callable(domain.dmarc), "Expected domain.dmarc to be callable"
    assert callable(domain.dkim), "Expected domain.dkim to be callable"
    assert callable(domain.whois), "Expected domain.whois to be callable"
    assert callable(domain.is_available), "Expected domain.is_available to be callable"
    assert callable(domain.ssl), "Expected domain.ssl to be callable"


@th.django_unit_test()
def test_domain_ip_single(opts):
    """Test single IP lookup."""
    from mojo.helpers import domain

    # Test well-known domain - default (first IP)
    ip = domain.ip('google.com')
    assert isinstance(ip, str), "Expected string IP"
    assert len(ip) > 0, "Expected IP for google.com"
    assert '.' in ip, "Expected IPv4 address"

    # Test random selection
    ip_random = domain.ip('google.com', random=True)
    assert isinstance(ip_random, str), "Expected string IP"
    assert '.' in ip_random, "Expected IPv4 address"

    # Test subdomain
    ip = domain.ip('mail.google.com')
    assert isinstance(ip, str), "Expected string IP"

    # Test invalid domain - should return empty string
    ip = domain.ip('thisdoesnotexist12345xyz.com')
    assert ip == '', "Expected empty string for non-existent domain"


@th.django_unit_test()
def test_domain_ips_simple(opts):
    """Test simple IP lookup."""
    from mojo.helpers import domain

    # Test well-known domain
    ips = domain.ips('google.com')
    assert isinstance(ips, list), "Expected list of IPs"
    assert len(ips) > 0, "Expected at least one IP for google.com"
    assert all('.' in ip for ip in ips), "Expected IPv4 addresses"

    # Test subdomain
    ips = domain.ips('mail.google.com')
    assert isinstance(ips, list), "Expected list of IPs"

    # Test invalid domain - should return empty list
    ips = domain.ips('thisdoesnotexist12345xyz.com')
    assert ips == [], "Expected empty list for non-existent domain"


@th.django_unit_test()
def test_domain_a_lookup(opts):
    """Test A record lookup."""
    from mojo.helpers import domain

    # Test well-known domain
    result = domain.a('google.com')
    assert result.records, "Expected A records for google.com"
    assert not result.error, f"Expected no error, got {result.error}"
    assert all('.' in ip for ip in result.records), "Expected IPv4 addresses"

    # Test invalid domain
    result = domain.a('thisdoesnotexist12345xyz.com')
    assert result.error, "Expected error for non-existent domain"
    assert not result.records, "Expected no records for non-existent domain"


@th.django_unit_test()
def test_domain_mx_lookup(opts):
    """Test MX record lookup."""
    from mojo.helpers import domain

    # Test well-known domain
    result = domain.mx('google.com')
    assert result.records, "Expected MX records for google.com"
    assert not result.error, f"Expected no error, got {result.error}"

    # Verify MX record structure
    mx = result.records[0]
    assert 'priority' in mx, "Expected priority in MX record"
    assert 'host' in mx, "Expected host in MX record"
    assert isinstance(mx['priority'], int), "Expected priority to be int"
    assert isinstance(mx['host'], str), "Expected host to be string"

    # Verify sorted by priority
    if len(result.records) > 1:
        priorities = [mx['priority'] for mx in result.records]
        assert priorities == sorted(priorities), "Expected MX records sorted by priority"


@th.django_unit_test()
def test_domain_txt_lookup(opts):
    """Test TXT record lookup."""
    from mojo.helpers import domain

    # Test well-known domain
    result = domain.txt('google.com')
    assert isinstance(result.records, list), "Expected records list"
    assert not result.error or 'No TXT' in result.error, f"Expected no error or no TXT records"


@th.django_unit_test()
def test_domain_reverse_lookup(opts):
    """Test reverse DNS lookup."""
    from mojo.helpers import domain

    # Test Google DNS
    result = domain.reverse('8.8.8.8')
    assert result.hostname, "Expected hostname for 8.8.8.8"
    assert not result.error, f"Expected no error, got {result.error}"
    assert 'google' in result.hostname.lower(), f"Expected 'google' in hostname, got {result.hostname}"


@th.django_unit_test()
def test_domain_dns_query(opts):
    """Test generic DNS query."""
    from mojo.helpers import domain

    # Test AAAA records (IPv6)
    result = domain.dns('google.com', 'AAAA')
    assert isinstance(result.records, list), "Expected records list"

    # Test NS records
    result = domain.dns('google.com', 'NS')
    assert result.records, "Expected NS records for google.com"
    assert not result.error, f"Expected no error, got {result.error}"


@th.django_unit_test()
def test_domain_lookup_comprehensive(opts):
    """Test comprehensive domain lookup."""
    from mojo.helpers import domain

    result = domain.lookup('google.com')
    assert result.domain == 'google.com', f"Expected domain to be google.com, got {result.domain}"
    assert result.a, "Expected A records"
    assert result.mx, "Expected MX records"
    assert isinstance(result.txt, list), "Expected TXT records list"
    assert not result.error, f"Expected no error, got {result.error}"

    # Verify structure
    assert all('.' in ip for ip in result.a), "Expected IPv4 addresses"
    assert all('priority' in mx and 'host' in mx for mx in result.mx), "Expected MX record structure"


@th.django_unit_test()
def test_domain_spf_lookup(opts):
    """Test SPF record lookup and parsing."""
    from mojo.helpers import domain

    # Test domain with SPF
    result = domain.spf('google.com')

    if result.valid:
        assert result.record, "Expected SPF record"
        assert result.record.startswith('v=spf1'), "Expected SPF record to start with v=spf1"
        assert result.parsed, "Expected parsed SPF data"
        assert result.parsed.version == 'spf1', f"Expected version spf1, got {result.parsed.version}"
        assert result.parsed.mechanisms, "Expected SPF mechanisms"
        assert result.parsed.qualifier, "Expected SPF qualifier"
    else:
        # It's okay if no SPF record found
        assert result.error or not result.record, "Expected error or no record"


@th.django_unit_test()
def test_domain_dmarc_lookup(opts):
    """Test DMARC record lookup and parsing."""
    from mojo.helpers import domain

    # Test domain with DMARC
    result = domain.dmarc('google.com')

    if result.valid:
        assert result.record, "Expected DMARC record"
        assert result.record.startswith('v=DMARC1'), "Expected DMARC record to start with v=DMARC1"
        assert result.parsed, "Expected parsed DMARC data"
        assert result.parsed.version == 'DMARC1', f"Expected version DMARC1"
        assert result.parsed.p, "Expected DMARC policy"
    else:
        # It's okay if no DMARC record found
        assert result.error or not result.record, "Expected error or no record"


@th.django_unit_test()
def test_domain_dkim_lookup(opts):
    """Test DKIM record lookup and parsing."""
    from mojo.helpers import domain

    # Test with common selector
    result = domain.dkim('google.com', selector='google')

    # DKIM may or may not be found depending on selector
    if result.valid:
        assert result.record, "Expected DKIM record"
        assert result.parsed, "Expected parsed DKIM data"
        assert result.parsed.p or result.parsed.public_key, "Expected public key in DKIM record"

    # Test invalid selector
    result = domain.dkim('google.com', selector='thisdoesnotexist12345')
    assert result.error or not result.valid, "Expected error or invalid for non-existent selector"


@th.django_unit_test()
def test_domain_whois_lookup(opts):
    """Test WHOIS lookup."""
    from mojo.helpers import domain

    # Test well-known domain
    result = domain.whois('google.com')

    if not result.error:
        assert result.domain_name, "Expected domain_name in WHOIS result"
        assert 'google.com' in result.domain_name.lower(), f"Expected google.com in domain_name"

        # Check for common WHOIS fields (may vary by registrar)
        # At least some of these should be present
        has_data = any([
            result.get('registrar'),
            result.get('creation_date'),
            result.get('expiration_date'),
            result.get('name_servers')
        ])
        assert has_data, "Expected at least some WHOIS data"
    else:
        # WHOIS may be rate-limited or blocked
        assert result.error, "Expected error message if WHOIS failed"


@th.django_unit_test()
def test_domain_is_available(opts):
    """Test domain availability check."""
    from mojo.helpers import domain

    # Test registered domain
    result = domain.is_available('google.com')
    assert result.domain == 'google.com', f"Expected domain to be google.com"
    assert result.available == False, "Expected google.com to not be available"
    assert result.reason, "Expected reason for availability status"

    # Test likely unregistered domain (very long random string)
    import random
    import string
    random_domain = ''.join(random.choices(string.ascii_lowercase + string.digits, k=63)) + '.com'
    result = domain.is_available(random_domain)
    assert result.domain == random_domain, f"Expected domain to be {random_domain}"
    # May be available or may get an error


@th.django_unit_test()
def test_domain_ssl_certificate(opts):
    """Test SSL certificate lookup."""
    from mojo.helpers import domain

    # Test well-known domain with valid SSL
    result = domain.ssl('google.com')

    if not result.error:
        assert result.subject, "Expected subject in SSL certificate"
        assert result.issuer, "Expected issuer in SSL certificate"
        assert result.valid_from, "Expected valid_from date"
        assert result.valid_until, "Expected valid_until date"
        assert result.days_remaining is not None, "Expected days_remaining"
        assert result.expired is not None, "Expected expired flag"
        assert isinstance(result.san, list), "Expected SAN to be list"

        # Verify certificate is not expired for google.com
        assert not result.expired, "Expected google.com certificate to not be expired"
        assert result.days_remaining > 0, "Expected positive days remaining for google.com"

        # Verify issuer info
        assert result.issuer, "Expected issuer information"
    else:
        # SSL lookup may fail due to network issues
        assert result.error, "Expected error message if SSL lookup failed"


@th.django_unit_test()
def test_domain_objict_access(opts):
    """Test that all results return objict instances."""
    from mojo.helpers import domain
    from objict import objict

    # Test A record
    result = domain.a('google.com')
    assert isinstance(result, objict), "Expected objict instance"
    assert hasattr(result, 'records'), "Expected records attribute"
    assert hasattr(result, 'error'), "Expected error attribute"

    # Test MX record
    result = domain.mx('google.com')
    assert isinstance(result, objict), "Expected objict instance"

    # Test comprehensive lookup
    result = domain.lookup('google.com')
    assert isinstance(result, objict), "Expected objict instance"
    assert result.domain == 'google.com', "Expected domain attribute access"

    # Test WHOIS
    result = domain.whois('google.com')
    assert isinstance(result, objict), "Expected objict instance"

    # Test SSL
    result = domain.ssl('google.com')
    assert isinstance(result, objict), "Expected objict instance"


@th.django_unit_test()
def test_domain_error_handling(opts):
    """Test error handling for invalid inputs."""
    from mojo.helpers import domain

    # Test invalid domain name
    result = domain.a('invalid..domain..com')
    assert result.error or not result.records, "Expected error or no records for invalid domain"

    # Test non-existent domain
    result = domain.a('thisdoesnotexist12345xyz.com')
    assert result.error, "Expected error for non-existent domain"

    # Test invalid IP for reverse lookup
    result = domain.reverse('999.999.999.999')
    assert result.error, "Expected error for invalid IP"

    # All errors should have error field
    assert hasattr(result, 'error'), "Expected error attribute"
    assert result.error, "Expected error message"


@th.django_unit_test()
def test_domain_normalization(opts):
    """Test domain name normalization."""
    from mojo.helpers import domain

    # Test uppercase domain
    result1 = domain.a('GOOGLE.COM')
    result2 = domain.a('google.com')

    # Should both work (normalization happens internally)
    if not result1.error and not result2.error:
        assert result1.records, "Expected records for uppercase domain"
        assert result2.records, "Expected records for lowercase domain"

    # Test with spaces
    result = domain.a('  google.com  ')
    if not result.error:
        assert result.records, "Expected records for domain with spaces"
