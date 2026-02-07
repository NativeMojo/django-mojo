# Domain Helper

The domain helper provides DNS, WHOIS, and SSL certificate lookup utilities. All functions return `objict` instances for convenient attribute-style access.

## Installation

The domain helper requires these additional dependencies:

```bash
pip install dnspython python-whois cryptography
```

## JSON Serialization

The `whois()` and `ssl()` functions return datetime objects by default, which are not JSON-serializable. Use the `safe=True` parameter to convert datetime objects to epoch timestamps (Unix timestamps) for JSON serialization:

```python
import json
from mojo.helpers import domain

# Default mode returns datetime objects
result = domain.whois('example.com')
print(result.expiration_date)  # datetime.datetime(2024, 8, 13, ...)
# json.dumps(dict(result))  # Would raise TypeError

# Safe mode returns epoch timestamps
result = domain.whois('example.com', safe=True)
print(result.expiration_date)  # 1723507199 (epoch timestamp)
json_str = json.dumps(dict(result))  # Works perfectly

# Same for SSL certificates
cert = domain.ssl('example.com', safe=True)
print(cert.valid_until)  # 1723507199 (epoch timestamp)
json_str = json.dumps(dict(cert))  # JSON serializable
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

Comprehensive DNS lookup (A, CNAME, MX, TXT records).

```python
result = domain.lookup('example.com')
# Returns: objict(
#     domain='example.com',
#     a=['93.184.216.34'],
#     cname=[],  # Empty if no CNAME, otherwise ['target.example.com']
#     mx=[{'priority': 0, 'host': 'example.com'}],
#     txt=['v=spf1 -all'],
#     error=None
# )

print(f"IP: {result.a[0]}")
print(f"CNAME: {result.cname[0] if result.cname else 'None'}")
print(f"Mail servers: {len(result.mx)}")
print(f"TXT records: {len(result.txt)}")

# Example with CNAME
result = domain.lookup('www.github.com')
print(f"CNAME: {result.cname}")  # ['github.com.']
print(f"A records: {result.a}")  # IPs after following CNAME
```

## Email Provider Detection

### domain.email_provider(domain)

Detect email provider from domain by analyzing MX records.

```python
result = domain.email_provider('gmail.com')
# Returns: objict(
#     provider='Gmail',
#     type='personal',
#     confidence='high',
#     custom_domain=False,
#     is_disposable=False,
#     is_corporate=False,
#     mx_records=[],
#     error=None
# )

print(result.provider)       # 'Gmail'
print(result.type)           # 'personal'
print(result.is_disposable)  # False
```

**Use Cases:**
- Validate email addresses (block disposable emails)
- Analytics (track which providers users use)
- Security (flag high-risk providers)
- User experience (provide provider-specific help)

**Provider Types:**
- `personal` - Gmail, Outlook, Yahoo, iCloud
- `business` - Google Workspace, Microsoft 365, Zoho
- `education` - .edu domains
- `disposable` - Temporary email services
- `corporate` - Self-hosted/corporate email
- `transactional` - AWS SES, SendGrid, Mailgun
- `security` - Mimecast, Proofpoint, Barracuda

**Examples:**

```python
# Custom domain using Google Workspace
result = domain.email_provider('yourcompany.com')
print(result.provider)        # 'Google Workspace'
print(result.type)            # 'business'
print(result.custom_domain)   # True
print(result.mx_records)      # [{'priority': 1, 'host': 'aspmx.l.google.com'}, ...]

# Disposable email detection
result = domain.email_provider('mailinator.com')
print(result.provider)        # 'Mailinator'
print(result.type)            # 'disposable'
print(result.is_disposable)   # True

# Corporate/self-hosted email
result = domain.email_provider('bigcorp.com')
print(result.provider)        # 'Self-Hosted / Corporate'
print(result.type)            # 'corporate'
print(result.is_corporate)    # True

