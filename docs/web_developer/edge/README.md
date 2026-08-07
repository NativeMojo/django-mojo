# edge — vhost and upstream API

How a domain gets served. Pairs with [dnsman](../dnsman/README.md), which owns
the domain and issues the certificate.

Backend reference: [django_developer/edge](../../django_developer/edge/README.md).

Releasing site builds (register, promote, roll back):
[releases.md](releases.md).

Deploying API code to the fleet (webhook + manual trigger):
[deploy.md](deploy.md).

## The shape to expect

You never send nginx configuration. A vhost is a small set of choices, and the
API derives everything that ends up in a config file:

- **`server_name`** comes from the `domain` you name plus a `label` — you
  cannot type it, and you cannot claim a name under a domain your group does
  not own.
- **The web root** comes from the vhost's own id.
- **The proxy destination** is a reference to a declared `upstream`, never a
  URL you supply.

A request carrying anything nginx would treat as syntax is rejected, not
escaped.

## Vhosts

```
GET    /api/edge/vhost                list (scoped to your group)
GET    /api/edge/vhost/<id>           detail
POST   /api/edge/vhost                create
POST   /api/edge/vhost/<id>           update
DELETE /api/edge/vhost/<id>           delete
```

**Permissions:** `view_dns` to read; `manage_dns` (or `security`) to write.

### Fields

| Field | Writable | Notes |
|---|---|---|
| `domain` | on create only | Owns the name and the tenancy. Immutable afterwards — changing it would move the vhost between groups. |
| `label` | yes | `""` serves the apex, `"*"` the wildcard, otherwise one DNS label (`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`). |
| `kind` | yes | `static`, `spa`, or `proxy`. |
| `upstream` | yes | **Required** for `proxy`, **rejected** otherwise. |
| `certificate` | yes | Must belong to the same domain and cover the derived name. |
| `pool` | yes | Which fleet pool serves it. Default `"default"`. |
| `is_enabled` | yes | Only enabled vhosts are served. |

`server_name` is returned on every graph as a read-only extra.

### Errors worth handling explicitly

| Situation | Response |
|---|---|
| Two **enabled** vhosts on the same `domain` + `label` | 400 — disable the old one first, or stage the replacement with `is_enabled: false` |
| `certificate` belongs to another domain, or does not cover the name | 400 |
| `proxy` with no `upstream`, or `static`/`spa` with one | 400 |
| `label` containing a dot, uppercase, or any punctuation | 400 |
| The name is reserved by the deployment (it is the API's own hostname) | 400 |

A vhost may be created **disabled** with a certificate that does not yet cover
it — useful while a certificate is being reissued. The coverage and
reserved-name checks apply at enable time.

## Upstreams

```
GET  /api/edge/upstream               list the ones you may select
GET  /api/edge/upstream/<id>          detail
POST /api/edge/upstream/<id>          update  (only `is_enabled` is writable)
POST /api/edge/upstream/declare       create   — PLATFORM ADMIN ONLY
POST /api/edge/upstream/retire        disable  — PLATFORM ADMIN ONLY
```

**You will normally only call the read endpoints.** Declaring an upstream is
restricted to platform administrators, because this row is the allowlist that
makes a proxy vhost safe — if any tenant could add one, the reference would
stop being a constraint. Build the vhost form as a **select** over
`GET /api/edge/upstream`, which returns your group's upstreams plus the
platform's shared ones.

`host`, `port`, `socket_path` and `kind` are not writable over REST at all,
including for platform admins — an existing upstream cannot be repointed, only
retired and replaced.

## Machine endpoints

```
GET /api/edge/desired_state?pool=default
GET /api/edge/material/<certificate-id>
```

These are for **serving nodes**, not for browser clients. They require the
global `edge_node` permission, which is protected: a group administrator cannot
grant it to an API key, and a member-scoped grant plus `?group=` does not open
them. Nothing in a portal should call these.

`desired_state` carries certificate **identifiers only** — never key material.
