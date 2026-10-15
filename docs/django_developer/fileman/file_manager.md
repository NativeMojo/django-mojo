# FileManager Model — Django Developer Reference

## Overview

`FileManager` is the storage backend configuration. Each `File` belongs to a `FileManager` that determines where and how files are stored (local disk, AWS S3, etc.).

## Key Concepts

- One or more `FileManager` instances exist per deployment
- A `FileManager` can be scoped to a `User`, a `Group`, or neither (system-wide default)
- Backends are pluggable: local, S3, and others

## Ownership and Scoping

`FileManager` supports three scopes: user-owned, group-scoped, and system-wide. Because of this, `FileManager.RestMeta` sets `CREATED_BY_OWNER_FIELD = None` — the framework's create-time auto-stamping of the `user` field is **disabled**.

Behavior on REST create (`POST /api/fileman/manager`):

| Request body | `user` on created record |
|---|---|
| `user` omitted | `None` — group- or system-scoped manager |
| `user: null` | `None` |
| `user: <id>` | That user's id — user-scoped manager |

`group` auto-fill from `request.group` is **not** affected and works normally.

### System-scoped creation is superuser-only

A manager created with **no `user` and no `group`** is system-scoped — it is eligible to become the system default that `get_for_user` / `get_for_group` derive every other manager from. `FileManager.on_rest_pre_save` rejects this via REST unless the requester is a superuser, raising `PermissionDeniedException` (HTTP 403). Direct ORM creation (`FileManager.objects.create(...)`, bootstrap code, the internal `get_for_*` provisioning helpers) does not go through `on_rest_pre_save` and bypasses this guard.

## Getting the Right FileManager

```python
from mojo.apps.fileman.models import FileManager

# From the current request (resolves group, then system default)
fm = FileManager.get_from_request(request)

# For a specific user/group
fm = FileManager.get_for_user_group(user, group)
```

Upload initiation must use `FileManager.resolve_for_upload(request)`, not a
bare primary-key lookup. The resolver applies the upload authorization truth
table before any `File` or upload capability is created:

- uploads require an ordinary authenticated `User` session; API keys,
  override-user API keys, and group-scoped tokens are rejected;
- inactive managers and managers in an effectively inactive group are rejected;
- a user-scoped manager requires the exact calling user;
- a group-scoped manager requires the exact active request group and an active
  direct or inherited membership;
- a manager with both scopes requires both constraints; and
- a system-scoped manager requires global `manage_files` or `files` authority.

Global file administrators may use any active manager, but explicit `group`
and `use` selectors must still agree with that manager. Client-supplied `user`
selectors are never accepted. Authorization failures happen before storage
targets are generated.

A REST create of a `File` (`POST /api/fileman/file`) uses
`FileManager.resolve_for_file_create(request)`. For user sessions it is
`resolve_for_upload` unchanged. An `ApiKey` or group token, which the upload rule
refuses, gets the same selectors (named manager, else `get_from_request`) and is
allowed only an active group-scoped manager with no user that
`FileManager.rest_check_permission(request, "SAVE_PERMS", manager)` passes: the
key's own group or a descendant, with `files` or `manage_files` there. Anything
else is `403 File manager is unavailable`. `request.group` is restored after the
check.

`upload_policy` is the safe graph for upload configuration. It contains only
`id`, `name`, `use`, `is_active`, `max_file_size`, `allowed_extensions`,
`allowed_mime_types`, and `supports_direct_upload`; backend locations,
credentials, and encrypted settings are deliberately absent.

## FileManager Settings

Each `FileManager` stores backend-specific settings through the encrypted
`MojoSecrets.mojo_secrets` text field. Use the model helpers rather than reading
or writing that encrypted blob directly:

```python
# Access a setting
expiry = fm.get_setting("urls_expire_in", 3600)  # URL expiry in seconds
is_pub = fm.is_public                             # Whether files are publicly accessible
root = fm.root_path                               # Storage root path/bucket
```

### Shortlink settings

Three optional keys control shortlink behavior for files and renditions in this manager:

| Key | Default | Description |
|---|---|---|
| `use_shortlinks` | `None` (inherit global) | Force-on (`True`) or force-off (`False`) shortlink wrapping for this manager. `None` defers to the global `FILEMAN_USE_SHORTLINKS` setting (default `True`). |
| `shortlink_track_clicks` | `False` | When `True`, tier-1 shortlinks (auto-generated display URLs) log each click as a `ShortLinkClick` record. |
| `shortlink_expire_days` | `0` (never) | Lifetime in days for tier-1 shortlinks. `0` means the shortlink never expires. |