# Educational institution
result = domain.email_provider('stanford.edu')
print(result.provider)        # 'Educational Institution'
print(result.type)            # 'education'
```

**Validation Example:**

```python
def validate_email_signup(email):
    """Validate email for user signup."""
    domain_part = email.split('@')[1]
    provider = domain.email_provider(domain_part)
    
    # Block disposable emails
    if provider.is_disposable:
        return {
            'valid': False,
            'error': f'Disposable email addresses are not allowed ({provider.provider})'
        }
    
    # Warn about personal emails for business signups
    if provider.type == 'personal':
        return {
            'valid': True,
            'warning': 'Consider using your work email address'
        }
    
    return {'valid': True}

# Usage
result = validate_email_signup('user@mailinator.com')
print(result)  # {'valid': False, 'error': 'Disposable email addresses...'}
```

**Supported Providers:**
- Gmail, Google Workspace
- Outlook, Microsoft 365
- Yahoo, ProtonMail, FastMail
- Zoho, iCloud
- AWS SES, SendGrid, Mailgun, Mandrill
- Mimecast, Proofpoint, Barracuda
- Common disposable email services
- Educational institutions (.edu)
- Self-hosted/corporate email

## Email Security Functions

### domain.email_security(domain, dkim_selectors=None)

Comprehensive email security check that tests SPF, DMARC, and DKIM records and provides an overall security score with recommendations.

```python
result = domain.email_security('example.com')
# Returns: objict(
#     domain='example.com',
#     summary='Good email security (70/100)',
#     score=70,
#     security_level='good',
#     spf=objict(status='configured', valid=True, record='...', qualifier='softfail'),
#     dmarc=objict(status='configured', valid=True, record='...', policy='reject', policy_strength='strong'),
#     dkim=objict(status='not_configured', valid=False, record=None, selector=None),
#     recommendations=[
#         'Missing: DKIM',
#         'Configure DKIM signing (tried selectors: default, google, k1...)'
#     ],
#     error=None
# )

print(f"Security Score: {result.score}/100")
print(f"Summary: {result.summary}")
print(f"Level: {result.security_level}")  # excellent, good, fair, poor, critical

# Check each component
print(f"SPF: {result.spf.status} - {result.spf.record}")
print(f"DMARC: {result.dmarc.status} - Policy: {result.dmarc.policy}")
print(f"DKIM: {result.dkim.status}")

# Get recommendations
for recommendation in result.recommendations:
    print(f"  - {recommendation}")

# Custom DKIM selectors
result = domain.email_security('example.com', dkim_selectors=['default', 'google', 'custom'])
```

**Scoring System:**
- SPF configured: +30 points
- DMARC configured: +40 points
- DKIM configured: +30 points

**Security Levels:**
- 90-100: Excellent
- 70-89: Good
- 40-69: Fair
- 20-39: Poor
- 0-19: Critical

**Use Cases:**
- Validate email configuration during domain setup
- Audit email security posture
- Monitor email authentication compliance
- Generate security reports
- Identify misconfigured email records

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

### domain.whois(domain, safe=False)

Query WHOIS information for a domain.

**Note:** Several fields are automatically normalized for easier use:
- **Dates**: Single values (not lists) - WHOIS servers return multiple timestamps with timezone variations, we keep the first/most accurate one
- **Registrar URL**: Single HTTPS URL preferred (not a list) - if both HTTP and HTTPS are available, HTTPS is returned
- **Address**: All address-related fields grouped into one object with `label`, `street`, `city`, `state`, `postal_code`, `country`
- **Status**: Parsed to boolean flags (see status object below)

```python
result = domain.whois('example.com')
# Returns: objict(
#     domain_name='EXAMPLE.COM',
#     registrar='IANA',
#     registrar_url='https://www.iana.org',  # Single HTTPS URL
#     creation_date=datetime(1995, 8, 14, 4, 0),  # Single datetime (not a list)
#     expiration_date=datetime(2024, 8, 13, 4, 0),  # Single datetime (not a list)
#     updated_date=datetime(2023, 8, 14, 7, 1, 31),  # Single datetime (not a list)
#     address=objict(
#         label='Company Name',
#         street='100 Main Street, Suite 200',
#         city='City Name',
#         state='State',
#         postal_code='12345',
#         country='US'
#     ),
#     status=objict(
#         delete_prohibited=True,
#         transfer_prohibited=True,
#         update_prohibited=False,
#         renew_prohibited=False,
#         hold=False,
#         locked=False,
#         ok=False,
#         pending_delete=False,
#         redemption_period=False
#     ),
#     name_servers=['A.IANA-SERVERS.NET', 'B.IANA-SERVERS.NET'],
#     name='...',
#     org='...',
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
    
    # Access clean address object
    print(f"Location: {result.address.city}, {result.address.state}")
    print(f"Address: {result.address.street}")
    
    # Check status flags (easy boolean checks)
    if result.status.transfer_prohibited:
        print("⚠️  Domain transfer is locked")
    if result.status.pending_delete:
        print("⚠️  Domain is pending deletion")
    if result.status.ok:
        print("✓ Domain status is OK")

