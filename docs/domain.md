# Domain Helper

The domain helper provides DNS, WHOIS, and SSL certificate lookup utilities. All functions return `objict` instances for convenient attribute-style access.

## Installation

The domain helper requires these additional dependencies:

```bash
pip install dnspython python-whois cryptography
```

## Quick Start

```python
from mojo.helpers import domain

# Quick DNS lookup
result = domain.lookup('example.com')
print(result.a)        # ['93.184.216.34']
print(result.mx)       # [{'priority': 0, 'host': 'example.com'}]
print(result.txt)      # ['v=spf1 ...']

# Individual lookups
a_records = domain.a('example.com')
mx_records = domain.mx('example.com')
txt_records = domain.txt('example.com')

# WHOIS information
info = domain.whois('example.com')
print(info.domain_name)     # 'EXAMPLE.COM'
print(info.registrar)       # 'IANA'
print(info.creation_date)   # datetime object
print(info.expiration_date) # datetime object

# Check domain availability
available = domain.is_available('example.com')
print(available.available)  # False
print(available.reason)     # 'Domain is registered'

# SSL certificate information
cert = domain.ssl('example.com')
print(cert.subject)         # {'CN': 'example.com'}
print(cert.issuer)          # {'CN': 'DigiCert'}
print(cert.valid_from)      # datetime object
print(cert.valid_until)     # datetime object
print(cert.days_remaining)  # 45
```

## DNS Functions

### domain.ip(domain, random=False)

Get a single IP address for a domain (most common use case).

```python
ip = domain.ip('example.com')
print(ip)  # '93.184.216.34' (first IP, DNS server may rotate)

# Works with subdomains
ip = domain.ip('mail.google.com')
print(ip)  # '142.250.185.37'

# Random selection from multiple IPs (for manual load balancing)
ip = domain.ip('example.com', random=True)
print(ip)  # Randomly selected IP if multiple exist

# Returns empty string on error
ip = domain.ip('invalid.domain.xxx')
print(ip)  # ''
```

**Note:** By default, returns the first IP from the DNS response. DNS servers typically handle round-robin rotation, so the first IP may vary between queries. Use `random=True` if you want to manually distribute load across multiple IPs.

### domain.ips(domain)

Get all IP addresses for a domain (returns list).

```python
ips = domain.ips('example.com')
print(ips)  # ['93.184.216.34']

# Some domains have multiple IPs (load balancing)
ips = domain.ips('google.com')
print(ips)  # ['142.250.185.46', '142.250.185.78', ...]

# Returns empty list on error
ips = domain.ips('invalid.domain.xxx')
print(ips)  # []
```

### domain.a(domain)

Query A records (IPv4 addresses) with full error details.

```python
result = domain.a('example.com')
# Returns: objict(records=['93.184.216.34'], error=None)

# Handle errors
result = domain.a('invalid.domain.xxx')
print(result.error)  # 'NXDOMAIN: Domain does not exist'
```

### domain.mx(domain)

Query MX records (mail servers).

```python
result = domain.mx('example.com')
# Returns: objict(
#     records=[
#         {'priority': 10, 'host': 'mail1.example.com'},
#         {'priority': 20, 'host': 'mail2.example.com'}
#     ],
#     error=None
# )

# Access records
for mx in result.records:
    print(f"Priority {mx['priority']}: {mx['host']}")
```

### domain.txt(domain)

Query TXT records.

```python
result = domain.txt('example.com')
# Returns: objict(records=['v=spf1 include:_spf.example.com ~all'], error=None)

# Multiple TXT records
for record in result.records:
    print(record)
```

### domain.reverse(ip)

Reverse DNS lookup (PTR record).

```python
result = domain.reverse('8.8.8.8')
# Returns: objict(hostname='dns.google', error=None)

print(result.hostname)  # 'dns.google'
```

### domain.dns(domain, record_type)

Query any DNS record type.

```python
# Query AAAA records (IPv6)
result = domain.dns('example.com', 'AAAA')
print(result.records)  # ['2606:2800:220:1:248:1893:25c8:1946']

# Query CNAME records
result = domain.dns('www.example.com', 'CNAME')
print(result.records)  # ['example.com']

# Query NS records
result = domain.dns('example.com', 'NS')
print(result.records)  # ['a.iana-servers.net', 'b.iana-servers.net']
```

### domain.lookup(domain)

Comprehensive DNS lookup (A, MX, TXT records).

