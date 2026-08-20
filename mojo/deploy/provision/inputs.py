"""The eight questions, the file the answers live in, and how to read it back.

An operator answers eight things once. Everything else — every VPC name, every
bucket, every log group, every instance count — is derived from those answers by
`spec.py`, which is why there is no ninth question and no free-text override of a
resource name anywhere in this file.

WHY THE ANSWERS ARE A COMMITTED FILE and not a hidden dotfile. `aws/environments/
<env>.json` sits next to the project, in git, and is reviewed like code. It is
the declaration of what the environment IS, and three different things read it:
this CLI, the node-configuration step, and a human trying to work out why prod
looks the way it does. A per-user file in `~` would make the answer to "what is
prod?" depend on whose laptop you asked.

WHICH IS EXACTLY WHY NOTHING SECRET MAY EVER LAND IN IT. `save()` enforces a
STRICT ALLOWLIST — the documented schema keys and nothing else. Not a denylist of
secret-shaped names: a denylist misses `bootstrap_token`, and it false-positives
on an honest future field like `ssh_key_pair_name`. Generated secrets live in
`bootstrap-secrets.json` in the config bucket, written by `storage.py`, read back
by the node. They are never asked for here and never written here.

THE ONE TENSION, AND HOW IT IS RESOLVED. `init` over an existing file must not
silently drop a key a newer django-mojo wrote, and `save()` must not be a way to
get an arbitrary key into the file. Both hold, because preservation is bounded to
bytes that were ALREADY ON DISK:

    answers = load(path)                       # everything in the file
    keep    = unrecognized(answers)            # only what this version cannot name
    save(path, schema_only(answers), preserved=keep)

`save()` refuses any unknown key in its `answers` argument outright, and only
writes `preserved` back verbatim. A prompt cannot reach `preserved` — it is
sourced from a prior `load()` of the same file and from nowhere else — so no
value an operator types can enter the file under a name this version does not
recognize. `init` is therefore non-destructive without the allowlist being a
suggestion.

INFRASTRUCTURE MODE IS DUPLICATED HERE ON PURPOSE. `mojo/helpers/infrastructure.py`
owns the same two literals and the same fail-closed value table for the running
portal. This package may not import it — `mojo.helpers.*` is off-limits below
`mojo/deploy/` — so the rule is restated rather than shared, and
`tests/test_deploy/provision_cli.py` imports BOTH in one process and asserts they
agree on the whole value table. Duplicating a refusal is safe in the direction
that matters: the failure mode of drift here is a run that refuses when it did
not have to, never one that proceeds when it should not.
"""

import ipaddress
import json
import os
import re
import sys

from mojo.deploy.provision import spec as spec_module


# ── the file ────────────────────────────────────────────────────────────────

ENV_DIR = os.path.join("aws", "environments")

# Bumped when a field's MEANING changes, not when one is added. A reader that
# does not recognize the version refuses rather than defaulting the fields it
# does not know about — silently provisioning something other than what the file
# describes is the one outcome worth an error message.
SCHEMA_VERSION = 1

# The allowlist. Every key this tool will ever write, and the complete set it
# will accept in `save()`. Adding a field means adding it here, which is the
# review moment this list exists to create.
SCHEMA_KEYS = frozenset((
    "schema_version",
    "project", "env", "region",
    "apex_domain", "operator_email", "preset", "github_repo",
    "aws_profile", "role_arn",
    "admin_cidrs",
    "backups_days", "reader", "replica", "nlb", "stable_node_ips",
    "route53_zone", "staging",
    "infrastructure_mode",
))

# The subset that must be present and non-empty for `apply` to run at all.
REQUIRED_KEYS = ("project", "env", "region", "apex_domain", "operator_email",
                 "preset", "github_repo")


class EnvFileError(ValueError):
    """The environment file is absent, unreadable, or not what it claims.

    One exception type for every way the file can be wrong, because the CLI
    answers all of them the same way: name the path, say what is wrong, exit 2,
    never a traceback.
    """


