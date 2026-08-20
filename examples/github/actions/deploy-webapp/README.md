# Deploy a WebApp

This composite action is the standard django-mojo WebApp deployment client. It
accepts an already-built static directory, registers its immutable manifest,
uploads each file using its presigned URL, completes server-side verification,
and waits until the active edge fleet reports the release live.

> **This is a live public contract.** The admin portal's onboarding wizard
> generates a workflow that references this action at
> `NativeMojo/django-mojo/examples/github/actions/deploy-webapp@main`, so a
> breaking change here breaks every generated pipeline on the next run. Treat
> `action.yml` and `deploy.py` as published API: change inputs additively, and
> keep the `@main` reference working. (The `@v1` pin below still works for repos
> that prefer to pin a tag.)

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

## How the release is labelled

`deploy.py` adds one additive key — `"source": "github"` — to the register
call, and only when `GITHUB_ACTIONS=true`, the variable the runner sets. Run
the same script from a laptop and the key is absent, so a hand-run stays
honestly labelled.

The marker is a hint, never an authority. The platform derives the source
class from the credential itself and only lets this key refine it: it becomes
`github` when a key-authenticated call registers against a site with a GitHub
repository configured, and `api` otherwise. An interactive browser session is
always `upload`, whatever it sends. No caller can label a release with a class
its credential did not already prove.

**A pinned or vendored copy reads "via CLI or API".** The marker lives in
`deploy.py`, so workflows on `@v1`, on a forked copy, or on hand-rolled CI of
their own keep registering without it — those releases are labelled `api`,
which is true. Moving to `@main`, or to a `@v1` tag that includes this change,
is what starts the GitHub labelling.

Verified completion always starts deployment. There is no separate promotion
approval or manual hold: the protected GitHub branch is the human control
plane. To roll back intentionally, rerun the workflow for the older commit.
