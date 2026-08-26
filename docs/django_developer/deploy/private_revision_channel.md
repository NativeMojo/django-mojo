# Private MojoLand framework revisions

MojoLand experiments may consume one exact django-mojo commit without making
that commit a public release. This is a laboratory path, not a second release
channel: PyPI remains the source for every ordinary deployment.

## Build an exact candidate

Start from the long-lived `codex/mojoland-pooling-lab` branch and use full
commit IDs:

```bash
uv run python scripts/build_mojoland_lab_wheel.py \
  --source-sha "$(git rev-parse HEAD)" \
  --base-sha 3b9763b327fed7a5081eb08211df6ea618fbf74a \
  --output-dir var/mojoland-lab-artifacts
```

The builder rejects abbreviated or unrelated commits and any Python migration
changed since the approved base. It exports the selected commit with
`git archive`, patches only the temporary copy, and gives the wheel a version
such as `1.19.1+mojoland.g<40-character-sha>`. The checkout's public version is
never edited.

The adjacent canonical manifest records the exact source and base commits,
wheel name, byte count and SHA-256, inspected distribution/version, migration
result, builder identity and UTC build time. Inspect and retain both files as
one candidate. The MojoLand publisher derives immutable S3 object keys from
their hashes; nodes consume only the exact activation descriptor committed to
MojoLand.

## Boundaries

- Do not publish the local-version wheel to PyPI.
- Do not accept a candidate that contains migrations. Application rollback
  cannot reverse schema changes.
- Building does not activate anything. Publishing the bytes and committing a
  MojoLand descriptor are separate reviewed steps.
- Retire the private descriptor and force-reinstall the recorded public
  version when the experiment ends. A plain `django-mojo==1.19.1` install is
  insufficient because PEP 440 can consider a local `1.19.1+...` build a
  match.