# Use safe=True for JSON serialization (converts datetime to epoch timestamps)
result = domain.whois('example.com', safe=True)
print(result.expiration_date)  # 1723507199 (epoch timestamp)
import json
json.dumps(dict(result))  # Works without errors
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

### domain.ssl_security(domain, port=443)

Comprehensive SSL/TLS security audit with scoring and recommendations (similar to email_security).

```python
result = domain.ssl_security('example.com')
# Returns: objict(
#     domain='example.com',
#     summary='Excellent SSL/TLS security (98/100)',
#     score=98,
#     security_level='excellent',
#     certificate=objict(status='valid', valid=True, days_remaining=65, expired=False),
#     tls_version='TLSv1.3',
#     tls_security='excellent',
#     cipher_suite='TLS_AES_256_GCM_SHA384',
#     cipher_bits=256,
#     cipher_security='strong',
#     key_type='EC-secp256r1',
#     key_size=256,
#     key_security='good',
#     signature_algorithm='ecdsa-with-SHA256',
#     signature_security='good',
#     has_san=True,
#     recommendations=[],
#     error=None
# )

print(f"Security Score: {result.score}/100")
print(f"Summary: {result.summary}")
print(f"TLS Version: {result.tls_version} ({result.tls_security})")
print(f"Cipher: {result.cipher_suite} ({result.cipher_bits} bits)")
print(f"Key: {result.key_type} {result.key_size} bits")

# Check recommendations
for recommendation in result.recommendations:
    print(f"  - {recommendation}")
```

**Scoring System:**
- Certificate validity: up to 25 points
- TLS version (1.3=25, 1.2=20, older=0-5): up to 25 points
- Cipher strength (256-bit=20, 128-bit=15): up to 20 points
- Key size (RSA 4096=15, RSA 2048=12, EC 256+=13-15): up to 15 points
- Signature algorithm (SHA256+=15, SHA1=5, MD5=0): up to 15 points

**Security Levels:**
- 90-100: Excellent
- 75-89: Good
- 50-74: Fair
- 25-49: Poor
- 0-24: Critical

**Use Cases:**
- SSL/TLS configuration audits
- Security compliance checks
- Certificate monitoring
- Identify weak ciphers or protocols
- Pre-deployment security validation

### domain.ssl(domain, port=443, safe=False)

Get detailed SSL certificate and TLS connection information.

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
#     tls_version='TLSv1.3',
#     cipher_suite='TLS_AES_256_GCM_SHA384',
#     cipher_bits=256,
#     key_type='EC-secp256r1',
#     key_size=256,
#     signature_algorithm='ecdsa-with-SHA256',
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
print(f"TLS Version: {cert.tls_version}")
print(f"Cipher Suite: {cert.cipher_suite} ({cert.cipher_bits} bits)")
print(f"Key Type: {cert.key_type} ({cert.key_size} bits)")
print(f"Signature: {cert.signature_algorithm}")
print(f"Subject Alt Names: {', '.join(cert.san)}")

# Check custom port
result = domain.ssl('mail.example.com', port=587)

# Use safe=True for JSON serialization (converts datetime to epoch timestamps)
cert = domain.ssl('example.com', safe=True)
print(cert.valid_until)  # 1723507199 (epoch timestamp)
import json
json.dumps(dict(cert))  # Works without errors
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
