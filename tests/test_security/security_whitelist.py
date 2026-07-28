# Security Whitelist Configuration
# This file contains known-good public endpoints and models that are intentionally public

SECURITY_WHITELIST = {
    'public_endpoints': [
        # Authentication & System Info (intentionally public)
        'api/version',
        'api/versions',
        'api/myip',
        'api/sysinfo/detailed',
        'api/sysinfo/network/tcp/summary',

        # Authentication endpoints (must be public)
        'api/login',
        'api/auth/login',
        'api/auth/forgot',
        'api/auth/password/reset/code',
        'api/auth/password/reset/token',
        'api/token/refresh',
        'api/auth/token/refresh',
        'api/refresh_token',

        # Documentation — ONLY the opt-in public surface. The docit CRUD and
        # slug endpoints are tenant-scoped and authenticated (maestro item
        # 530); they were whitelisted here as "intentionally public", which is
        # why this suite never flagged the cross-tenant exposure.
        'api/docit/public/book/<str:slug>',
        'api/docit/public/pages',
        'api/docit/public/page',

        # Add more patterns as needed...
        # Use this format for parameterized routes:
        # 'api/some/endpoint/<int:pk>'
    ],

    'public_models': [
        # No docit models here: reads are confined by RestMeta VIEW_PERMS +
        # GROUP_FIELD, and anonymous reading goes through the dedicated
        # public/* endpoints above rather than the models being public.

        # Add more models as needed...
        # Format: 'app_name.ModelName'
    ],

    # Optional: Add reasons for why things are whitelisted
    'whitelist_reasons': {
        'api/login': 'Authentication endpoint - must be public',
        'api/version': 'Version info - safe to be public',
        'api/myip': 'IP lookup utility - safe to be public',
        'api/sysinfo/detailed': 'System info endpoint - safe for monitoring',
        'api/docit/public/book/<str:slug>':
            'Opt-in public docs — serves only books with is_public=True',
        'api/docit/public/pages':
            'Opt-in public docs — published pages of an is_public book',
        'api/docit/public/page':
            'Opt-in public docs — one published page of an is_public book',
    }
}


# Example of how to add more items:
"""
To whitelist a new endpoint:
1. Add the pattern to 'public_endpoints'
2. Optionally add a reason to 'whitelist_reasons'

To whitelist a new model:
1. Add the full model name to 'public_models'
2. Optionally add a reason to 'whitelist_reasons'

Endpoint patterns support Django URL patterns:
- Simple: 'api/endpoint'
- With parameters: 'api/endpoint/<int:pk>'
- With string parameters: 'api/endpoint/<str:slug>'

Model names should be full module paths:
- Format: 'mojo.apps.app_name.ModelName'
"""
