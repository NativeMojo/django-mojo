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

        # Documentation (public documentation system)
        'api/docit/book',
        'api/docit/page',
        'api/docit/book/<int:pk>',
        'api/docit/page/<int:pk>',
        'api/docit/book/slug/<str:slug>',
        'api/docit/page/slug/<str:slug>',
        'api/docit/page/revision',
        'api/docit/page/revision/<int:pk>',
        'api/docit/book/asset',
        'api/docit/book/asset/<int:pk>',

        # Add more patterns as needed...
        # Use this format for parameterized routes:
        # 'api/some/endpoint/<int:pk>'
    ],

    'public_models': [
        # Documentation models (if meant to be public)
        'mojo.apps.docit.Book',
        'mojo.apps.docit.Page',
        'mojo.apps.docit.Asset',
        'mojo.apps.docit.PageRevision',

        # Add more models as needed...
        # Format: 'app_name.ModelName'
    ],

    # Optional: Add reasons for why things are whitelisted
    'whitelist_reasons': {
        'api/login': 'Authentication endpoint - must be public',
        'api/version': 'Version info - safe to be public',
        'api/myip': 'IP lookup utility - safe to be public',
        'api/sysinfo/detailed': 'System info endpoint - safe for monitoring',
        'api/docit/book': 'Public documentation system',
        'api/docit/page': 'Public documentation system',
        'mojo.apps.docit.Book': 'Public documentation model',
        'mojo.apps.docit.Page': 'Public documentation model',
        'mojo.apps.docit.Asset': 'Public documentation assets',
        'mojo.apps.docit.PageRevision': 'Public documentation revisions',
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
