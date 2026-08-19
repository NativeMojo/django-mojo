"""Provision an AWS environment from an empty account, idempotently.

This package OBSERVES an account against a declared topology and CONVERGES it
toward that topology: it creates what is missing, reports what has drifted, and
never deletes anything. It ships with no callers of its own — the bootstrap CLI,
the node sequence and the portal Setup section are all consumers of the same
functions, so every one of them sees exactly the same account.

    from mojo.deploy.provision import discover, plan, spec

    topology = spec.build("wmx", "prod", "us-west-2", preset="small")
    clients = discover.Clients()

    findings, actions, results = plan.observe(clients, topology)   # read-only
    findings, actions, results = plan.apply(clients, topology)     # converge

THE CONTRACT FOR THIS PACKAGE — on top of `mojo/deploy/`'s own (see
`mojo/deploy/__init__.py`), all of which still applies:

    - No Django, and no `mojo.helpers.*`. This code runs from a laptop against
      an account that has no django-mojo installed anywhere yet, so there are no
      settings to read and `mojo.helpers.logit` would raise on import.
    - boto3 is imported lazily, inside functions, for the same reason
      `mojo/deploy/` does it: importing this package must stay free.
    - NOTHING HERE PRINTS. Every function returns data — findings, actions and
      resolved values. Rendering belongs to the caller, because the CLI renders
      to a terminal and the portal renders to JSON, and a `print()` in here
      would corrupt the second one.
    - No delete, terminate, destroy, deregister, revoke or remove. Ever. The
      `Clients` proxy in `discover.py` refuses those method names at runtime, and
      a source scan in `tests/test_deploy/provision_ensure.py` refuses them at
      review time. Converging an account is additive; tearing one down is a
      deliberate human act performed elsewhere.
    - No boto3 waiters. RDS, ElastiCache and NLBs take five to fifteen minutes
      to become usable, and a waiter would either hang the CLI or need every
      poll stubbed in tests. A resource that is still coming up is a PENDING
      finding; the steps that need it are SKIPPED and picked up by the next run.
    - There is no state file. Resume is re-observation. Running `apply` twice
      against a converged account creates nothing the second time, and that is
      the property the whole design is arranged around.

WHY A SECOND AWS HELPER LAYER EXISTS

`mojo/helpers/aws/` already wraps boto3 for the running application, with a
`ProviderCaller` seam that labels every call `mutation=True`/`False` and maps
provider errors into a uniform shape. None of it is reachable from here: it
lives under `mojo.helpers`, which this package may not import. That is the only
reason for the duplication, and it is not an invitation to merge the two — the
one thing that IS shared is the convention. Every mutating call in this package
is labelled the same way `ProviderCaller` labels one, so a reader moving between
the two layers reads the same vocabulary.

THE MODULES

    spec            the topology as data — presets, names, tags, validation,
                    costs. Zero AWS calls; safe to import and inspect.
    report          Finding / Action / Result / Report, and `safe()`, which
                    turns a permission failure into a BLIND finding instead of
                    a traceback.
    discover        the injectable `Clients` container and `observe()`, which
                    reads the account's raw shape with no judgement attached.
    network         VPC, subnets, gateway, routes, endpoint, security groups.
    identity        EC2 key pair, node role + inline policy, instance profile.
    storage         config bucket, bootstrap secrets, the stage-1 payload.
    data            Aurora PostgreSQL cluster, Valkey replication group.
    nodes           AMI resolution, EC2 instances, per-node EIPs.
    balancer        NLB, target groups, listeners, target registration.
    observability   CloudTrail, GuardDuty, the CloudWatch log groups.
    dns             optional hosted zone and A records.
    plan            the DAG that orders all of the above, and the two entry
                    points `observe()` and `apply()`.

AND THE OPERATOR-FACING HALF, which consumes all of it and creates nothing of
its own:

    inputs          the eight questions, the committed `aws/environments/
                    <env>.json`, and `infrastructure_mode()` — the fail-closed
                    accessor the node-configuration step writes verbatim into
                    `django.conf`.
    clients         the boto3 session factory (`--profile` / `--role-arn` /
                    ambient) that puts REAL clients in the `Clients` container.
                    The one file here that decides which credential a run uses.
    __main__        `python3 -m mojo.deploy.provision init|apply|status`. Asks,
                    previews, prices, confirms, renders. Every AWS mutation it
                    causes belongs to `plan.apply()`.

There is deliberately NO `[project.scripts]` entry point, for the same reason
the rest of `mojo/deploy/` has none: a console script reintroduces a dependency
on wherever pip put the script directory, and skeleton nodes run no virtualenv.
`python3 -m` resolves against the interpreter that has django-mojo installed,
which is the only thing that is reliably true everywhere.
"""
