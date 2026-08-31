#!/usr/bin/env python3
"""Resolve the node's non-root application account from trusted configuration.

One resolver for every caller that must know who owns the application plane —
`jobman start` demoting a root invocation, and `update.sh` deciding who runs
Git inside the deployment transaction (via `python3 -m mojo.deploy app-user`).

THE LADDER, first valid answer wins (item #3429, reviving #2246's guard):

  1. Field 6 of the deployed jobs cron entry (`/etc/cron.d/3_mojo_jobs`,
     honoring $CRON_ETC). The cron entry names the exact account the FLEET
     intends to run the engine as — a deployed statement of intent, identical
     on every node, and what will own the engine sixty seconds from now no
     matter what any single invocation decides. Nothing outranks it.
  2. An explicit candidate — the caller's $APP_USER environment value. Real
     operator intent on a healthy call, but ambient enough that it must not
     beat the fleet: a root caller with a stray valid APP_USER (say `ubuntu`,
     which typically carries NOPASSWD:ALL) would otherwise hand the engine to
     an account cron can never manage again.
  3. The owner of the project checkout. Git and SSH identity follow that
     account by definition, and it covers a fresh node whose cron entry has
     not been rendered yet.

$SUDO_USER appears NOWHERE. It is merely whoever ran the sudo we are under:
on the deploy path a root engine makes it `root` (the exact poisoning this
module exists to reject), and on a manual `sudo bash update.sh` it is a login
account that must not own the engine either.

`root`, an empty answer, GNU stat's `UNKNOWN`, a uid-0 alias, and every
option-shaped or digit-only string all resolve to NOTHING — and nothing means
the caller fails closed. Guessing wrong is how a node gets bricked; guessing
not at all costs the caller a refusal it can report loudly.
"""

import os
import pwd
import re

CRON_NAME = "3_mojo_jobs"
DEFAULT_CRON_DIR = "/etc/cron.d"

# The charset the removed bash guard allowed. Anything else — spaces, colons,
# globs — is not a name this module will ever hand to `sudo -u`.
_NAME_SHAPE = re.compile(r"^[A-Za-z0-9_.-]+$")


def valid_app_user_name(name):
    """The syntactic half alone — no account lookup.

    This is all a renderer may check: `mojo.deploy render` legitimately runs
    on a machine that does not carry the target node's accounts.

    A leading dash is an OPTION to `id`/`sudo`, not a name. All-digits is a
    uid, not a name we were told to use — and the root ban must stay a ban on
    *power*, so the uid check in `valid_app_user` still runs for real use.
    """
    if not name or name in ("root", "UNKNOWN"):
        return False
    if name.startswith("-"):
        return False
    if not _NAME_SHAPE.match(name):
        return False
    if name.isdigit():
        return False
    return True


def valid_app_user(name):
    """Full validation: a syntactically safe, existing, non-uid-0 account.

    The uid test is separate from the string ban on "root" because a uid-0
    alias (`toor`) is a different string with identical power.
    """
    if not valid_app_user_name(name):
        return False
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return False
    return entry.pw_uid != 0


def cron_app_user(cron_path):
    """Field 6 of the first schedule line, or None.

    A 1:1 port of the removed awk (`$1 !~ /^#/ && $1 !~ /=/ && NF >= 7
    { print $6; exit }`): skip comments and the SHELL=/PATH= assignment
    lines, take the user field of the first real schedule entry, and stop.
    The value is NOT validated here — the ladder does that, so a poisoned
    cron file falls through instead of poisoning the answer.
    """
    try:
        with open(cron_path) as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 7:
                    continue
                if fields[0].startswith("#") or "=" in fields[0]:
                    continue
                return fields[5]
    except OSError:
        pass
    return None


def checkout_owner(root):
    """The account owning the project checkout, or None."""
    try:
        entry = pwd.getpwuid(os.stat(root).st_uid)
    except (OSError, KeyError):
        return None
    return entry.pw_name


def resolve_app_user(root, candidate=None, cron_path=None):
    """The ladder. Returns the account name, or None to fail closed."""
    if cron_path is None:
        cron_path = os.path.join(
            os.environ.get("CRON_ETC") or DEFAULT_CRON_DIR, CRON_NAME)
    for name in (cron_app_user(cron_path), candidate, checkout_owner(root)):
        if name and valid_app_user(name):
            return name
    return None