```python
result = domain.lookup('example.com')
# Returns: objict(
#     domain='example.com',
#     a=['93.184.216.34'],
#     mx=[{'priority': 0, 'host': 'example.com'}],
#     txt=['v=spf1 -all'],
#     error=None
# )

print(f"IP: {result.a[0]}")
print(f"Mail servers: {len(result.mx)}")
print(f"TXT records: {len(result.txt)}")
```

## Email Security Functions

### domain.spf(domain)

Query and parse SPF (Sender Policy Framework) records.

```python
result = domain.spf('example.com')
# Returns: objict(
#     record='v=spf1 include:_spf.example.com ~all',
#     parsed={
#         'version': 'spf1',
#         'mechanisms': ['include:_spf.example.com'],
#         'qualifier': '~all'
#     },
#     valid=True,
#     error=None
# )

if result.valid:
    print(f"SPF Record: {result.record}")
    print(f"Mechanisms: {result.parsed.mechanisms}")
```

### domain.dmarc(domain)

Query and parse DMARC records.

```python
result = domain.dmarc('example.com')
# Returns: objict(
#     record='v=DMARC1; p=reject; rua=mailto:dmarc@example.com',
#     parsed={
#         'version': 'DMARC1',
#         'policy': 'reject',
#         'rua': ['mailto:dmarc@example.com']
#     },
#     valid=True,
#     error=None
# )

if result.parsed:
    print(f"Policy: {result.parsed.policy}")
    print(f"Aggregate reports: {result.parsed.rua}")
```

### domain.dkim(domain, selector='default')

Query DKIM records for a specific selector.

```python
result = domain.dkim('example.com', selector='default')
# Returns: objict(
#     record='v=DKIM1; k=rsa; p=MIGfMA0GCSqGSIb3...',
#     parsed={
#         'version': 'DKIM1',
#         'key_type': 'rsa',
#         'public_key': 'MIGfMA0GCSqGSIb3...'
#     },
#     valid=True,
#     error=None
# )

# Try multiple selectors
for selector in ['default', 'google', 'k1']:
    result = domain.dkim('example.com', selector=selector)
    if result.valid:
        print(f"Found DKIM with selector '{selector}'")
        break
```

## WHOIS Functions

### domain.whois(domain)

Query WHOIS information for a domain.

```python
result = domain.whois('example.com')
# Returns: objict(
#     domain_name='EXAMPLE.COM',
#     registrar='IANA',
#     creation_date=datetime(1995, 8, 14, 4, 0),
#     expiration_date=datetime(2024, 8, 13, 4, 0),
#     updated_date=datetime(2023, 8, 14, 7, 1, 31),
#     status=['clientDeleteProhibited', 'clientTransferProhibited'],
#     name_servers=['A.IANA-SERVERS.NET', 'B.IANA-SERVERS.NET'],
#     registrant_name='...',
#     registrant_email='...',
#     admin_email='...',
#     tech_email='...',
#     error=None
# )

if result.domain_name:
    print(f"Domain: {result.domain_name}")
    print(f"Registrar: {result.registrar}")
    print(f"Created: {result.creation_date}")
    print(f"Expires: {result.expiration_date}")
    print(f"Status: {', '.join(result.status)}")
```

### domain.is_available(domain)

Check if a domain is available for registration.

```python
result = domain.is_available('example.com')
# Returns: objict(
#     domain='example.com',
#     available=False,
#     reason='Domain is registered',
#     error=None
# )

if result.available:
    print(f"{result.domain} is available!")
else:
    print(f"{result.domain} is not available: {result.reason}")

# Check multiple domains
domains = ['myawesomeapp.com', 'myawesomeapp.net', 'myawesomeapp.io']
for domain_name in domains:
    result = domain.is_available(domain_name)
    if result.available:
        print(f"✓ {domain_name} is available")
    else:
        print(f"✗ {domain_name} is taken")
```

## SSL Certificate Functions

### domain.ssl(domain, port=443)

Get SSL certificate information.

```python
result = domain.ssl('example.com')
# Returns: objict(
#     subject={'CN': 'example.com'},
#     issuer={'CN': 'DigiCert TLS RSA SHA256 2020 CA1', 'O': 'DigiCert Inc', 'C': 'US'},
#     serial_number='0F8A...',
#     version=3,
#     valid_from=datetime(2023, 1, 13, 0, 0),
#     valid_until=datetime(2024, 2, 13, 23, 59, 59),
#     days_remaining=45,
#     expired=False,
#     san=['example.com', 'www.example.com'],
#     fingerprint='A1:B2:C3:...',
#     error=None
# )

cert = domain.ssl('example.com')
if cert.expired:
    print("⚠️  Certificate has expired!")
elif cert.days_remaining < 30:
    print(f"⚠️  Certificate expires in {cert.days_remaining} days")
else:
    print(f"✓ Certificate valid for {cert.days_remaining} days")

print(f"Issued by: {cert.issuer.CN}")
print(f"Valid from: {cert.valid_from}")
print(f"Valid until: {cert.valid_until}")
print(f"Subject Alt Names: {', '.join(cert.san)}")

# Check custom port
result = domain.ssl('mail.example.com', port=587)
```

