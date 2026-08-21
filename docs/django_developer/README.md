# Django Developer Reference

LLM-optimized reference documentation for building Django applications with the django-mojo framework.

Companion track for REST integrators: [docs/web_developer/README.md](../web_developer/README.md).

## Framework Core

| Section | Description |
|---|---|
| [core/](core/README.md) | MojoModel, REST framework, decorators, middleware, serialization |
| [core/db_reader.md](core/db_reader.md) | Optional database-reader routing, request pinning, and explicit primary/reader scopes |
| [helpers/](helpers/README.md) | logit, dates, settings, settings reference keys, crypto, request, response, redis, and other utilities |
| [deploy/](deploy/README.md) | Node deployment tooling that runs before Django settings exist — S3 config sync, the read-only AWS account audit, foreground job engine control (`jobman`), and node convergence (`node_setup`) |
| [deploy/provision.md](deploy/provision.md) | Account provisioning — eight prompts take an empty AWS account to a running environment, with a committed environment declaration, a priced preview and an idempotent converge that never deletes |

## Built-in Apps

| Section | Description |
|---|---|
| [account/](account/README.md) | User, Group, JWT authentication, permissions, push notifications |
| [security/](security/README.md) | Security system — incidents, events, rules, handlers, LLM agent, firewall, bouncer integration, health monitoring |
| [security/mojosec_sensor.md](security/mojosec_sensor.md) | Settings-free EC2 host sensor — targeted journal/nginx/FIM signals, durable aggregation, and batched incident delivery |
| [logging/](logging/README.md) | Database logging (logit app), security incidents, fleet-wide IP blocking |
| [fileman/](fileman/README.md) | File upload, storage backends, renditions |
| [email/](email/README.md) | AWS SES email, templates, mailboxes, inbound handling |
| [aws/credentials.md](aws/credentials.md) | AWS credential modes — static keys, ambient/instance-profile, and cross-account AssumeRole |
| [aws/cloudwatch.md](aws/cloudwatch.md) | CloudWatch monitoring — EC2, RDS, and ElastiCache metrics via boto3 |
| [aws/load_balancer.md](aws/load_balancer.md) | Read-only ELBv2 serving-tier health — balancer, attached target groups, registered-target health, elastic IPs, and bounded denial reporting |
| [aws/guardduty.md](aws/guardduty.md) | GuardDuty findings via EventBridge/SNS — severity policy, dedupe and occurrence contract, bounded metadata, opt-in RuleSet |
| [aws/aws_check.md](aws/aws_check.md) | Audit and safely bootstrap AWS credentials, cron, S3, SES, SNS/CloudWatch, dnsman ACME/certificate state, and incident defaults |
| [aws/s3.md](aws/s3.md) | Bounded S3 inventory, verified public/private posture, private creation, and confirmed version-aware emptying |
| [aws/version_drift.md](aws/version_drift.md) | Daily RDS/Aurora and ElastiCache major-version drift inventory, support deadlines, and the opt-in ticket/board escalation |
| [aws/infra_drift.md](aws/infra_drift.md) | Daily read-only fleet drift scan — what is actually serving traffic vs. the fleet recorded in `EDGE_EXPECTED_TOPOLOGY`, in both directions, as operator-facing prose |
| [aws/maintenance.md](aws/maintenance.md) | Applying a pending RDS/ElastiCache engine upgrade and the django-mojo framework update — server-derived targets, single flight, and the permission AND |
| [aws/capacity.md](aws/capacity.md) | Adding and removing an app node, an RDS reader, or a cache replica from the portal — server-derived guards, the clone-and-prove join sequence, and what stays an operator task |
| [aws/infrastructure_mode.md](aws/infrastructure_mode.md) | `INFRASTRUCTURE_MODE` — the file-only switch that disables portal infrastructure mutations on installations whose AWS estate is applied by external IaC |
| [aws/admin_email.md](aws/admin_email.md) | Admin portal email management — the locked mailbox-default writer, the posture/summary/dashboard services, and the never-500 test send |
| [dnsman/](dnsman/README.md) | Domains, DNS records across providers, WHOIS/privacy, and centrally-issued TLS certificates |
| [edge/](edge/README.md) | nginx vhosts as structured rows, and the node-side installer that lands certs + vhosts as one validated generation |
| [jobs/](jobs/README.md) | Async job queue — publishing, scheduling, retries, webhooks, broadcasting, monitoring |
| [metrics/](metrics/README.md) | Redis-backed time-series metrics |
| [chat/](chat/README.md) | Real-time chat rooms, messages, reactions, moderation |
| [realtime/](realtime/README.md) | WebSocket pub/sub via Django Channels |
| [phonehub/](phonehub/README.md) | Phone and device management |
| [shortlink/](shortlink/README.md) | URL shortener with OG previews, file links, and click tracking |
| [filevault/](filevault/README.md) | Encrypted file vault |
| [docit/](docit/README.md) | Documentation system |
| [assistant/](assistant/README.md) | LLM admin assistant — architecture, tools, extensibility, custom tool registration |
| [assistant/approvals](assistant/approvals.md) | The approval boundary in front of every mutating assistant tool — `PendingAction`, the gate arguments, the transports, the audit trail |
| [github/](github/README.md) | GitHub App integration — GitHubInstall model, JWT/token service, webhook verification, OAuth login |
| [testit/](testit/Overview.md) | Testing framework and patterns for django-mojo projects |
| [testit/Isolation](testit/Isolation.md) | Per-checkout test isolation — how two worktrees run suites without colliding |

## Quick Start Pattern

Every feature follows the same pattern:

```python
# 1. Model (app/models/my_model.py)
from django.db import models
from mojo.models import MojoModel

class MyModel(models.Model, MojoModel):
    user = models.ForeignKey("account.User", null=True, on_delete=models.SET_NULL)
    group = models.ForeignKey("account.Group", null=True, on_delete=models.SET_NULL)
    name = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class RestMeta:
        VIEW_PERMS = ["view_mymodel", "owner"]
        SAVE_PERMS = ["manage_mymodel", "owner"]
        GRAPHS = {
            "list": {"fields": ["id", "name", "created"]},
            "default": {"fields": ["id", "name", "created", "modified"]},
        }

# 2. REST endpoint (app/rest/my_model.py)
import mojo.decorators as md
from ..models.my_model import MyModel

@md.URL('mymodel')
@md.URL('mymodel/<int:pk>')
def on_my_model(request, pk=None):
    return MyModel.on_rest_request(request, pk)
```

## Key Conventions

- Always use `request.DATA` (never `request.POST.get()` or `request.GET.get()`) — it is an [`objict`](helpers/objict.md) with attribute access and dot-notation nested keys
- View functions return **plain dicts** — never construct `JsonResponse` manually (see [Return Values](core/decorators.md#return-values))
- List endpoints must NOT end with trailing slashes
- **URL auto-prefix is the Django app directory name** — `@md.URL('session')` in app `wallet` → `/api/wallet/session`. Never use a parent package name.
- **Dynamic URL segments go at the END only** — `book/<int:pk>` is correct; `book/<int:pk>/chapters` is never correct. Use query params or POST data for nested lookups.
- **Use `POST_SAVE_ACTIONS` for model operations** — prefer `on_action_<name>` over dedicated REST endpoints when acting on a specific instance
- Model inheritance: `models.Model, MojoModel` (or `MojoSecrets, MojoModel`)
- Always include `created` and `modified` fields
- Add `user` and `group` FKs when model needs access control
- Never create migration files (user's responsibility)
- Never use Python type hints
