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


class DomainLookup:
    """Domain information lookup utilities."""

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        """Normalize domain name."""
        return domain.lower().strip()

    @staticmethod
    def _dns_query(domain: str, record_type: str) -> objict:
        """Execute DNS query."""
        if not DNS_AVAILABLE:
            return objict(records=[], error="dnspython not installed")

        try:
            domain = DomainLookup._normalize_domain(domain)
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

    def a(self, domain: str) -> objict:
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
        return self._dns_query(domain, 'A')

    def ips(self, domain: str) -> list:
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
        result = self._dns_query(domain, 'A')
        return result.records if not result.error else []

    def ip(self, domain: str, random: bool = False) -> str:
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
        result = self._dns_query(domain, 'A')
        if result.error or not result.records:
            return ''

        if random and len(result.records) > 1:
            import random as rand_module
            return rand_module.choice(result.records)

        return result.records[0]

    def mx(self, domain: str) -> objict:
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
            domain = self._normalize_domain(domain)
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

    def txt(self, domain: str) -> objict:
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
            domain = self._normalize_domain(domain)
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

    def reverse(self, ip: str) -> objict:
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

    def dns(self, domain: str, record_type: str) -> objict:
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
        return self._dns_query(domain, record_type.upper())

    def lookup(self, domain: str) -> objict:
        """
        Comprehensive DNS lookup (A, MX, TXT).

        Args:
            domain: Domain name to query

        Returns:
            objict with a, mx, txt lists and error field

        Example:
            result = domain.lookup('example.com')
            print(result.a)    # ['93.184.216.34']
            print(result.mx)   # [{'priority': 0, 'host': '...'}]
            print(result.txt)  # ['v=spf1 ...']
        """
        domain = self._normalize_domain(domain)

        # Get A records
        a_result = self.a(domain)
        a_records = a_result.records if not a_result.error else []

        # Get MX records
        mx_result = self.mx(domain)
        mx_records = mx_result.records if not mx_result.error else []

        # Get TXT records
        txt_result = self.txt(domain)
        txt_records = txt_result.records if not txt_result.error else []

        return objict(
            domain=domain,
            a=a_records,
            mx=mx_records,
            txt=txt_records,
            error=None
        )

    def spf(self, domain: str) -> objict:
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
        txt_result = self.txt(domain)
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

    def dmarc(self, domain: str) -> objict:
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
        dmarc_domain = f"_dmarc.{self._normalize_domain(domain)}"
        txt_result = self.txt(dmarc_domain)

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

    def dkim(self, domain: str, selector: str = 'default') -> objict:
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
        dkim_domain = f"{selector}._domainkey.{self._normalize_domain(domain)}"
        txt_result = self.txt(dkim_domain)

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

    def whois(self, domain: str) -> objict:
        """
        Query WHOIS information for a domain.

        Args:
            domain: Domain name to query

        Returns:
            objict with domain information and error field

        Example:
            result = domain.whois('example.com')
            print(result.registrar)
            print(result.expiration_date)
        """
        if not WHOIS_AVAILABLE:
            return objict(error="python-whois not installed")

        try:
            domain = self._normalize_domain(domain)
            # Suppress stderr to avoid "Error trying to connect to socket" messages
            with suppress_stderr():
                w = whois_lib.whois(domain)

            # Build result objict
            result = objict(error=None)

            # Handle different response formats
            if isinstance(w, dict):
                result.update(w)
            else:
                # Extract common fields
                for field in ['domain_name', 'registrar', 'creation_date', 'expiration_date',
                             'updated_date', 'status', 'name_servers', 'registrant_name',
                             'registrant_email', 'admin_email', 'tech_email']:
                    if hasattr(w, field):
                        value = getattr(w, field)
                        # Normalize lists
                        if isinstance(value, list) and len(value) == 1:
                            value = value[0]
                        result[field] = value

            # Normalize domain_name to string if it's a list
            if 'domain_name' in result and isinstance(result.domain_name, list):
                result.domain_name = result.domain_name[0] if result.domain_name else None

            return result

        except Exception as e:
            return objict(error=str(e))

    def is_available(self, domain: str) -> objict:
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
        whois_result = self.whois(domain)

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

    def ssl(self, domain: str, port: int = 443) -> objict:
        """
        Get SSL certificate information.

        Args:
            domain: Domain name to check
            port: Port number (default: 443)

        Returns:
            objict with certificate information and error field

        Example:
            result = domain.ssl('example.com')
            print(f"Expires in {result.days_remaining} days")
            print(f"Issuer: {result.issuer.CN}")
        """
        if not CRYPTOGRAPHY_AVAILABLE:
            return objict(error="cryptography not installed")

        try:
            domain = self._normalize_domain(domain)

            # Create SSL context
            context = ssl_module.create_default_context()

            # Connect and get certificate
            with socket.create_connection((domain, port), timeout=10) as sock:
                with context.wrap_socket(sock, server_hostname=domain) as ssock:
                    cert_der = ssock.getpeercert(binary_form=True)

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

            return objict(
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
                error=None
            )

        except socket.timeout:
            return objict(error="Connection timeout")
        except socket.gaierror:
            return objict(error="Domain not found")
        except ssl_module.SSLError as e:
            return objict(error=f"SSL error: {str(e)}")
        except Exception as e:
            return objict(error=str(e))


# Create singleton instance
_lookup = DomainLookup()

# Export convenience functions
a = _lookup.a
ip = _lookup.ip
ips = _lookup.ips
mx = _lookup.mx
txt = _lookup.txt
reverse = _lookup.reverse
dns = _lookup.dns
lookup = _lookup.lookup
spf = _lookup.spf
dmarc = _lookup.dmarc
dkim = _lookup.dkim
whois = _lookup.whois
is_available = _lookup.is_available
ssl = _lookup.ssl
