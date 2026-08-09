# Deploy a WebApp

This copyable composite action is the standard django-mojo WebApp deployment
client. It accepts an already-built static directory, registers its immutable
manifest, uploads each file using its presigned URL, completes server-side
verification, and waits until the active edge fleet reports the release live.

Keep the WebApp's linked service credential in the repository or environment
secret named exactly `MOJO_DEPLOY_KEY`. Developers do not receive the key. The
credential is exposed only to this deploy step and is never a workflow input.

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

Automatic deployment must be enabled for the WebApp (the default). An explicit
manual hold causes the action to fail after verification because CI cannot
truthfully report that release as deployed.
