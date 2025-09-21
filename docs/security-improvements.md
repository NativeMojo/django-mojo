# MOJO Security Improvements

## Current Security Status

Based on comprehensive security scanning:

- **68 endpoints properly secured** (36 with `@md.requires_perms` + 32 model-secured)
- **98 endpoints need explicit security declarations**
- **4 models intentionally public** (Docit documentation)

## Enhanced Security Framework

MOJO now has enhanced security decorators in `mojo/decorators/auth.py` with automatic detection:

### Core Security Decorators
```python
from mojo import decorators as md

@md.requires_perms('permission1', 'permission2')  # Specific permissions
@md.requires_auth()                               # Authentication required
@md.requires_bearer('token')                      # Bearer token validation
```

### New Explicit Declaration Decorators
```python
# Mark endpoints with custom security logic
@md.custom_security("Dynamic account-level permission checking")

# Mark intentionally public endpoints
@md.public_endpoint("GeoIP lookup for security monitoring")

# Mark endpoints that rely on model-level security
@md.uses_model_security(User) 

# Mark token-secured endpoints
@md.token_secured(['upload_token'], "Secured by upload token validation")
```

## Priority Recommendations

### Priority 1: Financial Systems (Critical)
**ATM/POS endpoints** - Handle financial transactions and need strict security:

```python
# mojo/apps/atm/rest/host.py
@md.URL('host')
@md.requires_perms('manage_atm', 'view_financial_data')
def on_atm_host(request, pk=None):
    # existing implementation

# mojo/apps/pos/rest/terminal.py  
@md.URL('terminal')
@md.requires_perms('manage_pos', 'view_financial_data')
def on_pos_terminal(request, pk=None):
    # existing implementation
```

### Priority 2: System Administration (High)
**Jobs control endpoints** - Many already secured, but some missing:

```python
# Already good pattern in mojo/apps/jobs/rest/control.py:
@md.GET('control/config')
@md.requires_perms('manage_jobs')  # ✓ Already implemented
```

### Priority 3: Business Logic (Medium)
**Incident management** - Mix of model security and explicit decorators needed:

```python
# mojo/apps/incident/rest/event.py
@md.URL('incident')
@md.requires_perms('view_incidents')  # Add this
def on_incident(request, pk=None):
    return Incident.on_rest_request(request, pk)  # Model security + explicit
```

### Priority 4: Supporting Systems (Lower)
**Metrics endpoints** - Have custom security logic, use new decorator for clarity:

```python
# mojo/apps/metrics/rest/base.py
@md.POST('record')
@md.custom_security("Dynamic account-level permission checking")
def on_metrics_record(request):
    # existing custom permission logic remains unchanged
```

## Implementation Strategy

### Phase 1: Add Missing Decorators
Apply `@md.requires_perms()` to endpoints based on their sensitivity:

- **Financial**: `'manage_atm'`, `'manage_pos'`, `'view_financial_data'`
- **Administrative**: `'manage_jobs'`, `'admin'`
- **Incident Management**: `'view_incidents'`, `'manage_incidents'`
- **File Management**: `'manage_files'`, `'view_files'`

### Phase 2: Document Intentionally Public Endpoints
Use the new `@md.public_endpoint()` decorator:

```python
# mojo/apps/aws/rest/sns.py
@md.URL('email/sns/inbound')
@md.public_endpoint("SNS webhook for incoming email - secured by AWS signatures")
def on_sns_inbound(request):
    # AWS signature validation provides security

# mojo/apps/account/rest/geoip.py  
@md.URL('system/geoip/lookup')
@md.public_endpoint("GeoIP lookup for security monitoring")
def on_geo_located_ip_lookup(request):
    # Intentionally public for security monitoring
```

### Phase 3: Token-Secured Endpoints
Use the new `@md.token_secured()` decorator:

```python
# mojo/apps/fileman/rest/upload.py
@md.URL('upload/<str:upload_token>')
@md.token_secured(['upload_token'], "Secured by upload token validation")
def on_upload(request, upload_token):
    # Token validation provides security

@md.URL('download/<str:download_token>')
@md.token_secured(['download_token'], "Secured by download token validation")
def on_download(request, download_token):
    # Token validation provides security
```

### Phase 4: Model-Secured Endpoints
Use `@md.uses_model_security()` for explicit model security:

```python
# mojo/apps/incident/rest/event.py
@md.URL('incident')
@md.uses_model_security(Incident)
def on_incident(request, pk=None):
    return Incident.on_rest_request(request, pk)  # RestMeta provides security
```

## Permission Categories

Based on existing usage patterns:

- **manage_jobs**: Job system administration
- **manage_email**: Email system configuration  
- **view_financial_data**: Financial transaction access
- **manage_atm**, **manage_pos**: Device-specific management
- **view_incidents**, **manage_incidents**: Security incident handling

## Testing Security Changes

After applying decorators, run the security test:
```bash
python runner.py tests.test_security.test_routes -v
```

This should show significantly fewer "security_type": "none" endpoints.

## Next Steps

1. **Apply Priority 1 decorators** to financial endpoints
2. **Test critical paths** to ensure decorators don't break functionality
3. **Document public endpoints** that should remain unsecured
4. **Consider model-level security** for CRUD operations vs explicit decorators

The goal is explicit security that's easily auditable while maintaining your existing security architecture.