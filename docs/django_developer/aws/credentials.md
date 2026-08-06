# AWS Credentials

Every AWS client in django-mojo is built from a session produced by
`mojo/helpers/aws/client.py`. There are two factories and three credential
modes.

```python
from mojo.helpers.aws.client import get_session, get_assumed_session
```

---

## The three credential modes

| Mode | Configure | Who you act as |
|---|---|---|
| **Static keys** | `access_key` + `secret_key` (or a manager's `aws_key`/`aws_secret`) | The IAM user those keys belong to |
| **Ambient** | configure nothing | Whatever botocore's default chain resolves |
| **AssumeRole** | `assume_role_arn` (+ optional `external_id`) | The role, in whatever account owns it |

### Ambient credentials already worked

This is worth stating plainly because it is easy to assume otherwise:
**passing no keys has always fallen through to botocore's default credential
chain.** `get_session` only calls into `boto3.Session` with credential kwargs
when they are actually set; an all-`None` call constructs a plain
`boto3.Session()`, which resolves credentials exactly the way the AWS CLI does.

What is new is `get_assumed_session` (cross-account `sts:AssumeRole`) and the
consolidation of the fileman S3 backend onto a single credential path.

### The default chain order

botocore resolves, in order:

1. Explicit `aws_access_key_id` / `aws_secret_access_key` passed to the session
2. Environment (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`)
3. Shared credentials file (`~/.aws/credentials`, selected by `profile`)
4. Shared config file (`~/.aws/config`)
5. Container credentials (ECS/Fargate task role)
6. **Instance metadata (IMDS) — the EC2 instance profile**

So an EC2 instance with an instance profile, or an ECS task with a task role,
needs no configuration at all. Leave the keys unset.

---

## `get_session`

```python
get_session(access_key=None, secret_key=None, region=None, profile=None)
```

Builds a `boto3.Session`. Two behaviors matter:

- **All-`None` is a supported call.** It returns a session that uses the default
  chain above.
- **A half-configured pair fails immediately.** Supplying an access key with no
  secret (or the reverse) raises `botocore.exceptions.PartialCredentialsError`
  before any AWS call, rather than silently falling through to a *different*
  identity from the default chain. That silent fallthrough is the failure mode
  this guard exists to prevent — a typo'd secret should not quietly promote the
  process to whatever the instance profile can do.

## `get_assumed_session`

```python
get_assumed_session(role_arn, region=None, external_id=None, session_name=None,
                    duration=None, access_key=None, secret_key=None, profile=None)
```

Returns a session whose clients act as `role_arn`.

- The **source identity** — the one that calls `sts:AssumeRole` — is resolved by
  `get_session` with the same arguments. That means the source may itself be
  static keys, a named profile, or an instance profile. The half-pair guard
  still applies.
- If no source identity can be resolved at all, it raises `NoCredentialsError`
  up front instead of failing later inside the credential fetcher.
- Credentials **refresh automatically**. The session is given a
  `DeferredRefreshableCredentials`; botocore re-assumes the role shortly before
  expiry. Do not cache a client and assume it will keep working "because it was
  created recently" — that is botocore's job, and it does it.
- `ExternalId` is sent **only** when one is configured. STS rejects a null or
  empty `ExternalId`, so it is omitted rather than passed as `None`.
- `session_name` defaults to `DEFAULT_ROLE_SESSION_NAME` (`django-mojo`).
  Callers should pass something more specific — it lands in the tenant's
  CloudTrail as `arn:aws:sts::…:assumed-role/<role>/<session_name>`, and it is
  also part of the process-wide assume-role cache key (see below).
- `duration` defaults to `DEFAULT_ROLE_DURATION`, **12 hours** — deliberately not
  the 1-hour STS default. A presigned S3 URL signed with temporary credentials
  stops working when those credentials expire, whatever its own `ExpiresIn`
  says, so a short role duration silently truncates every signed URL. The role's
  own `MaxSessionDuration` must allow the requested duration or the
  `AssumeRole` call fails.

### The shared cache

Assumed credentials are cached in a module-level dict shared by every fetcher in
the process. botocore's cache key hashes `RoleArn`, `ExternalId`,
`DurationSeconds` and `RoleSessionName` — but **not** the source credentials.
Two callers with different source identities and otherwise identical role
arguments would therefore share a cache entry. Keep this safe by deriving
`session_name` per-owner; the fileman backend uses the `FileManager` primary
key for exactly this reason.

---

## Cross-account recipe

The tenant owns the bucket and never gives you keys. You get a role you can
assume, guarded by an external ID.

**1. In the tenant's account** — the role's trust policy names your account as
the principal and requires the external ID:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::111111111111:role/mojo-platform" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "sts:ExternalId": "a-long-random-value" }
      }
    }
  ]
}
```

The role's own permission policy grants whatever the workload needs
(`s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on the prefix, plus
`s3:GetBucketPolicy` / `s3:PutBucketCors` if the manager should administer its
own bucket policy and CORS).

**2. In your account** — the source identity must be allowed to assume it:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::222222222222:role/tenant-mojo-access"
    }
  ]
}
```

**3. In django-mojo:**

```python
session = get_assumed_session(
    "arn:aws:iam::222222222222:role/tenant-mojo-access",
    region="us-east-1",
    external_id="a-long-random-value",
    session_name="mojo-tenant-42",
)
s3 = session.client("s3")
```

### Why the external ID matters

Without it, the trust policy says "anyone in account 111111111111 may assume
this role." If that account is a shared platform — and it is, since it serves
every tenant — then a tenant who learns another tenant's role ARN can ask the
platform to use it. The external ID is the *confused deputy* control: it is a
value the tenant chooses and the platform stores per-tenant, so knowing the role
ARN alone is not enough to get the platform to assume it. Treat it as a secret:
it is never returned by any REST graph, only its presence is.

---

## Where this is wired up

| Consumer | Doc |
|---|---|
| fileman S3 backend (`assume_role_arn` on a `FileManager`) | [../fileman/file_manager.md](../fileman/file_manager.md) |
| `aws_check` bootstrap/audit | [aws_check.md](aws_check.md) |
| CloudWatch helper | [cloudwatch.md](cloudwatch.md) |