These settings apply only to tier-1 (auto-generated) links. Tier-2 share links set their own `expire_days` and `track_clicks` per call and do not consult these settings.

```python
fm.set_setting("use_shortlinks", False)          # disable short URLs for this manager
fm.set_setting("shortlink_track_clicks", True)   # log clicks on display URLs
fm.set_setting("shortlink_expire_days", 90)      # expire tier-1 links after 90 days
fm.save()
```

See [shortlinks.md](shortlinks.md) for the full shortlink pipeline and opt-out behavior.

## Storage Backends

### Local Backend

Files stored on local filesystem. No presigned URLs — uses the framework's upload endpoint.

### S3 Backend

Files stored in AWS S3. Supports:
- Presigned upload URLs (direct browser-to-S3 uploads)
- Presigned download URLs with configurable TTL
- Public bucket support

`python manage.py aws-check --section s3 --check` audits the system-default S3
manager, bucket access/region, Public Access Block and CORS without persisting
audit state. `--apply --bucket-name <name>` can create a missing private bucket
and system manager. `--apply --section s3` also repairs missing direct-upload
CORS. The default is `AllowedOrigins: ["*"]`, matching legacy django-mojo: CORS
permits a browser origin to use a presigned URL but does not authorize uploads
or make the private bucket public. Set `allowed_origins` on the FileManager when
a deployment deliberately wants a narrower list. `--probe-s3` is separately
confirmed and uses only a unique sentinel key that is deleted in `finally`; it
never lists or deletes user objects. Existing policies and unrelated CORS rules
are preserved.

Bucket and prefix come from `backend_url` (`s3://my-bucket/some/prefix`).
Credentials come from the manager's own settings, falling back to these
`settings.py` keys when a manager is created through REST:

```python
AWS_KEY = "..."       # optional — leave unset to use the instance profile
AWS_SECRET = "..."
AWS_REGION = "us-east-1"
```

#### Credential resolution

All S3 calls a manager makes — uploads, downloads, CORS, the public-access
audit — use the same credential session. S3 backends reuse up to 128
session/client pairs per worker, including across separately loaded ORM instances
of a manager. The cache distinguishes manager/database, bucket, credentials,
endpoint, region, signing/addressing options and AssumeRole settings. A newly
loaded manager with changed credentials gets a new entry; existing backend
instances retain their configuration snapshot.

Client construction is serialized across threads; requests use the shared client.
Resources remain local to each backend. Forked workers discard inherited clients,
and eviction does not close clients still used by live backends. Applications
must not close cached clients or mutate their event handlers/configuration.

IAM/STS refreshable credentials continue refreshing through botocore. Changes to
ambient environment variables or AWS profile files require a worker restart;
they are not manager-setting changes. See
[../aws/credentials.md](../aws/credentials.md) for the underlying factories.

| Setting | Meaning |
|---|---|
| `aws_key` | Static access key id. Optional. |
| `aws_secret` | Static secret. Required if `aws_key` is set, and vice versa. |
| `aws_region` | Region. Defaults to `us-east-1`. |
| `assume_role_arn` | Cross-account role to assume. When set, the settings above become only the *source* identity used to call `sts:AssumeRole`. See the role rule below. |
| `external_id` | `sts:ExternalId` for the trust policy. Omitted from the STS call when unset. Write-only — reads back as `has_external_id`. |
| `role_session_name` | STS session name. Defaults to `django-mojo-fileman-<manager pk>`. |
| `assume_role_duration` | Role session seconds. Defaults to 43200 (12 hours). |

**Leaving `aws_key`/`aws_secret` unset is a supported configuration**, not a
misconfiguration: the session falls through to botocore's default chain, which
ends at the EC2 instance profile or ECS task role. Setting exactly one of the
pair is an error and fails fast with a readable message rather than silently
acting as a different identity.

**Credential settings are read from the primary parent manager.** The backend
resolves them through `file_manager.primary_settings`, which walks up the
`parent` chain to the root. Setting `aws_key` or `assume_role_arn` on a child
manager has no effect on that child's storage calls — configure the root.