# ── infrastructure mode ─────────────────────────────────────────────────────

MODE_KEY = "infrastructure_mode"
MANAGED = "managed"
EXTERNAL = "external"


def infrastructure_mode(answers):
    """Exactly ``MANAGED`` or ``EXTERNAL``, from the env file. Never raises.

    THE PUBLIC CROSS-ITEM CONTRACT. The node-configuration step writes what this
    returns into `django.conf`'s `INFRASTRUCTURE_MODE` verbatim — never a
    hardcoded `"managed"`. A run launched with `--override-external` still
    renders `external` if the file declares it, because the override is a
    property of one invocation and the file is a property of the environment.

    Unset, empty and `"managed"` are managed; `"external"` is external; anything
    else present — a typo, a number, a bool — is EXTERNAL. A switch whose whole
    job is to refuse must not be turned off by a spelling mistake. Byte-for-byte
    the same table as `mojo.helpers.infrastructure.infrastructure_mode`, which a
    test asserts in one process.
    """
    raw = (answers or {}).get(MODE_KEY)
    if raw is None:
        return MANAGED
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in ("", MANAGED):
            return MANAGED
        if value == EXTERNAL:
            return EXTERNAL
    return EXTERNAL


def mode_is_external(answers):
    return infrastructure_mode(answers) == EXTERNAL


# ── validation ──────────────────────────────────────────────────────────────

# A hostname label set, at least two deep. Deliberately permissive about the TLD
# (new ones appear constantly) and strict about the shape, which is what a
# fat-fingered "example.com." or "https://example.com" actually violates.
DOMAIN_RE = re.compile(
    r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")

# Not RFC 5322. A full parser here would accept things no ACME provider will,
# and the real check is that a human reads the address at the top of the
# preview.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# owner/name, the two segments a clone URL is built from.
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

# Region ids are stable in shape even as new ones ship: partition-ish prefix,
# a direction, a digit.
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")

WORLD = "0.0.0.0/0"

BACKUP_CHOICES = (7, 35)


def check_domain(value):
    if not DOMAIN_RE.match((value or "").strip().lower()):
        return (f"{value!r} is not a domain — give the apex, like "
                f"`example.com`, with no scheme and no trailing dot")
    return None


def check_email(value):
    if not EMAIL_RE.match((value or "").strip()):
        return f"{value!r} is not an email address"
    return None


def check_repo(value):
    if not REPO_RE.match((value or "").strip()):
        return (f"{value!r} is not a repository — give it as `owner/name`, "
                f"not a URL")
    return None


def check_region(value):
    if not REGION_RE.match((value or "").strip()):
        return (f"{value!r} is not an AWS region id — they look like "
                f"`us-west-2` or `eu-central-1`")
    return None


def check_preset(value):
    if value not in spec_module.PRESETS:
        return (f"{value!r} is not a size — one of "
                f"{', '.join(spec_module.PRESET_NAMES)}")
    return None


def check_slug(label, value):
    if not value:
        return f"{label} is required"
    if not spec_module.SLUG_RE.match(value):
        return (f"{label} {value!r} must be lowercase, start with a letter, "
                f"and use single hyphens between alphanumeric parts")
    if len(value) > spec_module.SLUG_MAX:
        return (f"{label} {value!r} is {len(value)} characters — the cap is "
                f"{spec_module.SLUG_MAX}, because every AWS name is built "
                f"from it")
    return None


def parse_cidrs(raw):
    """A comma or space separated CIDR list, normalized. Raises ValueError.

    Every entry must carry a prefix length. A bare `203.0.113.4` is almost
    always meant as `/32`, but "almost always" is not good enough for the rule
    that decides who can reach port 22 — so it is rejected and the operator
    types the four characters.
    """
    values = [part.strip() for part in re.split(r"[,\s]+", raw or "")
              if part.strip()]
    if not values:
        return []
    cidrs = []
    for value in values:
        if "/" not in value:
            raise ValueError(
                f"{value!r} has no prefix length — write a single address as "
                f"{value}/32 so there is no doubt what it opens")
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as err:
            raise ValueError(f"{value!r} is not a CIDR block: {err}")
        if network.version != 4:
            raise ValueError(
                f"{value!r} is IPv6 — the node security group rule this feeds "
                f"is IPv4 only (CidrIp), so an IPv6 block would silently open "
                f"nothing")
        cidrs.append(str(network))
    return cidrs


def egress_cidr(timeout=3):
    """This machine's public address as a `/32`, or None.

    Offered as the default for `admin_cidrs`, because the honest answer to "who
    should be able to SSH in?" on day one is "me, from here", and an operator
    who has to look their own address up will reach for `0.0.0.0/0` instead.

    `checkip.amazonaws.com` rather than a third party: the run is about to talk
    to AWS anyway, so it adds no new trust relationship. Any failure — offline,
    proxied, DNS — returns None and the prompt simply has no default.
    """
    import urllib.request

    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com",
                                    timeout=timeout) as answer:
            raw = answer.read().decode("utf-8", "replace").strip()
        return str(ipaddress.ip_network(f"{raw}/32", strict=False))
    except Exception:
        return None


