---
globs: mojo/**/models/**/*.py,mojo/**/models/*.py
---

# Model Conventions

- Regular models: inherit `models.Model, MojoModel` (in that order).
- Secrets models: inherit `MojoSecrets, MojoModel` (do not include `models.Model`).
- Include `created` and `modified` fields:
  - `created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)`
  - `modified = models.DateTimeField(auto_now=True, db_index=True)`
- One model per file.
- Use `user` (`account.User`) and/or `group` (`account.Group`) ForeignKeys where access control requires it.
- Define `RestMeta` with explicit permissions and graphs.
- Always include the domain category permission (`security`, `users`, `groups`, `comms`, `jobs`, `metrics`, `files`) in both `VIEW_PERMS` and `SAVE_PERMS`.
- Use view/manage pairs for fine-grained access: `view_X` for read-only, `manage_X` for write.
- If `manage_X` is in `SAVE_PERMS`, add it to `VIEW_PERMS` too — users must read what they can write.