**Presigned URL lifetimes are clamped under an assumed role.** A URL signed with
STS temporary credentials stops working the moment those credentials expire,
regardless of its own `ExpiresIn`. botocore refreshes the role 10–15 minutes
ahead of expiry, so the backend caps `ExpiresIn` at whatever the live credential
has left. With the default 12-hour role duration this is never the binding
constraint for a 1-hour URL; with a short `assume_role_duration` it will be, and
the returned URL simply expires sooner than requested. The lifetime actually
requested for downloads is `urls_expire_in` (default 3600).

**Who may set a role over REST.** REST save dispatches a `set_<key>` method for
any key in the payload — the flat fields, `secrets` and `settings` all write the
same store — and `SAVE_PERMS` for FileManager is the group-level
`files`/`manage_files` permission. Without a check, anyone who can administer
files could point the platform's own credentials at a role they control — a
confused deputy. So `on_rest_pre_save` applies one rule to the store as the
request leaves it: a non-superuser may not leave `assume_role_arn` set on a
store for which `uses_platform_credentials()` is true, if the request changed
any of `FileManager.ROLE_SETTING_KEYS`, `aws_key` or `aws_secret` (on create,
every value counts as changed). It raises `PermissionDeniedException` (HTTP
403) before the `is_default` reshuffle or the save, so nothing from the object
that carries the refused change is stored, and the refused role value is stored
on no manager. The check does not make the request all-or-nothing: REST save
writes each object of a request separately (the addressed record's own fields,
then each nested object such as `parent: {...}`), so a change carried by a
different object may be kept when another object of the same request refuses,
on a different record or on the same one.

`uses_platform_credentials()` is true when the manager is system-scoped, when
its own secrets hold no `aws_key` or no `aws_secret` (a parent's key does not
count), or when its `aws_key` equals `settings.AWS_KEY` or the `aws_key` of any
system-scoped manager. Stores created over REST without a key, and the stores
`get_for_user` / `get_for_group` derive, are seeded with the platform's key, so
they stay in this group until given their own. On a store with its own key the
role is assumed with that key and reaches only what the key already reaches, so
the file permission is enough.

The rule reads each manager's own secrets. A child manager's own key and role
are stored but not used for storage calls (see above), and are judged the same
way.

Limits: the rule lives in the REST save hook only — direct ORM use (bootstrap,
`on_action_clone`, `get_for_*`) is not checked, and a role already stored is not
undone. A platform key that is in neither `settings.AWS_KEY` nor a system-scoped
manager cannot be recognised. `RestMeta.NO_SAVE_FIELDS` keeps `mojo_secrets`,
`secret` and `setting` out of a request body.

Changing `assume_role_arn` or `external_id` also changes the manager's
public-access config fingerprint, so cached audit evidence collected under the
old identity is invalidated.

#### S3 credential boundary

FileManager credentials are written with `set_aws_key()` and
`set_aws_secret()` and read by trusted backend code through the raw `aws_key`
and `aws_secret` properties. Those raw properties are internal-only and must
never be added to a REST graph.