# ── the console seam ────────────────────────────────────────────────────────

class Console:
    """Reader and writer, injected.

    Tests script a run by handing in a list-backed reader rather than patching
    `builtins.input`, which is a process-global that leaks into whatever else
    the test runner is doing in the same interpreter.
    """

    def __init__(self, reader=input, writer=print, interactive=None):
        self._reader = reader
        self._writer = writer
        # None means "ask the real stdin". A test hands in True or False and
        # gets the interactive or the CI path deterministically, without the
        # answer depending on how the suite was launched.
        self._interactive = interactive

    def is_interactive(self):
        if self._interactive is not None:
            return bool(self._interactive)
        return require_tty()

    def say(self, line=""):
        self._writer(line)

    def ask(self, prompt):
        return self._reader(prompt)

    def confirm(self, question):
        """A literal typed `yes`. Not `y`, not Enter.

        Every confirmation in this package means "I have read the thing above",
        and a single keystroke is not evidence of that.
        """
        self.say(question)
        try:
            return self.ask("  type yes to continue: ").strip().lower() == "yes"
        except EOFError:
            return False


# ── the eight ───────────────────────────────────────────────────────────────

class Field:
    """One value asked for at the keyboard.

    `parse` turns the typed string into the stored value and may raise
    ValueError with the sentence the operator reads. `check` validates an
    already-parsed value and returns a problem or None — it also runs against a
    hand-edited file, which is why it is separate from `parse`.

    `route` exists for the one question that produces different keys depending
    on the answer (a profile name or a role ARN). Without it a field writes to
    `key`.
    """

    def __init__(self, key, question, help="", default=None, parse=None,
                 check=None, required=True, route=None, confirm=None):
        self.key = key
        self.question = question
        self.help = help
        self.default = default
        self.parse = parse
        self.check = check
        self.required = required
        self.route = route
        self.confirm = confirm

    def default_for(self, answers):
        if callable(self.default):
            return self.default(answers)
        return self.default


class Question:
    """One of the eight things an operator is asked.

    A question can carry more than one field — "project slug + env" is one
    decision the operator makes and two values the tool stores — so the count
    of questions stays eight no matter how the values are shaped.
    """

    def __init__(self, title, fields, help=""):
        self.title = title
        self.fields = tuple(fields)
        self.help = help


