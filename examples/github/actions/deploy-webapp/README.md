# Deploy a WebApp

This copyable composite action is the standard django-mojo WebApp deployment
client. It accepts an already-built static directory, registers its immutable
manifest, uploads each file using its presigned URL, completes server-side
verification, and waits until the active edge fleet reports the release live.

Keep the WebApp's linked service credential in the repository or environment
secret named exactly `MOJO_DEPLOY_KEY`. Developers do not receive the key. The
credential is exposed only to this deploy step and is never a workflow input.

For the first deployment, bootstrap the credential from the already-running
Django platform:

```bash
ssh api-host '/opt/api/.venv/bin/python /opt/api/manage.py webapp_bootstrap \
  --webapp 42 --token-only' \
  | gh secret set MOJO_DEPLOY_KEY --repo YOUR_ORG/YOUR_WEBAPP
```

The management command refuses to rotate an existing key unless `--rotate` is
explicit. After the first deployment, web-mojo admin provides **Link new CI
key** on the WebApp detail screen.

```yaml
- name: Deploy WebApp
  uses: NativeMojo/django-mojo/examples/github/actions/deploy-webapp@v1
  with:
    api-url: ${{ vars.MOJO_API_URL }}
    webapp-id: ${{ vars.MOJO_WEBAPP_ID }}
    artifact-dir: dist
    version: ${{ github.sha }}
  env:
    MOJO_DEPLOY_KEY: ${{ secrets.MOJO_DEPLOY_KEY }}
```

The action deliberately does not build the app. The calling repository owns
its checkout, dependency install, tests, and build. Using the full Git commit
SHA as `version` makes reruns idempotent: the same manifest is reused, while a
different artifact under the same SHA is rejected.

Verified completion always starts deployment. There is no separate promotion
approval or manual hold: the protected GitHub branch is the human control
plane. To roll back intentionally, rerun the workflow for the older commit.
