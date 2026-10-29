# MojoSec Default Incident Policy

MojoSec has its own central RuleSets; it does not depend on an OSSEC request
to install them. On deployments running this implementation, the incident
app seeds missing defaults after migration on the `default` database only,
when routing permits both RuleSet and Rule and their current schema is ready.
`RuleSet.ensure_default_rules()` and validated sensor ingestion also ensure
the defaults exist. Importing the app performs no policy writes.

The policies are fleet-wide database policy, not per-sensor configuration.
Automatic setup is create-only: it preserves edited and disabled defaults.
If a category contains another operator policy, setup preserves that category
without installing new defaults that could preempt it. Defaults are governed
aggregates, marked with `metadata.mojosec_default_version = 1`; use the
revision-bound [Admin Security action authority](README.md#administrative-policy-authority)
to change or disable them. Disable a default to retain an override; deleting
it can cause the next setup pass to recreate it.

## Behavior

| Incoming evidence | Default result |
|---|---|
| Public-key SSH success with a validated user/source and no recent trusted SSH failure from that source | Ignore for incident policy |
| Exact proven local PAM lifecycle or the expected app-user firewall-broker sudo | Local-only receipt where applicable; routine policy also recognizes the exact match |
| `fim.change` with a bounded expected-change annotation and a live central deployment registration for the same installation key | Ignore for incident policy |
| `web.probe` with status 300–499 | Ignore unless the repeated-scanner rule matches first |
| At least three trusted `web.probe` receipts from one source on at least three distinct sensitive paths in ten minutes | Request a one-hour fleet-wide block through the existing checked block handler |
| Other MojoSec kinds, including unproven sudo, service/SSH failures, OOM, FIM overflow/errors, and 2xx/5xx probes | Create or bundle a visible review incident; no default notification or block handler |

Routine, expected-change, redirected/refused-probe, and scanner defaults require
an Event level at most 8. A higher-level Event reaches review instead. The
review policy exists for every server-known kind and `mojosec.unrecognized`.
The scanner rule has priority 5, routine/ignore rules 10, and review rules 100.

Ignoring through a RuleSet retains the Event and receipt and avoids creating
an Incident. The exact local-only compatibility path instead retains its
eventless receipt as described in the [sensor guide](mojosec_sensor.md).
Generic command names, host-supplied flags, and response-class claims do not
prove routine activity. Broker compatibility requires the exact
`app_user_firewall_broker_v1` classifier, the exact broker `command_path`, and
untruncated command evidence; interactive or other near matches remain visible.

The ten-minute context is bounded to the newest 256 matching receipts. Scanner
evidence requires separate validated receipts, not a sensor's aggregate
`count` or repeated delivery of one ID. Both receipt creation and sensor
observation must be fresh within ten minutes, with at most five minutes of
future sensor clock skew. The governed default block job rechecks the scanner
predicate when it executes, so queued stale evidence cannot authorize a block.
If that recheck fails, the existing incident remains visible. A newly received
stale 3xx/4xx probe still follows the ignore policy and retains its Event audit.
Sensitive paths are `.env`/`.env.*`
segments, recognized `.git` internals, and `vendor/phpunit/.../eval-stdin.php`.
Paths are compared case-insensitively after the safe public projection. A
successful response, 5xx response, user agent, or one suspicious path cannot
authorize this default block. A checked-handler request is not proof that
every expected firewall host applied it; the block handler resolves the
incident only when the checked result is verified and reports `ok=true`.

Host review defaults bundle by `MODEL_NAME_AND_ID`; web review and scanner
defaults bundle by `SOURCE_IP`. Every default uses a 60-minute bundle window
and keeps bundles separate per RuleSet. Newly projected sensor Events have
`model_name="mojosec_sensor"` and `model_id` equal to the installation API key's
primary key, so different sensors do not merge into a host bundle merely
because application workers share a hostname. `hostname` keeps its existing
meaning; historical Events are not rewritten.

## Reserved rule fields

The governed field schema exposes these typed operands. They are computed
from one integrity-checked, protocol-valid durable receipt linked to the
Event, not from arbitrary Event metadata or browser/sensor-supplied policy
flags. Receipt identity, payload digest, category, canonical source, and any
populated Event installation identity must agree. Missing, ambiguous, altered,
or unavailable proof resolves to no value and cannot grant an ignore or
automatic block.
Legacy policy using these reserved names receives the same protected resolver.

| Field | Type | Meaning |
|---|---|---|
| `mojosec_kind` | `str` | Validated sensor event kind |
| `mojosec_user` | `str` | First validated literal identity from user, actor, or target user |
| `mojosec_auth_method` | `str` | Projected authentication method, e.g. `PUBLICKEY` |
| `mojosec_path` | `str` | Safe normalized path |
| `mojosec_status` | `int` | Validated HTTP status |
| `mojosec_response_class` | `str` | Projected response class; alone proves neither safety nor compromise |
| `mojosec_command_path` | `str` | Accepted executable path |
| `mojosec_proof_status` | `str` | Projected proof label; alone does not prove a routine command |
| `mojosec_expected_change` | `bool` | Bounded FIM annotation plus live central registration |
| `mojosec_routine` | `bool` | Complete server-defined SSH, local-operation, or expected-FIM proof |
| `mojosec_scanner_confirmed` | `bool` | Bounded distinct-receipt/distinct-sensitive-path scanner threshold |

These are policy operands, not new top-level Event fields or sensor protocol
fields. The existing batch/ack version and REST URLs remain unchanged.

## Preview and apply

Run against the intended application database. The command previews by
default and returns `revision`, `create`, `retire`, and `preserved`:

```bash
python manage.py mojosec_rules
python manage.py mojosec_rules --apply --expected-revision <revision>
```

Use the explicit repair flag to replace only the exact old WMWX visibility
defaults. Preview and apply must use the same flags and revision:

```bash
python manage.py mojosec_rules --repair-visibility
python manage.py mojosec_rules --repair-visibility --apply --expected-revision <revision>
```

Repair recognizes only the exact `WMWX mojosec.<kind>` name, category,
visibility metadata, priority, empty handler/rules/trigger fields, and old
hostname-bundle shape. It disables matching active legacy rows and creates
missing governed defaults. Customized or mixed categories stay preserved.
Concurrent policy changes invalidate the preview revision; preview again
before applying. The command never deletes Events, Incidents, or receipts,
does not replay historical Events, and does not change sensor enrollment.