def _preset_menu():
    """The size table, rendered from `spec.PRESETS` so it cannot go stale.

    The HTTPS column is the part operators actually choose on. A preset with a
    balancer gets the :80 certbot target group and both listeners built for it,
    so the ACME challenge lands on a predictable node and the certificate
    finishes without anyone touching a box.
    """
    lines = []
    for name in spec_module.PRESET_NAMES:
        shape = spec_module.PRESETS[name]
        https = ("HTTPS finished automatically at the balancer"
                 if shape["balancer"]
                 else "HTTPS terminates on the single node itself")
        lines.append(
            f"    {name:<7} {shape['node_count']} x {shape['node_type']}, "
            f"{shape['db_type']}, {shape['cache_type']} — {https}")
    return "\n".join(lines)


def _default_admin_cidrs(answers):
    existing = answers.get("admin_cidrs")
    if existing:
        return ",".join(existing)
    return egress_cidr()


def _cidr_confirm(value, console):
    """`0.0.0.0/0` needs a second, separate, typed confirmation.

    An SSH rule open to the internet is the single finding `check_setup.py`
    grades FAIL on the accounts this tool builds, so the provisioner must not be
    the thing that creates one by accident. It is still allowed: an operator with
    a bastion-less emergency and a hardened sshd is making a real trade-off, and
    refusing outright would just get the rule added by hand in the console where
    nobody reviews it.
    """
    if WORLD not in (value or []):
        return True
    console.say("")
    console.say(f"  {WORLD} opens port 22 to the entire internet.")
    console.say("  Every account audit django-mojo ships grades this FAIL, and")
    console.say("  it is the rule attackers find first on a new environment.")
    return console.confirm(
        "  Leave admin access open to the world anyway?")


PROMPTS = (
    Question(
        "AWS credential",
        help="Which credential builds this environment. Leave blank to use the "
             "ambient chain (environment variables, or the default profile).\n"
             "    A profile name is the path to use for MFA and for long runs — "
             "botocore prompts, caches and refreshes it for you.\n"
             "    A role ARN (arn:aws:iam::…:role/…) is assumed once, without "
             "MFA, and is not refreshed.",
        fields=[Field(
            "aws_profile", "AWS profile name or role ARN", required=False,
            default=lambda answers: (answers.get("role_arn")
                                     or answers.get("aws_profile")),
            route=lambda value: ({"role_arn": value, "aws_profile": None}
                                 if str(value).startswith("arn:")
                                 else {"aws_profile": value,
                                       "role_arn": None}))]),
    Question(
        "Region",
        help="Everything lands here. Moving an environment between regions "
             "later is a rebuild, not a setting.",
        fields=[Field("region", "AWS region", default="us-east-1",
                      parse=lambda raw: raw.strip().lower(),
                      check=check_region)]),
    Question(
        "Project and environment",
        help="Two slugs. Every VPC, bucket, cluster, target group and log group "
             "name is derived from them, so they are short, lowercase and "
             "permanent — renaming one later builds a second environment beside "
             "the first.",
        fields=[
            Field("project", "project slug", default="mojo",
                  parse=lambda raw: raw.strip().lower(),
                  check=lambda value: check_slug("project", value)),
            Field("env", "environment slug", default="prod",
                  parse=lambda raw: raw.strip().lower(),
                  check=lambda value: check_slug("env", value)),
        ]),
    Question(
        "Apex domain",
        help="The domain the portal will answer on, e.g. `example.com`. It is "
             "what the certificate is issued for and what the A records are "
             "created under.",
        fields=[Field("apex_domain", "apex domain",
                      parse=lambda raw: raw.strip().lower().rstrip("."),
                      check=check_domain)]),
    Question(
        "Operator email",
        help="This address does TWO jobs, and both matter:\n"
             "    1. it is the ACME contact — expiry warnings for the TLS "
             "certificate go here;\n"
             "    2. it becomes the FIRST SUPERUSER of the portal, so it is the "
             "account you log in with.",
        fields=[Field("operator_email", "operator email",
                      parse=lambda raw: raw.strip(),
                      check=check_email)]),
    Question(
        "Size",
        help="One coherent shape each, not a menu to mix. Growing later is a "
             "re-run of `apply` at the bigger preset.\n" + _preset_menu(),
        fields=[Field("preset", "size", default="small",
                      parse=lambda raw: raw.strip().lower(),
                      check=check_preset)]),
    Question(
        "GitHub repository",
        help="`owner/name` of the project this environment deploys. The node "
             "clones it; nothing here needs a token.",
        fields=[Field("github_repo", "repository (owner/name)",
                      parse=lambda raw: raw.strip(),
                      check=check_repo)]),
    Question(
        "Emergency admin access",
        help="Comma-separated CIDR blocks allowed to reach port 22 on the "
             "nodes. Blank opens SSH to nobody, which is a working "
             "configuration — Session Manager still reaches the box — and a far "
             "better accident than a world-open one.",
        fields=[Field("admin_cidrs", "admin CIDRs", required=False,
                      default=_default_admin_cidrs,
                      parse=parse_cidrs, confirm=_cidr_confirm,
                      check=lambda value: None)]),
)


