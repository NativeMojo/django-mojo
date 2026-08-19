# Admin Metrics — CloudWatch charts without the AWS console

The Metrics route inside the Platform feature charts CloudWatch time series for
the EC2, RDS, and ElastiCache resources the installation's AWS identity can
see. It is a read-only surface: it discovers resources, fetches a metric, and
draws it. It never writes to AWS.

## Capability flow

Metrics is gated by one grant, and it travels through three named layers rather
than being read off a raw permission in the browser:

1. `mojo/apps/account/rest/admin_portal.py` publishes
   `capabilities["manage_aws"] = has(["manage_aws"])` in the bootstrap payload.
   `User.has_permission` already short-circuits superusers, so no `admin`
   alias is listed.
2. `mojo/apps/account/services/admin_features/platform.py` turns that into the
   feature lane: `capabilities["metrics"] = bool(capabilities.get("manage_aws"))`.
   The Platform feature's `enabled` flag stays `any(values.values())`, so an
   operator holding only `manage_aws` gets the Platform namespace with the
   Metrics entry and nothing else.
3. The browser reads `ctx.features.platform.capabilities.metrics` — never
   `ctx.capabilities.manage_aws`. `assets/features/platform/feature.js` gates
   both the sidebar entry and the page on it; `metrics.js` re-checks it first
   and answers `permissionDeniedState` when it is absent.

The REST endpoints themselves are gated independently by
`@md.requires_global_perms("manage_aws")`, so hiding the lane is a UI courtesy,
not the security boundary.

## Degradation envelope

`mojo/apps/aws/rest/cloudwatch.py` is the only file that classifies provider
failures. An installation with no AWS credentials, a refused IAM identity, or an
unreachable endpoint is an ordinary state for this page, not a server error, so
both endpoints answer **200** and describe the situation.

Classification runs through `mojo.helpers.aws.provider_call.map_error`, and the
reason codes are that module's vocabulary verbatim:

| `reason` | Raised by |
|---|---|
| `credentials_unavailable` | `NoCredentialsError`, `PartialCredentialsError` |
| `network_unavailable` | `ConnectTimeoutError`, `EndpointConnectionError`, `ReadTimeoutError` |
| `denied` | a `ClientError` whose code is a denial, or any HTTP 403 |
| `service_error` | any other `ClientError` / `BotoCoreError` |

Anything that is **not** a botocore exception is not a provider failure — it
propagates and still returns 500. A swallowed `TypeError` would look like an AWS
outage forever, so the discriminator is the exception type, not a bare `except`.

Raw provider text never reaches the wire. Only `ProviderCallError.detail()` —
operation, provider code, retryability, request id — is written to `aws.log`,
because botocore messages can carry credentials, signed URLs, and request
parameters.

`/aws/cloudwatch/resources` reads the three services independently, so one
refused service still lists the other two:

```json
{"status": true, "ec2": [...], "rds": [], "redis": [...],
 "degraded": {"rds": "denied"}, "available": true, "reason": null}
```

`available` is false only when **every** service failed, and `reason` then names
the single cause (most actionable first, since all three share one session).

`/aws/cloudwatch/fetch` carries the same two flags **inside** `data`:

```json
{"status": true,
 "data": {"data": {}, "labels": [], "available": false, "reason": "denied"}}
```

That nesting is deliberate. The Admin portal's `api()` helper returns
`payload.data ?? payload`, so a top-level sibling of `data` would be silently
dropped before the page ever saw it. Parameter validation is unchanged and still
raises a 400.

The helper is constructed with `max_attempts=1` (through `get_client`, not by
editing the helper): botocore's default of three retries turns an unreachable or
unauthorized endpoint into a multi-second wait, and a page whose whole job is to
say "AWS is not answering" has to say it quickly.

## Browser modules

`assets/features/platform/chart.js` is a dependency-free SVG line chart —
`lineChart({labels, series, unit, stat, timeRange, ariaLabel})` returns
`{node, dispose}`. It builds every element with `document.createElementNS` and
writes every string with `textContent`; there is no `innerHTML` anywhere,
because EC2 series names are Name-tag values that anyone with tag rights
controls. The x axis is computed client-side from the requested range and
granularity rather than from the server's period labels, which lose the day as
soon as a range crosses midnight. Series colours cycle `--chart-1`…`--chart-6`,
declared in all three theme blocks in `assets/features/platform/styles.css`.

`assets/features/platform/metrics.js` owns the page: capability gate, resource
sections built from the shared `components/rows.js` builders, the control row,
the fetch, and the deep-link state. It holds its own `AbortController` so a
control change cancels the in-flight request, and it aborts on `dispose()`.

## Preview states

`bin/admin_preview` renders the page deterministically with
`--metrics-state live|empty|unconfigured|denied|partial`:

| State | What it proves |
|---|---|
| `live` | Two EC2 instances, one RDS, one ElastiCache; deterministic sawtooth series |
| `empty` | The same resources with an all-zero series and the "no non-zero datapoints" caption |
| `unconfigured` | Whole-page degradation with `credentials_unavailable` |
| `denied` | Whole-page degradation with `denied` |
| `partial` | EC2 and ElastiCache live, RDS degraded — the quiet "Unavailable right now" line |

`bin/admin_preview_support/features/platform.py` answers both CloudWatch paths
from a small route switch, and its bootstrap grants `manage_aws` so the lane is
always visible in the fixture.

## Tests

- `tests/test_aws/cloudwatch.py` — the degradation envelope, with a stubbed
  helper installed through the module's `_get_helper` seam. These call the view
  body in-process (`__wrapped__`) because `opts.client` talks to a separate
  server process a stub could never reach; the permission gate is covered by the
  HTTP tests in the same file.
- `tests/test_account/test_admin_portal_assets.py` — the capability string, both
  endpoint paths, the markup-free chart, and the dark-theme palette.
- `tests/test_account/test_admin_preview.py` — all five preview states.
