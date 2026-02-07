"""
Domain Helper - DNS, WHOIS, and SSL certificate lookups

Provides utilities for domain information gathering including:
- DNS record lookups (A, MX, TXT, etc.)
- WHOIS information
- SSL certificate details
- Email security records (SPF, DMARC, DKIM)

All functions return objict instances for convenient attribute access.

Dependencies:
    pip install dnspython python-whois cryptography

Example:
    from mojo.helpers import domain

    # DNS lookup
    result = domain.a('example.com')
    print(result.records)  # ['93.184.216.34']

    # WHOIS
    info = domain.whois('example.com')
    print(info.registrar)

    # SSL certificate
    cert = domain.ssl('example.com')
    print(f"Expires in {cert.days_remaining} days")
"""

import socket
import ssl as ssl_module
import re
import sys
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

try:
    import dns.resolver as dns_resolver
    import dns.reversename as dns_reversename
    DNS_AVAILABLE = True
except ImportError:
    DNS_AVAILABLE = False

try:
    import whois as whois_lib
    WHOIS_AVAILABLE = True
except ImportError:
    WHOIS_AVAILABLE = False

try:
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False

from objict import objict


# Known email provider patterns (MX hostname patterns -> provider info)
EMAIL_PROVIDERS = {
    # Major providers
    'google.com': {'provider': 'Gmail', 'type': 'personal', 'confidence': 'high'},
    'googlemail.com': {'provider': 'Gmail', 'type': 'personal', 'confidence': 'high'},
    'aspmx.l.google.com': {'provider': 'Google Workspace', 'type': 'business', 'confidence': 'high'},
    'outlook.com': {'provider': 'Outlook', 'type': 'personal', 'confidence': 'high'},
    'hotmail.com': {'provider': 'Outlook', 'type': 'personal', 'confidence': 'high'},
    'protection.outlook.com': {'provider': 'Microsoft 365', 'type': 'business', 'confidence': 'high'},
    'yahoodns.net': {'provider': 'Yahoo', 'type': 'personal', 'confidence': 'high'},
    'protonmail.ch': {'provider': 'ProtonMail', 'type': 'personal', 'confidence': 'high'},
    'mail.protonmail.ch': {'provider': 'ProtonMail', 'type': 'personal', 'confidence': 'high'},
    'messagingengine.com': {'provider': 'FastMail', 'type': 'business', 'confidence': 'high'},
    'fastmail.com': {'provider': 'FastMail', 'type': 'business', 'confidence': 'high'},
    'zoho.com': {'provider': 'Zoho', 'type': 'business', 'confidence': 'high'},
    'zoho.eu': {'provider': 'Zoho', 'type': 'business', 'confidence': 'high'},
    'mail.me.com': {'provider': 'iCloud', 'type': 'personal', 'confidence': 'high'},
    'icloud.com': {'provider': 'iCloud', 'type': 'personal', 'confidence': 'high'},
    'mimecast.com': {'provider': 'Mimecast', 'type': 'security', 'confidence': 'high'},
    'pphosted.com': {'provider': 'Proofpoint', 'type': 'security', 'confidence': 'high'},
    'barracudanetworks.com': {'provider': 'Barracuda', 'type': 'security', 'confidence': 'high'},
    
    # Transactional email services
    'amazonses.com': {'provider': 'AWS SES', 'type': 'transactional', 'confidence': 'high'},
    'sendgrid.net': {'provider': 'SendGrid', 'type': 'transactional', 'confidence': 'high'},
    'mailgun.org': {'provider': 'Mailgun', 'type': 'transactional', 'confidence': 'high'},
    'mandrillapp.com': {'provider': 'Mandrill', 'type': 'transactional', 'confidence': 'high'},
    'sparkpostmail.com': {'provider': 'SparkPost', 'type': 'transactional', 'confidence': 'high'},
    
    # Educational
    'edu': {'provider': 'Educational Institution', 'type': 'education', 'confidence': 'medium'},
    
    # Common disposable email providers
    'mailinator.com': {'provider': 'Mailinator', 'type': 'disposable', 'confidence': 'high', 'is_disposable': True},
    'guerrillamail.com': {'provider': 'Guerrilla Mail', 'type': 'disposable', 'confidence': 'high', 'is_disposable': True},
    'tempmail.com': {'provider': 'TempMail', 'type': 'disposable', 'confidence': 'high', 'is_disposable': True},
    '10minutemail.com': {'provider': '10 Minute Mail', 'type': 'disposable', 'confidence': 'high', 'is_disposable': True},
    'throwaway.email': {'provider': 'Throwaway Email', 'type': 'disposable', 'confidence': 'high', 'is_disposable': True},
}


@contextmanager
def suppress_stderr():
    """Context manager to suppress stderr output."""
    null_fd = os.open(os.devnull, os.O_RDWR)
    old_stderr = os.dup(2)
    try:
        os.dup2(null_fd, 2)
        yield
    finally:
        os.dup2(old_stderr, 2)
        os.close(null_fd)
        os.close(old_stderr)