def _parse_bool(raw):
    value = (raw or "").strip().lower()
    if value in ("y", "yes", "true", "1"):
        return True
    if value in ("n", "no", "false", "0", ""):
        return False
    raise ValueError(f"{raw!r} is not yes or no")


def _parse_backups(raw):
    value = (raw or "").strip()
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{raw!r} is not a number of days")
    if days not in BACKUP_CHOICES:
        raise ValueError(
            f"{days} is not one of {', '.join(str(d) for d in BACKUP_CHOICES)} "
            f"— Aurora's own retention tiers, and the only two priced here")
    return days


OPTIONAL_PROMPTS = (
    Question(
        "Backup retention",
        help="Days of Aurora automated backups. 7 is the default; 35 is the "
             "engine maximum and costs roughly five times the backup storage.",
        fields=[Field("backups_days", "backup retention days", default=7,
                      parse=_parse_backups, required=False)]),
    Question(
        "Aurora reader",
        help="Add a read replica beyond what the size already includes. "
             "Additive — it never removes a reader the preset asked for.",
        fields=[Field("reader", "add an Aurora reader? (y/N)", default=False,
                      parse=_parse_bool, required=False)]),
    Question(
        "Cache replica",
        help="Add a Valkey replica beyond what the size already includes. "
             "Additive, same as the reader.",
        fields=[Field("replica", "add a cache replica? (y/N)", default=False,
                      parse=_parse_bool, required=False)]),
    Question(
        "Route53 zone",
        help="Create the hosted zone for the apex domain if it does not exist. "
             "Off by default: most domains already have a zone somewhere, and a "
             "second one that nobody delegated to is a silent DNS failure.",
        fields=[Field("route53_zone", "create the hosted zone? (y/N)",
                      default=False, parse=_parse_bool, required=False)]),
    Question(
        "Staging environment",
        help="Records the intent and prints the follow-up command. It NEVER "
             "provisions two environments in one run — one apply builds one "
             "environment, always.",
        fields=[Field("staging", "plan a staging environment too? (y/N)",
                      default=False, parse=_parse_bool, required=False)]),
)


# ── asking ──────────────────────────────────────────────────────────────────

def require_tty(stream=None):
    """True when there is a human to ask.

    Checked before prompting rather than discovered by an EOFError three
    questions in, so a CI run that is missing an answer says so on line one.
    """
    stream = stream or sys.stdin
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def ask(prompts, reader=input, writer=print, answers=None, console=None):
    """Walk the questions, returning the answers dict.

    Prefills from `answers`, so re-running `init` over an existing file is a
    review of what is already there rather than a retype of all eight. A field
    that fails validation is re-asked with the reason; it is never accepted and
    fixed later, because "later" is inside `CreateDBCluster`.
    """
    answers = dict(answers or {})
    console = console or Console(reader=reader, writer=writer)

    for question in prompts:
        console.say("")
        console.say(f"── {question.title}")
        if question.help:
            console.say(f"    {question.help}")
        for field in question.fields:
            _ask_field(console, field, answers)
    return answers