The `default`, `list`, and `basic` REST graphs expose only
`aws_key_masked` and `aws_secret_masked`. A configured value longer than four
characters keeps only its last four characters visible; a non-empty value of
four characters or fewer is fully masked. An absent value is returned as an
empty string. File responses that nest a manager use the same safe `basic`
graph. An unmapped *common* graph name (e.g. `full`) falls back to the safe
`default` graph; an unmapped *special* name is refused with `400` rather than
served — see [Core → Graphs](../core/graphs.md#graph-resolution-fallback-refusal-and-the-whole-model-guard).

REST create and update requests still accept `aws_key` and `aws_secret` as
write-only inputs through the model's custom setters. The masked properties are
response-only: generic REST save ignores them. Clients should omit unchanged
credentials and must never echo a displayed mask as a replacement value.

This response-contract change does not rotate, rewrite, or migrate stored
credentials and does not change backend authentication. No data migration is
required.

#### CDN delivery (CloudFront)

An S3 file manager can deliver its files through CloudFront instead of S3
links (maestro #8250). This is what lets a long-running client (a kiosk, a
signage player) hold a link to a private, SSE-KMS bucket: a public manager
gets **unsigned CDN URLs that never expire**, a private manager gets
**CloudFront signed URLs** whose lifetime can exceed S3's 7-day presign cap.
The bucket stays private: CloudFront reads it through an Origin Access
Control, and an anonymous raw-S3 request still gets 403.

**Enable it:**

```bash
manage.py fileman_cdn --manager <id> enable            # provisions everything
manage.py fileman_cdn --manager <id> check             # read-only, prints exact fixes
manage.py fileman_cdn --manager <id> disable [--retire]
manage.py fileman_cdn rotate-key   /   revoke-key --key-id <K...>
```

or the FileManager actions `enable_cdn`, `check_cdn`, `disable_cdn`
(`POST /api/fileman/manager/<id>` with `{"action": "enable_cdn"}`); all three
need the **global** `manage_files` (or `files`) permission, which a superuser
implies: they read or change the account under the deployment identity and
report ARNs and policy documents, so the group-level grant that satisfies the
manager's `SAVE_PERMS` is not enough. `enable` runs under the deployment's AWS
identity (`AWS_KEY`/`AWS_SECRET` or the instance role), never a manager's
upload credentials; `--profile` runs under an operator's boto3 profile.

**What `enable` creates, and how it finds it again.** It converges
additively, like `mojo/deploy/provision`: create what is missing, never
delete, no waiters, no state file. A second run creates nothing; a run
interrupted anywhere is finished by the next one.

| Resource | Identity | Notes |
|---|---|---|
| Signing key (`CdnSigningKey`) | row saved first; CloudFront name `mojo-fm-key-<pk>` is the CallerReference | RSA-2048, private half in the KMS envelope (`KMS_KEY_ID` required) |
| Key group | one per deployment, `mojo-fm-<hash>` | CloudFront allows 10 per account, 5 keys each |
| Origin Access Control | `mojo-fm-oac-<hash of bucket>` | s3, sigv4, always |
| Distribution | tag `mojo:fileman-bucket=<bucket>` | signed default behavior; one behavior per manager prefix (public unsigned, private signed), longest prefix first; GET/HEAD; HTTPS; CachingOptimized; response headers policy `FILEMAN_CDN_RESPONSE_HEADERS_POLICY` (SimpleCORS by default, see **CORS**) on every behavior |
| Bucket policy | Sid `AllowCloudFrontServicePrincipalReadOnly_<id>` | merged with a SourceArn condition; a `Principal "*"` statement is reported, never removed |
| KMS key policy | Sid `AllowCloudFrontDecrypt_<id>` | `kms:Decrypt` only; read-merge-write |

**State is per bucket.** `CdnDistribution` is one row per bucket; every S3
manager on that bucket delivers through it, including the per-user and
per-group managers `get_for_user`/`get_for_group` create (they copy the
system manager's secrets, which is why CDN state is not a manager setting).
Disable, rotate and revoke are therefore one row and every manager follows.
A manager opts out with its own or top-parent setting `cdn = "off"`.

**Public means deliverable through the CDN.** In CDN mode `is_public` no
longer writes a bucket policy or runs the S3 audit (which would read the
private bucket and rewrite `is_public`). Any change that must protect a
prefix — a new private manager, a public→private flip, a moved
`backend_url` — converges the cache behaviors in `on_rest_pre_save`, under a
lock on the bucket's `CdnDistribution` row, and refuses the save if
CloudFront refuses, so the edge never serves a prefix unsigned while the row
says private. That holds even when the CDN row is disabled or the manager is
opted out, because the distribution still serves the bucket. Private→public
converges after the write. The sync rewrites cache behaviors only — never
`Enabled`, the origin or a BYO distribution's own settings.

**Behaviors are planned fail-closed.** Every S3 manager on the bucket counts
(active or not, opted out or not); where two share a prefix, signed wins;
a prefix containing `*`, `?` or any character CloudFront could not match
literally gets no behavior (it falls under the signed default) and such a
path is refused on save; a child with the same visibility as its nearest
ancestor is covered by the ancestor (which keeps the count under
CloudFront's 25-behavior quota); a **private manager under a public
manager's prefix is refused** — whether CloudFront matches the encoded or
the decoded path decides if the longer signed pattern could be sidestepped,
and until a live probe settles that the layout is not allowed. A clone keeps
its original's visibility. A manager at the **top of the bucket** (no path of
its own) gets no behavior either: a private one falls under the signed
default; a public one would get unsigned links the signed default refuses, so
it keeps its S3 links instead (`uses_cdn` is false). `check` and `enable`
name each active, not opted-out one as an advisory `DRIFT`
`prefix.root_public` that does not stop enablement; give it a path of its own
(`s3://<bucket>/<prefix>`) to deliver it through the CDN. Because it no
longer counts as CDN-delivered, the S3 public-access audit applies to it as
it does to an opted-out manager: a personal or group manager at the root that
`reconcile_fileman_public_access` (or the lazy personal-manager audit) finds
private behind Block Public Access is recorded `is_public = False`. It is
then a private manager at the root, so it falls under the signed default:
once reloaded it delivers through the CDN with signed links. The instance
that ran the audit keeps its cached S3 resolution until it is reloaded. The
system manager is never reconciled.

**Links.** `S3StorageBackend.get_url` is the one seam: `expires_in=None`
is unsigned, otherwise a canned-policy signed URL for
`min(expires_in, FILEMAN_CDN_MAX_EXPIRES_IN)` seconds (cap default 90 days,
a validator-registered Setting), rounded up to the hour so links are stable
inside an hour. `File`/`FileRendition` pass the manager's
`cdn_url_expires_in` (default 7 days) for a private manager; the token
download route keeps its 3600 s; the `download_url` cache is never used in
CDN mode. Signing is local — the signer for a key is cached per process, so
a link costs one RSA signature, not a KMS decrypt. The `cdn` and
`cdn_url_expires_in` settings are writable by global file admins
(`manage_files` / `files`, or a superuser); anyone else sending them gets 403.

**CORS.** Every behavior of a distribution `enable` provisions carries the
response headers policy named by the global setting
`FILEMAN_CDN_RESPONSE_HEADERS_POLICY`: AWS's managed SimpleCORS
(`60669652-455b-4ae9-85a4-c4c02393f86c`) by default, another policy id, or
`"none"` for no policy (a validator-registered Setting; anything else is a
400). SimpleCORS answers a request that carries `Origin` with
`Access-Control-Allow-Origin: *`, so a page on another site (a kiosk) can
read CDN files with `fetch()`. On a provisioned distribution `check` reports
a different policy on the default behavior as `DRIFT`
`distribution.default_behavior.response_headers` and `enable` converges every
behavior; links keep working meanwhile. **After upgrading, re-run `enable`
once per bucket**: until then the policy is missing and that drift is
reported. A BYO distribution keeps whatever policy its own default behavior
has (no drift is reported for it), and its per-prefix behaviors copy it.
The visibility sync never reads the setting: it copies the live default
behavior's policy, so a bad setting can never refuse a manager save. Two
limits: SimpleCORS adds the header only when the request sends `Origin`, so
a browser that cached a copy loaded without it (a plain `<img>`) can reuse
that copy for a later `fetch()` and refuse it — pages that read files
cross-site must request every load with CORS (`crossorigin="anonymous"`), or
the deployment attaches its own policy that always sends the header.
Preflighted and credentialed requests are not covered: `OPTIONS` is not an
allowed method.

**Bring your own distribution.** `fileman_cdn --manager <id> byo
--distribution <id> --alias https://media.example.com` registers one you
created; `check` then validates origin, OAC, signed default, behaviors,
bucket policy, Block Public Access, Object Ownership and the KMS policy, and
prints each missing statement as the exact merged JSON. django-mojo never
adopts an untagged distribution on a bucket: that is a `MANUAL` finding.

**Limits worth knowing.**
- Objects under the AWS-managed `aws/s3` key cannot be served: its policy
  cannot be changed. `enable` reports `MANUAL` with the remedy — a
  dedicated media CMK (not `KMS_KEY_ID`, which wraps every secret the
  deployment holds) as the manager's `kms_key_id`, and re-encryption of
  existing objects.
- Block Public Access and Object Ownership are checked, not changed: turning
  them on bucket-wide breaks the raw-S3 public links of every manager opted
  out of the CDN, and of every public manager at the top of the bucket.
  `enable --harden` applies them after naming those managers.
- A signed link cannot be revoked before it expires except by revoking the
  key (`revoke-key`), which takes minutes to reach the edge and kills every
  link that key signed. Rotation (`rotate-key`) adds a key, switches signing
  to it and retires the old one once its last link has expired (the daily
  `retire_cdn_signing_keys` cron). Rotation refuses when the group already
  holds four keys.
- A CDN copy outlives a delete or a regenerated rendition until its TTL
  (24 h default); both call `backend.invalidate()` which queues a
  CloudFront invalidation (1,000 paths a month are free). Originals never
  change in place.
- `disable` reverts links immediately and leaves AWS as is; `--retire` sets
  the distribution disabled. Deleting it is a human step.
- `revoke-key` marks a key inactive only once CloudFront has confirmed the
  key group no longer trusts it; a refused update returns `status: false`.
- CloudFront public key names include the deployment slug; a name that
  already exists with a different public key is reported, never adopted.
- CloudFront is never granted the deployment's `KMS_KEY_ID`; a manager whose
  `kms_key_id` resolves to it is reported MANUAL.

**IAM, by mode** (exact action names; a denial reports the one to grant):
- links: `kms:Decrypt` on `KMS_KEY_ID`.
- `check`: `cloudfront:ListDistributions, GetDistribution, GetDistributionConfig, GetOriginAccessControl, ListOriginAccessControls, GetKeyGroup, ListKeyGroups, ListPublicKeys, ListTagsForResource`; `s3:GetBucketLocation, GetBucketPolicy, GetBucketPublicAccessBlock, GetEncryptionConfiguration, GetBucketOwnershipControls`; `kms:DescribeKey, GetKeyPolicy`; `sts:GetCallerIdentity`.
- `enable`: check + `cloudfront:CreateOriginAccessControl, CreatePublicKey, CreateKeyGroup, UpdateKeyGroup, CreateDistribution, TagResource, UpdateDistribution`; `s3:PutBucketPolicy`; `kms:PutKeyPolicy`; with `--harden` also `s3:PutBucketPublicAccessBlock, PutBucketOwnershipControls`. Attaching the response headers policy needs no further action: it is part of `CreateDistribution` / `UpdateDistribution`.
- cron and revoke: `cloudfront:GetKeyGroup, UpdateKeyGroup`; invalidation: `cloudfront:CreateInvalidation`; `--retire`: `cloudfront:GetDistributionConfig, UpdateDistribution`.

A fleet provisioned by `mojo.deploy.provision` carries all of this on its node
role: the `DjangoMojoFilemanCdn` statement grants the CloudFront actions, and
the setup statement's `s3:*` / `kms:*` cover the bucket and key policies. A
fleet whose node role was built elsewhere (OpenTofu, by hand) and then
narrowed needs the CloudFront actions added, plus `kms:GetKeyPolicy` and
`kms:PutKeyPolicy` on the media bucket's key; `check_cdn` names each missing
one as a denied IAM action.

Tests: `tests/test_fileman/26_test_cdn_links.py` and
`27_test_cdn_provision.py` (no AWS); the opt-in
`tests/test_fileman_cdn_live_slow_serial/` proves it on a real SSE-KMS bucket.

#### Public-access reconciliation

For a user-scoped S3 manager, `is_public` is a stored classification that must
agree with the storage service. FileManager performs a policy-level check when
`get_for_user()` provisions or retrieves a personal manager. The first real
file or rendition URL then supersedes policy-only evidence once with a stronger
object-level check. An existing manager can also enter directly through that
object-level path when one of its files is resolved after upgrade.

The internal `public_access_audit` field stores versioned evidence with one of
three statuses:

- `public` — anonymous access was conclusively established; `is_public` is repaired to `True` and unsigned URLs are allowed.
- `private` — anonymous access was conclusively denied; `is_public` is repaired to `False`.
- `unknown` — AWS could not establish either result. The stored `is_public` value is preserved. For a personal (user-scoped) manager, download behavior fails closed to a presigned URL. For a group or system manager, the stored `is_public` value alone decides whether links are signed.

The audit uses an anonymous HEAD probe after authenticated S3 access confirms a
real object exists. An anonymous 403 is enough to disprove manager-wide public
access. A successful probe proves only that one object is readable, so the
manager is classified public only when policy evidence also shows an
unconditional anonymous `s3:GetObject` allow covering the entire prefix, no
matching deny can override it, and effective bucket/account Public Access Block
settings do not restrict the existing policy. Before any object exists, that
same conservative policy evidence is used by itself. The audit never adds or
broadens bucket policy statements.

The policy evidence reads the bucket policy and the bucket and account Public
Access Block settings. It does not read object or bucket ACLs. These are the
policy shapes it classifies; anything not listed answers `unknown`:

| Bucket policy | Result |
|---|---|
| No policy | `private` |
| Transport-only Deny, no public Allow, HTTPS endpoint | `private` |
| Any other Deny that overlaps the prefix, with or without a public Allow | `unknown` |
| Public Allow covering the whole prefix | `public` |
| Whole-prefix public Allow and a transport-only Deny, HTTPS endpoint | `public` |
| Either of the two rows above with `RestrictPublicBuckets` on, bucket or account | `private` |
| Whole-prefix public Allow and a transport-only Deny, endpoint that is not HTTPS | `unknown` |
| Public Allow that is conditional or covers only part of the prefix | `unknown` |
| Allow written with `NotPrincipal`, `NotAction` or `NotResource` that may reach anonymous `s3:GetObject` on the prefix | `unknown` |
| Unreadable policy, a policy that is not valid JSON, a policy document or statement not in the standard form (see below) | `unknown` |
| Unreadable Public Access Block | `unknown` |

Valid JSON is not enough. The policy text is read strictly: a member written
twice in the same object, at any depth, answers `unknown`, because a JSON
reader keeps only the last one and the audit would then judge a different
document from the one stored. So does a number written as `NaN` or `Infinity`.
The document may hold only `Version`, `Id` and `Statement`, and `Statement`
must be there and not be an empty list.

Every statement must be an object in the standard policy form, and one
statement outside it makes the whole result `unknown`, whatever the other
statements say:

- only the elements `Sid`, `Effect`, `Principal`/`NotPrincipal`,
  `Action`/`NotAction`, `Resource`/`NotResource` and `Condition`;
- `Effect` exactly `Allow` or `Deny`;
- exactly one of each pair, where an action or resource is a string or a
  non-empty list of strings, and a principal is the string `*` or an object
  keyed by `AWS`, `Service`, `Federated` or `CanonicalUser` with a string or a
  non-empty list of strings;
- a `Condition`, when present, that is a non-empty object of operators, each a
  non-empty object of keys, each with a string, number or boolean or a
  non-empty list of them. An empty `Condition` object answers `unknown`.

A public Allow is `Principal` `*` (or `{"AWS": "*"}`) with an `Action` matching
`s3:GetObject`, no `Condition`, and a `Resource` ending in `*` whose literal
part is a prefix of the manager's own prefix.

The **transport-only Deny** is the one Deny that does not block a `public`
answer. It is a Deny whose `Condition` is exactly
`{"Bool": {"aws:SecureTransport": "false"}}`: one operator, named exactly
`Bool`, with that one key (its name compared without case) and the value
`false` as a string in any case, as the boolean `false`, or as a one-element
list of either. This is the statement storage provisioning
(`mojo/deploy/provision/storage.py`, `secure_transport_policy`) writes on every
bucket. It can refuse plain-HTTP requests only, so it cannot override anonymous
reads over HTTPS. The audit sets it aside only when the backend's own endpoint
is HTTPS, and records `transport_only_deny: true` in the stored evidence when
it does. `BoolIfExists`, a second key or operator, the value `true`, a minimum
TLS version rule, a source address rule and an unconditional Deny are all still
treated as able to override, and keep the result `unknown`.

Provisioning has its own looser matcher, `_has_secure_transport_deny`, which
only asks whether plain HTTP is denied at all. The audit does not use it: a
loose match is safe for that question and unsafe for this one, so the two are
kept separate on purpose.

Audit metadata carries a one-way fingerprint of the backend URL/type and
effective connection settings. Changing those FileManager inputs invalidates
the evidence. Normal reads do not use an hourly TTL and do not poll AWS after
the current evidence is established.

Bucket/account policy or Public Access Block changes made through AWS Console,
Terraform, or another path outside FileManager are not observable from the
model. Force a bulk policy refresh after such a change:

```bash
python manage.py reconcile_fileman_public_access
python manage.py reconcile_fileman_public_access --dry-run
python manage.py reconcile_fileman_public_access --groups
python manage.py reconcile_fileman_public_access --manager 12 --manager 40
```

The command audits each selected manager independently, reports
`public`/`private`/`unknown` totals, and continues if one manager fails. Dry-run
performs the read-only AWS inspection without changing `is_public` or audit
metadata. Which managers are selected depends on the arguments:

- No argument — active user-scoped S3 managers only. If active group-scoped S3
  managers exist, one line reports how many were not checked.
- `--groups` — also every active S3 manager that has a group, whether or not
  it also has a user.
- `--manager <pk>` (repeatable) — only the named managers, user-scoped or
  group-scoped. It overrides `--groups`.

Inactive managers and system managers (no user and no group) are never
checked. One named with `--manager` is reported as skipped with the reason, as
is a manager that is not S3 or does not exist. A reactivated group manager is
not re-checked on its own; run the command for it.

A user-scoped manager is corrected in both directions. A group-scoped manager
is only ever corrected from public to private. The command never turns a group
manager public, because a group manager's policy answer is never confirmed
against a real object, and a store kept private on purpose (a FileVault store,
or one under a world-readable prefix) would have its expiring signed links
replaced by permanent unsigned ones. Two notes mark the group managers the
command left for an operator:

- `(policy is public, is_public False kept)` — the policy allows anonymous
  reads but the manager is flagged private. Its links stay signed and keep
  working. Set `is_public` on the manager if it should be public.
- `(is_public True kept, not verified)` — the manager is flagged public and
  the check answered `unknown`. Its links stay unsigned and may be refused by
  the bucket. These are counted as `unverified_public` in the summary.

The summary line also reports `changed` (`would_change` in a dry run) and
`skipped`. On a bucket whose only Deny statement is the TLS-only one, the check
is conclusive only from the release that carries the TLS-only Deny fix; before
it, such group managers report `unknown`.

### Backend interface — `download(file_path, local_path)`

All backends used by the renderer pipeline (image, vector, video, audio, document) must implement:

```python
def download(self, file_path: str, local_path: str) -> None:
    """Copy the stored file at file_path to a local filesystem path."""
```

The renderers download the original file to a temp path before processing. The local backend (`FileSystemStorageBackend`) and the S3 backend both implement this. If you write a custom backend, implement `download()` or rendition generation will fail for that backend's files.

See `mojo/apps/fileman/backends/` — each backend inherits `BaseStorageBackend`.

## Initiated Upload Flow

```python
# The REST endpoint resolves and authorizes the manager, validates policy,
# creates the File, and returns a normalized upload target.
POST /api/fileman/upload/initiate

# The client transfers to the returned target, then confirms exactly once
# (retries are safe):
POST /api/fileman/file/<id>  {"action": "mark_as_completed"}
```

Local and cloud transfers share this lifecycle. A successful transfer leaves
the `File` in `uploading`; only `mark_as_completed` validates the stored object,
sets `completed`, revokes the local upload token, and publishes rendition work.
Completion is lock-protected and side-effect-idempotent.

Initiation validates a normalized basename, a nonnegative integer size,
extension policy, and exact/top-level-wildcard MIME policy. Local uploads are
also bounded while streaming and checked against their actual byte count and
detected content type. Validation failure removes any partial storage object
and moves the row to `failed`.

An optional idempotency key may contain 1–128 ASCII letters, digits, `.`, `_`,
`:`, or `-`. The raw key is never stored: an internal `UploadInitiation` stores
only a digest, fingerprint, actor, and `File`. Same actor/key/fingerprint
replays the same file. Only `uploading` replays receive a refreshed target;
`completed`, `failed`, and `expired` replays return lifecycle state without a
writable capability. Omitting the key always creates a distinct file.

## Multiple FileManagers per Group

A group can have multiple named FileManagers (e.g., `"avatars"`, `"documents"`). Specify by name:

```python
fm = FileManager.get_for_user_group(user, group, use="avatars")
```

REST clients pass `?use=avatars` to select a specific manager.

## Auto-Provisioning

If no `FileManager` exists for a group or user, the system-wide default is used
automatically. Set `is_default=True` on one FileManager to designate it. A new
user manager inherits the system manager's public/private value and reconciles
its derived S3 prefix before it is returned.

A new group manager created by `get_for_group()` follows the same contract: it
inherits the system manager's public/private value and its S3 prefix is checked
once, at creation. A conclusive check repairs `is_public` in either direction.
An `unknown` check keeps the inherited value. An existing group manager is
returned as stored and is not re-checked on read.

The inherited value is the operator's setting on the system default manager.
That setting is not itself checked automatically.
`reconcile_fileman_public_access` checks user-scoped managers by default and
group-scoped managers when run with `--groups` or `--manager <pk>`. It never
checks system managers.