def _make_safe(obj: Any) -> Any:
    """
    Recursively convert datetime objects to epoch timestamps for JSON serialization.
    
    Args:
        obj: Object to make safe (objict, dict, list, or primitive)
        
    Returns:
        JSON-safe version of the object with datetime objects converted to epoch timestamps
    """
    if isinstance(obj, datetime):
        # Convert to epoch timestamp (seconds since 1970-01-01 UTC)
        return int(obj.timestamp())
    elif isinstance(obj, objict):
        return objict({k: _make_safe(v) for k, v in obj.items()})
    elif isinstance(obj, dict):
        return {k: _make_safe(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_make_safe(item) for item in obj]
    return obj


def _normalize_domain(domain: str) -> str:
    """Normalize domain name."""
    return domain.lower().strip()


def _dns_query(domain: str, record_type: str) -> objict:
    """Execute DNS query."""
    if not DNS_AVAILABLE:
        return objict(records=[], error="dnspython not installed")

    try:
        domain = _normalize_domain(domain)
        answers = dns_resolver.resolve(domain, record_type)
        records = [str(rdata) for rdata in answers]
        return objict(records=records, error=None)
    except dns_resolver.NXDOMAIN:
        return objict(records=[], error="NXDOMAIN: Domain does not exist")
    except dns_resolver.NoAnswer:
        return objict(records=[], error=f"No {record_type} records found")
    except dns_resolver.Timeout:
        return objict(records=[], error="DNS query timeout")
    except Exception as e:
        return objict(records=[], error=str(e))


def a(domain: str) -> objict:
    """
    Query A records (IPv4 addresses).

    Args:
        domain: Domain name to query

    Returns:
        objict with records list and error field

    Example:
        result = domain.a('example.com')
        print(result.records)  # ['93.184.216.34']
    """
    return _dns_query(domain, 'A')


def ips(domain: str) -> list:
    """
    Get IP addresses for a domain (simple list).

    Args:
        domain: Domain name to query

    Returns:
        List of IP addresses (empty list if error)

    Example:
        ips = domain.ips('example.com')
        print(ips)  # ['93.184.216.34']
    """
    result = _dns_query(domain, 'A')
    return result.records if not result.error else []


def ip(domain: str, random: bool = False) -> str:
    """
    Get a single IP address for a domain.

    Args:
        domain: Domain name to query
        random: If True, randomly select from multiple IPs (default: False, returns first)

    Returns:
        Single IP address (empty string if error)

    Example:
        ip = domain.ip('example.com')
        print(ip)  # '93.184.216.34' (first IP)

        ip = domain.ip('example.com', random=True)
        print(ip)  # Random IP from available IPs
    """
    result = _dns_query(domain, 'A')
    if result.error or not result.records:
        return ''

    if random and len(result.records) > 1:
        import random as rand_module
        return rand_module.choice(result.records)

    return result.records[0]


def mx(domain: str) -> objict:
    """
    Query MX records (mail servers).

    Args:
        domain: Domain name to query

    Returns:
        objict with records list (dicts with priority and host) and error field

    Example:
        result = domain.mx('example.com')
        for mx in result.records:
            print(f"{mx['priority']}: {mx['host']}")
    """
    if not DNS_AVAILABLE:
        return objict(records=[], error="dnspython not installed")

    try:
        domain = _normalize_domain(domain)
        answers = dns_resolver.resolve(domain, 'MX')
        records = [
            {'priority': int(rdata.preference), 'host': str(rdata.exchange).rstrip('.')}
            for rdata in answers
        ]
        # Sort by priority
        records.sort(key=lambda x: x['priority'])
        return objict(records=records, error=None)
    except dns_resolver.NXDOMAIN:
        return objict(records=[], error="NXDOMAIN: Domain does not exist")
    except dns_resolver.NoAnswer:
        return objict(records=[], error="No MX records found")
    except dns_resolver.Timeout:
        return objict(records=[], error="DNS query timeout")
    except Exception as e:
        return objict(records=[], error=str(e))


def txt(domain: str) -> objict:
    """
    Query TXT records.

    Args:
        domain: Domain name to query

    Returns:
        objict with records list and error field

    Example:
        result = domain.txt('example.com')
        for record in result.records:
            print(record)
    """
    if not DNS_AVAILABLE:
        return objict(records=[], error="dnspython not installed")

    try:
        domain = _normalize_domain(domain)
        answers = dns_resolver.resolve(domain, 'TXT')
        records = []
        for rdata in answers:
            # TXT records can have multiple strings
            txt_strings = [s.decode() if isinstance(s, bytes) else s for s in rdata.strings]
            records.append(''.join(txt_strings))
        return objict(records=records, error=None)
    except dns_resolver.NXDOMAIN:
        return objict(records=[], error="NXDOMAIN: Domain does not exist")
    except dns_resolver.NoAnswer:
        return objict(records=[], error="No TXT records found")
    except dns_resolver.Timeout:
        return objict(records=[], error="DNS query timeout")
    except Exception as e:
        return objict(records=[], error=str(e))


def reverse(ip: str) -> objict:
    """
    Reverse DNS lookup (PTR record).

    Args:
        ip: IP address to lookup

    Returns:
        objict with hostname and error field

    Example:
        result = domain.reverse('8.8.8.8')
        print(result.hostname)  # 'dns.google'
    """
    if not DNS_AVAILABLE:
        return objict(hostname=None, error="dnspython not installed")

    try:
        addr = dns_reversename.from_address(ip)
        answers = dns_resolver.resolve(addr, 'PTR')
        hostname = str(answers[0]).rstrip('.')
        return objict(hostname=hostname, error=None)
    except dns_resolver.NXDOMAIN:
        return objict(hostname=None, error="No PTR record found")
    except dns_resolver.NoAnswer:
        return objict(hostname=None, error="No PTR record found")
    except dns_resolver.Timeout:
        return objict(hostname=None, error="DNS query timeout")
    except Exception as e:
        return objict(hostname=None, error=str(e))


def dns(domain: str, record_type: str) -> objict:
    """
    Query any DNS record type.

    Args:
        domain: Domain name to query
        record_type: DNS record type (A, AAAA, CNAME, NS, etc.)

    Returns:
        objict with records list and error field

    Example:
        result = domain.dns('example.com', 'AAAA')
        print(result.records)  # IPv6 addresses
    """
    return _dns_query(domain, record_type.upper())


def lookup(domain: str) -> objict:
    """
    Comprehensive DNS lookup (A, CNAME, MX, TXT).

    Args:
        domain: Domain name to query

    Returns:
        objict with a, cname, mx, txt lists and error field

    Example:
        result = domain.lookup('example.com')
        print(result.a)      # ['93.184.216.34']
        print(result.cname)  # ['target.example.com'] or []
        print(result.mx)     # [{'priority': 0, 'host': '...'}]
        print(result.txt)    # ['v=spf1 ...']
    """
    domain = _normalize_domain(domain)

    # Get A records
    a_result = a(domain)
    a_records = a_result.records if not a_result.error else []

    # Get CNAME records
    cname_result = dns(domain, 'CNAME')
    cname_records = cname_result.records if not cname_result.error else []

    # Get MX records
    mx_result = mx(domain)
    mx_records = mx_result.records if not mx_result.error else []

    # Get TXT records
    txt_result = txt(domain)
    txt_records = txt_result.records if not txt_result.error else []

    return objict(
        domain=domain,
        a=a_records,
        cname=cname_records,
        mx=mx_records,
        txt=txt_records,
        error=None
    )


def spf(domain: str) -> objict:
    """
    Query and parse SPF (Sender Policy Framework) records.

    Args:
        domain: Domain name to query

    Returns:
        objict with record, parsed dict, valid bool, and error field

    Example:
        result = domain.spf('example.com')
        if result.valid:
            print(result.parsed.mechanisms)
    """
    txt_result = txt(domain)
    if txt_result.error:
        return objict(record=None, parsed=None, valid=False, error=txt_result.error)

    # Find SPF record (starts with v=spf1)
    spf_record = None
    for record in txt_result.records:
        if record.startswith('v=spf1'):
            spf_record = record
            break

    if not spf_record:
        return objict(record=None, parsed=None, valid=False, error="No SPF record found")

    # Parse SPF record
    parts = spf_record.split()
    parsed = objict(
        version=parts[0].replace('v=', ''),
        mechanisms=[],
        qualifier=None
    )

    for part in parts[1:]:
        if part.startswith(('~all', '-all', '+all', '?all')):
            parsed.qualifier = part
        else:
            parsed.mechanisms.append(part)

    return objict(
        record=spf_record,
        parsed=parsed,
        valid=True,
        error=None
    )


def dmarc(domain: str) -> objict:
    """
    Query and parse DMARC records.

    Args:
        domain: Domain name to query

    Returns:
        objict with record, parsed dict, valid bool, and error field

    Example:
        result = domain.dmarc('example.com')
        if result.valid:
            print(result.parsed.policy)
    """
    # DMARC records are at _dmarc subdomain
    dmarc_domain = f"_dmarc.{_normalize_domain(domain)}"
    txt_result = txt(dmarc_domain)

    if txt_result.error:
        return objict(record=None, parsed=None, valid=False, error=txt_result.error)

    # Find DMARC record (starts with v=DMARC1)
    dmarc_record = None
    for record in txt_result.records:
        if record.startswith('v=DMARC1'):
            dmarc_record = record
            break

    if not dmarc_record:
        return objict(record=None, parsed=None, valid=False, error="No DMARC record found")

    # Parse DMARC record
    parsed = objict()
    parts = dmarc_record.split(';')

    for part in parts:
        part = part.strip()
        if '=' in part:
            key, value = part.split('=', 1)
            key = key.strip()
            value = value.strip()

            # Handle special cases
            if key in ('rua', 'ruf'):
                parsed[key] = [v.strip() for v in value.split(',')]
            else:
                parsed[key] = value

    # Set version if found
    if 'v' in parsed:
        parsed.version = parsed.v

    return objict(
        record=dmarc_record,
        parsed=parsed,
        valid=True,
        error=None
    )


def dkim(domain: str, selector: str = 'default') -> objict:
    """
    Query DKIM records for a specific selector.

    Args:
        domain: Domain name to query
        selector: DKIM selector (default: 'default')

    Returns:
        objict with record, parsed dict, valid bool, and error field

    Example:
        result = domain.dkim('example.com', selector='google')
        if result.valid:
            print(result.parsed.key_type)
    """
    # DKIM records are at <selector>._domainkey subdomain
    dkim_domain = f"{selector}._domainkey.{_normalize_domain(domain)}"
    txt_result = txt(dkim_domain)

    if txt_result.error:
        return objict(record=None, parsed=None, valid=False, error=txt_result.error)

    if not txt_result.records:
        return objict(record=None, parsed=None, valid=False, error="No DKIM record found")

    # Get first TXT record (should be DKIM)
    dkim_record = txt_result.records[0]

    # Parse DKIM record
    parsed = objict()
    parts = dkim_record.split(';')

    for part in parts:
        part = part.strip()
        if '=' in part:
            key, value = part.split('=', 1)
            key = key.strip()
            value = value.strip()
            parsed[key] = value

    # Map common fields
    if 'v' in parsed:
        parsed.version = parsed.v
    if 'k' in parsed:
        parsed.key_type = parsed.k
    if 'p' in parsed:
        parsed.public_key = parsed.p

    return objict(
        record=dkim_record,
        parsed=parsed,
        valid=bool(parsed.get('p')),
        error=None
    )


def whois(domain: str, safe: bool = False) -> objict:
    """
    Query WHOIS information for a domain.

    Args:
        domain: Domain name to query
        safe: If True, convert datetime objects to epoch timestamps for JSON serialization

    Returns:
        objict with domain information and error field

    Example:
        result = domain.whois('example.com')
        print(result.registrar)
        print(result.expiration_date)  # datetime object
        
        # For JSON serialization (datetime -> epoch timestamp)
        result = domain.whois('example.com', safe=True)
        print(result.expiration_date)  # 1723507199 (epoch timestamp)
        json.dumps(result)  # Works without errors
    """
    if not WHOIS_AVAILABLE:
        return objict(error="python-whois not installed")

    try:
        domain = _normalize_domain(domain)
        # Suppress stderr to avoid "Error trying to connect to socket" messages
        with suppress_stderr():
            w = whois_lib.whois(domain)

        # Build result objict
        result = objict(error=None)

        # Handle different response formats
        if isinstance(w, dict):
            result.update(w)
        else:
            # Extract all non-private fields from WHOIS object
            for field in dir(w):
                if not field.startswith('_') and field not in ['text', 'dayfirst', 'yearfirst', 'domain', 'regex']:
                    if hasattr(w, field):
                        value = getattr(w, field)
                        if value is not None and not callable(value):
                            # Normalize single-item lists to scalar
                            if isinstance(value, list) and len(value) == 1:
                                value = value[0]
                            result[field] = value

        # Normalize domain_name to string if it's a list
        if 'domain_name' in result and isinstance(result.domain_name, list):
            result.domain_name = result.domain_name[0] if result.domain_name else None
        
        # Normalize datetime lists to single values (WHOIS servers return multiple timestamps)
        # Keep the first one as it's usually the most accurate
        for date_field in ['creation_date', 'expiration_date', 'updated_date']:
            if date_field in result and isinstance(result[date_field], list):
                result[date_field] = result[date_field][0] if result[date_field] else None
        
        # Normalize registrar_url to prefer HTTPS (often returns both http and https)
        if 'registrar_url' in result and isinstance(result.registrar_url, list):
            urls = result.registrar_url
            # Prefer HTTPS over HTTP
            https_urls = [url for url in urls if url and url.startswith('https://')]
            http_urls = [url for url in urls if url and url.startswith('http://')]
            result.registrar_url = https_urls[0] if https_urls else (http_urls[0] if http_urls else None)
        
        # Consolidate address-related fields into clean address object
        # Check for any address-related field (different TLDs use different field names)
        address_fields = ['address', 'city', 'state', 'country', 'registrant_postal_code',
                         'registrant_state_province', 'registrant_country', 'registrant_city',
                         'registrant_street', 'registrant_address']
        
        if any(field in result for field in address_fields):
            # Parse address list if it exists
            address_list = result.get('address') or result.get('registrant_address')
            if isinstance(address_list, list) and len(address_list) > 0:
                # Address is a list with label and street
                label = address_list[0] if len(address_list) > 0 else None
                street = address_list[1] if len(address_list) > 1 else None
            else:
                # Use registrant_name or name as label, address as street if it's a string
                label = result.get('registrant_name') or result.get('name')
                street = address_list if address_list and not isinstance(address_list, list) else result.get('registrant_street')
            
            # Group all address fields together (handle different field name variations)
            address_obj = objict(
                label=label,
                street=street,
                city=result.get('city') or result.get('registrant_city'),
                state=result.get('state') or result.get('registrant_state_province'),
                postal_code=result.get('registrant_postal_code') or result.get('registrant_postal'),
                country=result.get('country') or result.get('registrant_country')
            )
            
            # Remove redundant top-level fields (must do this before assigning address_obj)
            fields_to_remove = ['address', 'city', 'state', 'registrant_postal_code', 'country',
                               'registrant_state_province', 'registrant_country', 'registrant_city',
                               'registrant_street', 'registrant_address', 'registrant_postal']
            for field in fields_to_remove:
                if field in result:
                    del result[field]
            
            # Now assign the consolidated address object
            result.address = address_obj
        
        # Parse status field into clean dict with boolean flags
        if 'status' in result and isinstance(result.status, list):
            status_raw = result.status
            status_dict = objict(
                raw=status_raw,  # Keep original for reference
                delete_prohibited=False,
                transfer_prohibited=False,
                update_prohibited=False,
                renew_prohibited=False,
                hold=False,
                locked=False,
                auto_renew_period=False,
                redemption_period=False,
                pending_delete=False,
                pending_transfer=False,
                ok=False
            )
            
            # Parse each status string
            for status_str in status_raw:
                status_lower = status_str.lower()
                if 'clientdeleteprohibited' in status_lower or 'serverdeleteprohibited' in status_lower:
                    status_dict.delete_prohibited = True
                if 'clienttransferprohibited' in status_lower or 'servertransferprohibited' in status_lower:
                    status_dict.transfer_prohibited = True
                if 'clientupdateprohibited' in status_lower or 'serverupdateprohibited' in status_lower:
                    status_dict.update_prohibited = True
                if 'clientrenewprohibited' in status_lower or 'serverrenewprohibited' in status_lower:
                    status_dict.renew_prohibited = True
                if 'clienthold' in status_lower or 'serverhold' in status_lower:
                    status_dict.hold = True
                if 'locked' in status_lower:
                    status_dict.locked = True
                if 'autorenewperiod' in status_lower:
                    status_dict.auto_renew_period = True
                if 'redemptionperiod' in status_lower:
                    status_dict.redemption_period = True
                if 'pendingdelete' in status_lower:
                    status_dict.pending_delete = True
                if 'pendingtransfer' in status_lower:
                    status_dict.pending_transfer = True
                if status_lower.strip() == 'ok':
                    status_dict.ok = True
            
            result.status = status_dict

        # Make safe for JSON serialization if requested
        if safe:
            result = _make_safe(result)

        return result

    except Exception as e:
        return objict(error=str(e))


def is_available(domain: str) -> objict:
    """
    Check if a domain is available for registration.

    Args:
        domain: Domain name to check

    Returns:
        objict with domain, available bool, reason, and error field

    Example:
        result = domain.is_available('example.com')
        if result.available:
            print("Domain is available!")
    """
    whois_result = whois(domain)

    if whois_result.error:
        # Check if error indicates domain doesn't exist
        error_lower = whois_result.error.lower()
        if any(x in error_lower for x in ['no match', 'not found', 'no entries found', 'available']):
            return objict(
                domain=domain,
                available=True,
                reason='Domain appears to be available',
                error=None
            )
        return objict(
            domain=domain,
            available=None,
            reason='Unable to determine availability',
            error=whois_result.error
        )

    # If we got WHOIS data, domain is registered
    return objict(
        domain=domain,
        available=False,
        reason='Domain is registered',
        error=None
    )


def ssl(domain: str, port: int = 443, safe: bool = False) -> objict:
    """
    Get SSL certificate information.

    Args:
        domain: Domain name to check
        port: Port number (default: 443)
        safe: If True, convert datetime objects to epoch timestamps for JSON serialization

    Returns:
        objict with certificate information and error field

    Example:
        result = domain.ssl('example.com')
        print(f"Expires in {result.days_remaining} days")
        print(f"Issuer: {result.issuer.CN}")
        print(result.valid_until)  # datetime object
        
        # For JSON serialization (datetime -> epoch timestamp)
        result = domain.ssl('example.com', safe=True)
        print(result.valid_until)  # 1723507199 (epoch timestamp)
        json.dumps(result)  # Works without errors
    """
    if not CRYPTOGRAPHY_AVAILABLE:
        return objict(error="cryptography not installed")

    try:
        domain = _normalize_domain(domain)

        # Create SSL context
        context = ssl_module.create_default_context()

        # Connect and get certificate + TLS info
        with socket.create_connection((domain, port), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as ssock:
                cert_der = ssock.getpeercert(binary_form=True)
                
                # Get TLS version and cipher info
                tls_version = ssock.version()
                cipher_info = ssock.cipher()  # (name, protocol_version, bits)

        # Parse certificate
        cert = x509.load_der_x509_certificate(cert_der, default_backend())

        # Extract subject
        subject = {}
        for attr in cert.subject:
            subject[attr.oid._name] = attr.value

        # Extract issuer
        issuer = {}
        for attr in cert.issuer:
            issuer[attr.oid._name] = attr.value

        # Get SAN (Subject Alternative Names)
        san = []
        try:
            san_ext = cert.extensions.get_extension_for_oid(
                x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME
            )
            san = [name.value for name in san_ext.value]
        except x509.ExtensionNotFound:
            pass

        # Calculate days remaining
        now = datetime.now(timezone.utc)
        not_after = cert.not_valid_after_utc if hasattr(cert, 'not_valid_after_utc') else cert.not_valid_after.replace(tzinfo=timezone.utc)
        not_before = cert.not_valid_before_utc if hasattr(cert, 'not_valid_before_utc') else cert.not_valid_before.replace(tzinfo=timezone.utc)
        days_remaining = (not_after - now).days
        expired = now > not_after

        # Get fingerprint
        fingerprint = ':'.join([f'{b:02X}' for b in cert.fingerprint(cert.signature_hash_algorithm)])
        
        # Get signature algorithm
        signature_algorithm = cert.signature_algorithm_oid._name
        
        # Get key size
        public_key = cert.public_key()
        key_size = None
        key_type = None
        try:
            from cryptography.hazmat.primitives.asymmetric import rsa, ec, ed25519, ed448
            if isinstance(public_key, rsa.RSAPublicKey):
                key_size = public_key.key_size
                key_type = 'RSA'
            elif isinstance(public_key, ec.EllipticCurvePublicKey):
                key_size = public_key.curve.key_size
                key_type = f'EC-{public_key.curve.name}'
            elif isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
                key_type = 'EdDSA'
        except:
            pass

        result = objict(
            subject=subject,
            issuer=issuer,
            serial_number=hex(cert.serial_number)[2:].upper(),
            version=cert.version.value,
            valid_from=not_before,
            valid_until=not_after,
            days_remaining=days_remaining,
            expired=expired,
            san=san,
            fingerprint=fingerprint,
            signature_algorithm=signature_algorithm,
            key_type=key_type,
            key_size=key_size,
            tls_version=tls_version,
            cipher_suite=cipher_info[0] if cipher_info else None,
            cipher_bits=cipher_info[2] if cipher_info else None,
            error=None
        )

        # Make safe for JSON serialization if requested
        if safe:
            result = _make_safe(result)

        return result

    except socket.timeout:
        return objict(error="Connection timeout")
    except socket.gaierror:
        return objict(error="Domain not found")
    except ssl_module.SSLError as e:
        return objict(error=f"SSL error: {str(e)}")
    except Exception as e:
        return objict(error=str(e))


def email_provider(domain: str) -> objict:
    """
    Detect email provider from domain by analyzing MX records.
    
    Args:
        domain: Email domain to check (e.g., "gmail.com" or "yourcompany.com")
        
    Returns:
        objict with provider information and error field
        
    Example:
        result = domain.email_provider('example.com')
        print(result.provider)          # "Google Workspace"
        print(result.type)              # "business"  
        print(result.custom_domain)     # True (using Google with custom domain)
        print(result.is_disposable)     # False
    """
    domain = _normalize_domain(domain)
    
    # Check if domain itself is a known provider (gmail.com, outlook.com, etc.)
    if domain in EMAIL_PROVIDERS:
        provider_info = EMAIL_PROVIDERS[domain].copy()
        return objict(
            provider=provider_info.get('provider', 'Unknown'),
            type=provider_info.get('type', 'unknown'),
            confidence=provider_info.get('confidence', 'high'),
            custom_domain=False,
            is_disposable=provider_info.get('is_disposable', False),
            is_corporate=False,
            mx_records=[],
            error=None
        )
    
    # Get MX records
    mx_result = mx(domain)
    if mx_result.error:
        return objict(
            provider='Unknown',
            type='unknown',
            confidence='none',
            custom_domain=False,
            is_disposable=False,
            is_corporate=False,
            mx_records=[],
            error=mx_result.error
        )
    
    if not mx_result.records:
        return objict(
            provider='Unknown',
            type='unknown',
            confidence='none',
            custom_domain=False,
            is_disposable=False,
            is_corporate=False,
            mx_records=[],
            error='No MX records found'
        )
    
    # Analyze MX records to detect provider
    mx_hosts = [mx_rec['host'].lower() for mx_rec in mx_result.records]
    
    # Check each MX hostname against known patterns
    detected_provider = None
    confidence = 'low'
    
    for mx_host in mx_hosts:
        # Check if any known provider pattern matches
        for pattern, provider_info in EMAIL_PROVIDERS.items():
            if pattern in mx_host:
                detected_provider = provider_info.copy()
                confidence = provider_info.get('confidence', 'medium')
                break
        if detected_provider:
            break
    
    # If no provider detected, it's likely a corporate/self-hosted email
    if not detected_provider:
        # Check if .edu domain for education
        if domain.endswith('.edu'):
            return objict(
                provider='Educational Institution',
                type='education',
                confidence='medium',
                custom_domain=True,
                is_disposable=False,
                is_corporate=False,
                mx_records=mx_result.records,
                error=None
            )
        
        # Unknown/corporate email
        return objict(
            provider='Self-Hosted / Corporate',
            type='corporate',
            confidence='medium',
            custom_domain=True,
            is_disposable=False,
            is_corporate=True,
            mx_records=mx_result.records,
            error=None
        )
    
    # Provider detected via MX records
    return objict(
        provider=detected_provider.get('provider', 'Unknown'),
        type=detected_provider.get('type', 'unknown'),
        confidence=confidence,
        custom_domain=True,  # Using provider but with custom domain
        is_disposable=detected_provider.get('is_disposable', False),
        is_corporate=False,
        mx_records=mx_result.records,
        error=None
    )


def email_security(domain: str, dkim_selectors: list = None) -> objict:
    """
    Comprehensive email security check (SPF, DMARC, DKIM).
    
    Checks all email authentication records and returns a summary with
    overall security score and specific findings.
    
    Args:
        domain: Domain name to check
        dkim_selectors: List of DKIM selectors to try (default: common selectors)
        
    Returns:
        objict with security summary and error field
        
    Example:
        result = domain.email_security('example.com')
        print(f"Security Score: {result.score}/100")
        print(f"SPF: {result.spf.status}")
        print(f"DMARC: {result.dmarc.status}")
        print(f"DKIM: {result.dkim.status}")
        
        # Check recommendations
        for rec in result.recommendations:
            print(f"- {rec}")
    """
    domain = _normalize_domain(domain)
    
    # Default DKIM selectors to try
    if dkim_selectors is None:
        dkim_selectors = ['default', 'google', 'k1', 's1', 'selector1', 'selector2', 
                          'dkim', 'mail', 'email', 'mx']
    
    # Initialize result
    result = objict(
        domain=domain,
        spf=objict(status='not_configured', valid=False, record=None),
        dmarc=objict(status='not_configured', valid=False, record=None, policy=None),
        dkim=objict(status='not_configured', valid=False, record=None, selector=None),
        score=0,
        security_level='poor',
        recommendations=[],
        error=None
    )
    
    # Check SPF
    spf_result = spf(domain)
    if spf_result.valid:
        result.spf.status = 'configured'
        result.spf.valid = True
        result.spf.record = spf_result.record
        result.score += 30
        
        # Check for common SPF issues
        if '~all' in spf_result.record:
            result.spf.qualifier = 'softfail'
        elif '-all' in spf_result.record:
            result.spf.qualifier = 'fail'
        elif '+all' in spf_result.record:
            result.spf.qualifier = 'pass'
            result.recommendations.append('SPF record uses +all which is insecure (allows anyone to send)')
        elif '?all' in spf_result.record:
            result.spf.qualifier = 'neutral'
    else:
        result.recommendations.append('Configure SPF record to prevent email spoofing')
    
    # Check DMARC
    dmarc_result = dmarc(domain)
    if dmarc_result.valid:
        result.dmarc.status = 'configured'
        result.dmarc.valid = True
        result.dmarc.record = dmarc_result.record
        result.dmarc.policy = dmarc_result.parsed.get('p', 'none')
        result.score += 40
        
        # Check DMARC policy strength
        policy = result.dmarc.policy
        if policy == 'reject':
            result.dmarc.policy_strength = 'strong'
        elif policy == 'quarantine':
            result.dmarc.policy_strength = 'moderate'
            result.recommendations.append('Consider upgrading DMARC policy from quarantine to reject')
        elif policy == 'none':
            result.dmarc.policy_strength = 'weak'
            result.recommendations.append('DMARC policy is set to none (monitoring only)')
        
        # Check for reporting
        if 'rua' not in dmarc_result.parsed:
            result.recommendations.append('Add DMARC aggregate reporting (rua) to monitor email authentication')
    else:
        result.recommendations.append('Configure DMARC record for email authentication and reporting')
    
    # Check DKIM (try multiple selectors)
    dkim_found = False
    for selector in dkim_selectors:
        dkim_result = dkim(domain, selector=selector)
        if dkim_result.valid:
            result.dkim.status = 'configured'
            result.dkim.valid = True
            result.dkim.record = dkim_result.record
            result.dkim.selector = selector
            result.score += 30
            dkim_found = True
            break
    
    if not dkim_found:
        result.recommendations.append(f'Configure DKIM signing (tried selectors: {", ".join(dkim_selectors[:5])}...)')
    
    # Determine security level based on score
    if result.score >= 90:
        result.security_level = 'excellent'
    elif result.score >= 70:
        result.security_level = 'good'
    elif result.score >= 40:
        result.security_level = 'fair'
    elif result.score >= 20:
        result.security_level = 'poor'
    else:
        result.security_level = 'critical'
    
    # Add summary
    result.summary = f"{result.security_level.title()} email security ({result.score}/100)"
    
    # Add overall recommendation
    if result.score < 100:
        configured = []
        if result.spf.valid:
            configured.append('SPF')
        if result.dmarc.valid:
            configured.append('DMARC')
        if result.dkim.valid:
            configured.append('DKIM')
        
        missing = []
        if not result.spf.valid:
            missing.append('SPF')
        if not result.dmarc.valid:
            missing.append('DMARC')
        if not result.dkim.valid:
            missing.append('DKIM')
        
        if missing:
            result.recommendations.insert(0, f"Missing: {', '.join(missing)}")
    
    return result


def ssl_security(domain: str, port: int = 443) -> objict:
    """
    Comprehensive SSL/TLS security check with scoring and recommendations.
    
    Analyzes certificate validity, TLS version, cipher strength, key size,
    and provides an overall security score with actionable recommendations.
    
    Args:
        domain: Domain name to check
        port: Port number (default: 443)
        
    Returns:
        objict with security summary and error field
        
    Example:
        result = domain.ssl_security('example.com')
        print(f"Security Score: {result.score}/100")
        print(f"TLS Version: {result.tls_version}")
        print(f"Certificate: {result.certificate.status}")
        
        # Check recommendations
        for rec in result.recommendations:
            print(f"- {rec}")
    """
    domain = _normalize_domain(domain)
    
    # Get SSL certificate info
    ssl_result = ssl(domain, port)
    
    if ssl_result.error:
        return objict(
            domain=domain,
            score=0,
            security_level='critical',
            error=ssl_result.error,
            recommendations=[f'Unable to establish SSL connection: {ssl_result.error}']
        )
    
    # Initialize result
    result = objict(
        domain=domain,
        port=port,
        certificate=objict(status='valid', valid=True, days_remaining=ssl_result.days_remaining, expired=ssl_result.expired),
        tls_version=ssl_result.tls_version,
        cipher_suite=ssl_result.cipher_suite,
        cipher_bits=ssl_result.cipher_bits,
        key_type=ssl_result.key_type,
        key_size=ssl_result.key_size,
        signature_algorithm=ssl_result.signature_algorithm,
        score=0,
        security_level='poor',
        recommendations=[],
        error=None
    )
    
    # Check certificate validity and expiration
    if ssl_result.expired:
        result.certificate.status = 'expired'
        result.certificate.valid = False
        result.recommendations.append('Certificate has expired - immediate renewal required')
    elif ssl_result.days_remaining < 7:
        result.certificate.status = 'expiring_soon'
        result.recommendations.append(f'Certificate expires in {ssl_result.days_remaining} days - renew immediately')
        result.score += 10
    elif ssl_result.days_remaining < 30:
        result.certificate.status = 'expiring'
        result.recommendations.append(f'Certificate expires in {ssl_result.days_remaining} days - schedule renewal')
        result.score += 20
    else:
        result.score += 25
    
    # Check TLS version (TLSv1.3 = 25pts, TLSv1.2 = 20pts, older = 0pts)
    tls_version = ssl_result.tls_version
    if tls_version == 'TLSv1.3':
        result.tls_security = 'excellent'
        result.score += 25
    elif tls_version == 'TLSv1.2':
        result.tls_security = 'good'
        result.score += 20
        result.recommendations.append('Consider upgrading to TLS 1.3 for better security and performance')
    elif tls_version in ['TLSv1.1', 'TLSv1']:
        result.tls_security = 'weak'
        result.score += 5
        result.recommendations.append(f'Upgrade from {tls_version} to TLS 1.2 or 1.3 (older versions are deprecated)')
    else:
        result.tls_security = 'insecure'
        result.recommendations.append(f'Insecure TLS version: {tls_version} - upgrade immediately')
    
    # Check cipher strength (>= 256 bits = 20pts, >= 128 bits = 15pts, < 128 = 0pts)
    cipher_bits = ssl_result.cipher_bits
    if cipher_bits and cipher_bits >= 256:
        result.cipher_security = 'strong'
        result.score += 20
    elif cipher_bits and cipher_bits >= 128:
        result.cipher_security = 'adequate'
        result.score += 15
        result.recommendations.append('Consider using 256-bit ciphers for better security')
    elif cipher_bits:
        result.cipher_security = 'weak'
        result.recommendations.append(f'Weak cipher strength: {cipher_bits} bits - upgrade cipher suite')
    
    # Check key type and size
    if ssl_result.key_type == 'RSA':
        if ssl_result.key_size >= 4096:
            result.key_security = 'excellent'
            result.score += 15
        elif ssl_result.key_size >= 2048:
            result.key_security = 'good'
            result.score += 12
        else:
            result.key_security = 'weak'
            result.score += 5
            result.recommendations.append(f'RSA key size {ssl_result.key_size} is weak - use 2048+ bits')
    elif ssl_result.key_type and ssl_result.key_type.startswith('EC-'):
        if ssl_result.key_size >= 384:
            result.key_security = 'excellent'
            result.score += 15
        elif ssl_result.key_size >= 256:
            result.key_security = 'good'
            result.score += 13
        else:
            result.key_security = 'adequate'
            result.score += 10
    elif ssl_result.key_type == 'EdDSA':
        result.key_security = 'excellent'
        result.score += 15
    
    # Check signature algorithm
    sig_alg = ssl_result.signature_algorithm
    if sig_alg and 'sha256' in sig_alg.lower():
        result.signature_security = 'good'
        result.score += 15
    elif sig_alg and 'sha384' in sig_alg.lower():
        result.signature_security = 'excellent'
        result.score += 15
    elif sig_alg and 'sha512' in sig_alg.lower():
        result.signature_security = 'excellent'
        result.score += 15
    elif sig_alg and 'sha1' in sig_alg.lower():
        result.signature_security = 'deprecated'
        result.score += 5
        result.recommendations.append('SHA-1 signature algorithm is deprecated - upgrade to SHA-256+')
    elif sig_alg and 'md5' in sig_alg.lower():
        result.signature_security = 'insecure'
        result.recommendations.append('MD5 signature algorithm is insecure - upgrade immediately')
    
    # Determine overall security level
    if result.score >= 90:
        result.security_level = 'excellent'
    elif result.score >= 75:
        result.security_level = 'good'
    elif result.score >= 50:
        result.security_level = 'fair'
    elif result.score >= 25:
        result.security_level = 'poor'
    else:
        result.security_level = 'critical'
    
    # Add summary
    result.summary = f"{result.security_level.title()} SSL/TLS security ({result.score}/100)"
    
    # Add best practices check
    if len(ssl_result.san) > 1:
        result.has_san = True
    else:
        result.has_san = False
        result.recommendations.append('Consider adding Subject Alternative Names (SAN) for multiple domains')
    
    return result