def _coerce_default(field, default):
    """A default typed as a string goes through the same parser a typed answer
    would, so pressing Enter and typing the default produce the same value."""
    if isinstance(default, str) and field.parse:
        try:
            return field.parse(default)
        except ValueError:
            return default
    return default


def _effective_default(field, answers):
    """What Enter takes. An answer already in the file ALWAYS wins.

    Without this, re-running `init` over an existing file offers the module's
    static default — `us-east-1` for a file that says `us-west-2` — and one
    absent-minded Enter silently moves the environment to another region.
    """
    current = answers.get(field.key)
    if current not in (None, ""):
        return current
    return field.default_for(answers)


def _ask_field(console, field, answers):
    default = _effective_default(field, answers)
    while True:
        suffix = "" if default in (None, "") \
            else f" [{_render_default(default)}]"
        try:
            raw = console.ask(f"  {field.question}{suffix}: ")
        except EOFError:
            raise EnvFileError(
                f"stdin ended while asking for {field.key!r} — run `init` at a "
                f"terminal, or fill the field in the environment file by hand")

        raw = (raw or "").strip()
        if raw:
            if field.parse:
                try:
                    value = field.parse(raw)
                except ValueError as err:
                    console.say(f"    {err}")
                    continue
            else:
                value = raw
        elif default not in (None, ""):
            value = _coerce_default(field, default)
        elif field.required:
            console.say(f"    {field.key} is required")
            continue
        else:
            _store(field, answers, None)
            return

        if field.required and value in (None, "", []):
            console.say(f"    {field.key} is required")
            continue

        problem = field.check(value) if field.check else None
        if problem:
            console.say(f"    {problem}")
            continue

        if field.confirm and not field.confirm(value, console):
            console.say("    not recorded — answer again")
            continue

        _store(field, answers, value)
        return


def _render_default(value):
    if isinstance(value, bool):
        return "y" if value else "n"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def _store(field, answers, value):
    updates = field.route(value) if (field.route and value) else {field.key: value}
    for key, item in updates.items():
        if item in (None, ""):
            answers.pop(key, None)
        else:
            answers[key] = item


# ── the file ────────────────────────────────────────────────────────────────

def env_path(project_root, env):
    """`<project_root>/aws/environments/<env>.json`."""
    return os.path.join(project_root or ".", ENV_DIR, f"{env}.json")


def load(path):
    """Everything in the file, unrecognized keys included. Raises EnvFileError.

    Unknown keys survive the read deliberately — see the module docstring — so
    `init` can write them back rather than silently dropping a field a newer
    release put there.
    """
    try:
        with open(path) as handle:
            raw = handle.read()
    except FileNotFoundError:
        raise EnvFileError(
            f"no environment file at {path} — run `init` to create it")
    except OSError as err:
        raise EnvFileError(f"cannot read {path}: {err}")

    try:
        answers = json.loads(raw)
    except ValueError as err:
        raise EnvFileError(f"{path} is not valid JSON: {err}")
    if not isinstance(answers, dict):
        raise EnvFileError(
            f"{path} must contain a JSON object of answers, not "
            f"{type(answers).__name__}")

    version = answers.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise EnvFileError(
            f"{path} declares schema_version {version!r}, and this django-mojo "
            f"understands {SCHEMA_VERSION} — upgrade django-mojo rather than "
            f"guessing at what the fields mean")
    return answers


def unrecognized(answers):
    """The keys in a loaded file this version cannot name."""
    return {key: value for key, value in (answers or {}).items()
            if key not in SCHEMA_KEYS}


def schema_only(answers):
    """Just the documented keys, ready to hand to `save()`."""
    return {key: value for key, value in (answers or {}).items()
            if key in SCHEMA_KEYS}