## Error Handling

All functions return `objict` with an `error` field. Always check for errors:

```python
result = domain.a('invalid.domain.xxx')
if result.error:
    print(f"Error: {result.error}")
else:
    print(f"IP addresses: {result.records}")

# WHOIS errors
result = domain.whois('thisdoesnotexist12345.com')
if result.error:
    print(f"WHOIS lookup failed: {result.error}")

# SSL errors
result = domain.ssl('expired.badssl.com')
if result.expired:
    print("Certificate has expired")
if result.error:
    print(f"SSL error: {result.error}")
```

## Advanced Usage

### Batch Domain Lookups

```python
domains = ['example.com', 'google.com', 'github.com']

# Check multiple domains
results = []
for domain_name in domains:
    info = domain.lookup(domain_name)
    results.append({
        'domain': domain_name,
        'ips': info.a,
        'mx_count': len(info.mx),
        'has_spf': bool(domain.spf(domain_name).valid)
    })

for result in results:
    print(f"{result['domain']}: {len(result['ips'])} IPs, {result['mx_count']} MX records")
```

### Email Server Validation

```python
def validate_email_domain(domain_name):
    """Check if domain is properly configured for email."""
    results = {
        'domain': domain_name,
        'mx_records': False,
        'spf': False,
        'dmarc': False,
        'dkim': False
    }
    
    # Check MX records
    mx = domain.mx(domain_name)
    results['mx_records'] = len(mx.records) > 0 if not mx.error else False
    
    # Check SPF
    spf = domain.spf(domain_name)
    results['spf'] = spf.valid
    
    # Check DMARC
    dmarc = domain.dmarc(domain_name)
    results['dmarc'] = dmarc.valid
    
    # Check DKIM (try common selectors)
    for selector in ['default', 'google', 'k1', 's1']:
        dkim = domain.dkim(domain_name, selector=selector)
        if dkim.valid:
            results['dkim'] = True
            break
    
    return results

# Usage
validation = validate_email_domain('example.com')
print(f"MX Records: {'✓' if validation['mx_records'] else '✗'}")
print(f"SPF: {'✓' if validation['spf'] else '✗'}")
print(f"DMARC: {'✓' if validation['dmarc'] else '✗'}")
print(f"DKIM: {'✓' if validation['dkim'] else '✗'}")
```

### SSL Certificate Monitoring

```python
def check_ssl_expiry(domains):
    """Monitor SSL certificates for multiple domains."""
    warnings = []
    
    for domain_name in domains:
        cert = domain.ssl(domain_name)
        if cert.error:
            warnings.append(f"{domain_name}: {cert.error}")
        elif cert.expired:
            warnings.append(f"{domain_name}: Certificate expired!")
        elif cert.days_remaining < 30:
            warnings.append(f"{domain_name}: Expires in {cert.days_remaining} days")
    
    return warnings

# Usage
domains = ['example.com', 'api.example.com', 'mail.example.com']
warnings = check_ssl_expiry(domains)
for warning in warnings:
    print(f"⚠️  {warning}")
```

## Return Value Structure

All functions return `objict` instances with consistent structure:

### DNS Functions
```python
{
    'records': [...],      # List of records
    'error': None          # Error message if failed
}
```

### WHOIS Functions
```python
{
    'domain_name': '...',
    'registrar': '...',
    'creation_date': datetime,
    'expiration_date': datetime,
    # ... other fields
    'error': None
}
```

### SSL Functions
```python
{
    'subject': {...},
    'issuer': {...},
    'valid_from': datetime,
    'valid_until': datetime,
    'days_remaining': int,
    'expired': bool,
    'san': [...],
    'error': None
}
```

### Parsed Email Security Records
```python
{
    'record': '...',       # Raw record
    'parsed': {...},       # Parsed components
    'valid': bool,         # Whether record is valid
    'error': None
}
```

## Notes

- All datetime values are returned as Python `datetime` objects
- Domain names are automatically normalized (lowercased, stripped)
- DNS lookups use system resolver by default
- WHOIS lookups may be rate-limited by registrars
- SSL certificate checks open a socket connection
- All functions handle errors gracefully and return error information
- Use `objict` for convenient dot-notation access: `result.records` vs `result['records']`
