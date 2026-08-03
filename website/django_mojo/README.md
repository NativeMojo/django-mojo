# Django-MOJO website

This directory is the committed source for the public Django-MOJO marketing
and documentation site. The Markdown under `docs/django_developer/` and
`docs/web_developer/` remains the only authored documentation source.

Build the deployable bundle:

```bash
uv run python scripts/build_django_mojo_site.py
```

Validate links, anchors, deterministic shards, pinned dependencies, required
entry points, and SitesMojo file limits without keeping an output directory:

```bash
uv run python scripts/build_django_mojo_site.py --check
```

The ignored bundle is written to `docs_site/`. Catalog and full-text search
data are generated from the two documentation trees and split below 150 KB per
shard. Articles are fetched from the public GitHub repository at the exact
commit recorded in `data/build.json`; regenerate and redeploy to publish docs
changes.

The hosted site belongs to the NativeMojo workspace, is linked to the
django-mojo project, and uses the immutable slug `django-mojo`. A SitesMojo
deployment is public immediately. Large generated bundles must be uploaded
from disk through the Sites API rather than embedded in an MCP tool argument.