def save(path, answers, preserved=None):
    """Write the environment file. Refuses any key outside the allowlist.

    Sorted keys, two-space indent, trailing newline: the file is committed, so
    its diff has to be readable and stable across runs.
    """
    strays = sorted(set(answers or {}) - set(SCHEMA_KEYS))
    if strays:
        raise EnvFileError(
            f"refusing to write {', '.join(strays)} to {path} — this file is "
            f"committed to git, so it carries the documented schema and nothing "
            f"else. Generated secrets live in the config bucket's "
            f"bootstrap-secrets.json, never here.")

    body = dict(answers or {})
    body["schema_version"] = SCHEMA_VERSION

    for key, value in (preserved or {}).items():
        if key in SCHEMA_KEYS:
            raise EnvFileError(
                f"{key!r} is a schema key and cannot be carried through as an "
                f"unrecognized one — pass it in answers")
        body[key] = value

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as handle:
        handle.write(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return path


def problems(answers):
    """Everything wrong with a set of answers, as sentences. Empty means usable.

    Runs against a HAND-EDITED file as well as a freshly prompted one — the
    prompt path and the file path validate through exactly these functions, so
    an operator cannot get past the keyboard check by editing JSON.
    """
    found = []
    answers = answers or {}

    for key in REQUIRED_KEYS:
        value = answers.get(key)
        if value in (None, "", []):
            found.append(f"{key} is missing — re-run `init`")

    for label in ("project", "env"):
        if answers.get(label):
            problem = check_slug(label, answers[label])
            if problem:
                found.append(problem)

    for key, check in (("region", check_region), ("apex_domain", check_domain),
                       ("operator_email", check_email),
                       ("github_repo", check_repo), ("preset", check_preset)):
        if answers.get(key):
            problem = check(answers[key])
            if problem:
                found.append(problem)

    cidrs = answers.get("admin_cidrs")
    if cidrs not in (None, [], ""):
        if not isinstance(cidrs, (list, tuple)):
            found.append("admin_cidrs must be a list of CIDR blocks")
        else:
            try:
                parse_cidrs(",".join(str(item) for item in cidrs))
            except ValueError as err:
                found.append(str(err))

    days = answers.get("backups_days")
    if days is not None and days not in BACKUP_CHOICES:
        found.append(
            f"backups_days {days!r} is not one of "
            f"{', '.join(str(d) for d in BACKUP_CHOICES)}")

    if answers.get("aws_profile") and answers.get("role_arn"):
        found.append(
            "the file names both aws_profile and role_arn — keep one; a "
            "profile that assumes a role declares role_arn in ~/.aws/config")

    if found:
        # Deriving AWS names from answers that are already wrong produces a
        # second wave of noise about names nobody would have used.
        return found

    return list(spec_module.validate_names(to_spec(answers)))


def to_spec(answers, nlb=False, stable_node_ips=False):
    """The `spec.Spec` these answers describe.

    Reader and replica are ADDITIVE on top of the preset — `reader: true` means
    "at least one", never "exactly one". An opt-in that silently removed the
    reader a preset asked for would be a downgrade wearing the label of an
    upgrade.
    """
    answers = answers or {}
    overrides = {}

    domain = answers.get("apex_domain")
    if domain:
        overrides["domain"] = domain
    overrides["create_zone"] = bool(answers.get("route53_zone"))
    overrides["admin_cidrs"] = list(answers.get("admin_cidrs") or [])
    overrides["db_retention_days"] = int(answers.get("backups_days") or 7)

    if nlb or answers.get("nlb"):
        overrides["want_balancer"] = True
    if stable_node_ips or answers.get("stable_node_ips"):
        overrides["stable_node_ips"] = True

    built = spec_module.build(
        answers.get("project") or "", answers.get("env") or "",
        answers.get("region") or "", preset=answers.get("preset") or "small",
        **overrides)

    if answers.get("reader"):
        built.db_readers = max(built.db_readers, 1)
    if answers.get("replica"):
        built.cache_replicas = max(built.cache_replicas, 1)
    return built
