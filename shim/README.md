# django-nativemojo

> **Deprecated compatibility package.** New installs should use [`django-mojo`](https://pypi.org/project/django-mojo/) instead.

This package is a thin shim that depends on `django-mojo`. It exists only to keep existing deployments working during the migration.

## Migrate

```bash
pip install django-mojo
```

Your imports (`from mojo import ...`) are unchanged.
