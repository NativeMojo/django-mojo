"""Real boto3 clients, built once per invocation and handed to everything else.

`discover.Clients` is the *seam* — a container that can be given stubbed clients
in a test. This module is what puts REAL ones in it, and it is the only file in
the package that decides which credential the run uses. Everything downstream
takes a `Clients` and never asks where it came from, which is what lets the same
ensure functions serve a laptop CLI, a portal request and a test.

THREE WAYS TO SAY WHO YOU ARE, and they are not equivalent:

    --profile NAME   `boto3.Session(profile_name=NAME)`. THE PATH TO USE for
                     anything MFA-gated or long-running. A profile in
                     `~/.aws/config` carrying `role_arn` + `mfa_serial` gets
                     botocore's own MFA prompt, its credential cache, and —
                     the part that matters — automatic refresh when the
                     assumed credential expires. A fresh account's first
                     `apply` can sit for ten minutes waiting on Aurora, and a
                     one-hour credential that renews itself is the difference
                     between a resumable run and a half-built VPC.
    --role-arn ARN   one plain `sts:AssumeRole`, no MFA, no refresh. A
                     convenience for the common "assume the bootstrap role in
                     the target account" case, and honestly documented as
                     such: the credential it mints expires and this tool will
                     not renew it. Use `--profile` if the run might be long.
    neither          the ambient chain — environment variables, the shared
                     credential file's default profile, an instance role.

WHY NOT `mojo.helpers.aws`. It already builds sessions, caches clients and
labels mutations. All of it lives under `mojo.helpers`, which this package may
not import (see `mojo/deploy/__init__.py`) — `logit` reads a path attribute that
only exists once Django settings have been configured, and there are no settings
on the laptop of someone provisioning an empty account. So boto3 is built
directly here, lazily, inside functions, exactly as the rest of `mojo/deploy/`
does it.

Nothing here prints. A credential problem is raised as `CredentialError` and the
CLI decides how to say it, because the portal will one day call the same code
and needs the exception, not a line on stdout.
"""

from mojo.deploy.provision import discover


# What shows up in CloudTrail as `userIdentity.sessionContext` for a
# `--role-arn` run. Named for the tool rather than the human so an auditor
# reading the trail six months later can tell a bootstrap apart from a console
# click without knowing who ran it.
SESSION_NAME = "django-mojo-provision"

# An assumed session lasts this long unless the role's own MaximumSessionDuration
# is shorter, in which case AWS caps it there. One hour is boto3's default and
# is deliberately not raised: a longer credential is not the fix for a run that
# might outlive it — `--profile` is.
SESSION_SECONDS = 3600


class CredentialError(RuntimeError):
    """No usable AWS credential, or one AWS refused.

    Raised rather than printed so the same code path serves a CLI that wants a
    one-line message and a portal that wants a JSON error. The message is
    already a full sentence a human can act on.
    """


def build_session(profile=None, role_arn=None, region=None):
    """A `boto3.Session` for the credential the operator asked for.

    `profile` and `role_arn` are mutually exclusive on purpose. Accepting both
    would mean silently picking one, and the two answer to different people —
    a profile is what the operator's laptop is configured with, a role ARN is
    what someone pasted from a runbook.
    """
    if profile and role_arn:
        raise CredentialError(
            "--profile and --role-arn both name a credential — pass one. A "
            "profile that should assume a role declares `role_arn` in "
            "~/.aws/config, which is also the only way to get MFA and "
            "automatic refresh.")

    try:
        import boto3
    except ImportError:
        raise CredentialError(
            "boto3 is not installed — `pip install boto3` (or run this from an "
            "environment that has it) before provisioning an account.")

    if profile:
        return boto3.Session(profile_name=profile, region_name=region)

    if role_arn:
        return _assume(boto3, role_arn, region)

    return boto3.Session(region_name=region)


def _assume(boto3, role_arn, region):
    """One `sts:AssumeRole`, no MFA, no refresh. See the module docstring.

    Deliberately NOT wrapped in `report.safe`: this happens before there is a
    findings list to append to, and a credential that cannot be assumed is not
    a finding about the account — it is the end of the run.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    base = boto3.Session(region_name=region)
    try:
        assumed = base.client("sts").assume_role(
            RoleArn=role_arn, RoleSessionName=SESSION_NAME,
            DurationSeconds=SESSION_SECONDS)
    except (BotoCoreError, ClientError) as err:
        raise CredentialError(
            f"could not assume {role_arn}: {err}")

    credentials = (assumed or {}).get("Credentials") or {}
    missing = [key for key in ("AccessKeyId", "SecretAccessKey", "SessionToken")
               if not credentials.get(key)]
    if missing:
        raise CredentialError(
            f"sts:AssumeRole answered without {', '.join(missing)} — the "
            f"response cannot be turned into a session")

    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region)


def build_clients(profile=None, role_arn=None, region=None, session=None):
    """The `discover.Clients` every ensure function in this package takes.

    Built ONCE per invocation and threaded through, not rebuilt per step: the
    container caches one client per service, so a run makes one connection pool
    per service rather than one per call site.
    """
    if session is None:
        session = build_session(profile=profile, role_arn=role_arn,
                                region=region)
    return discover.Clients(session=session)


def identify(clients):
    """Who this credential is, in one `sts:GetCallerIdentity`.

    Called before anything is observed so the very first line an operator sees
    names the account they are about to change. Confirming the account BEFORE
    the preview is most of the value: "wrong account" is the mistake that costs
    an afternoon, and it is invisible in a plan that only lists resource names.

    Returns `{"account_id", "arn", "user_id"}`. Raises `CredentialError` when
    the call cannot be made, because there is no version of this run that is
    safe to continue with an unusable credential.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        answer = clients.get("sts").get_caller_identity()
    except (BotoCoreError, ClientError) as err:
        raise CredentialError(
            f"sts:GetCallerIdentity failed, so the account this would build in "
            f"is unknown: {err}")

    account_id = (answer or {}).get("Account")
    if not account_id:
        raise CredentialError(
            "sts:GetCallerIdentity answered without an account id — refusing "
            "to provision into an account that cannot be named")

    return {"account_id": account_id,
            "arn": (answer or {}).get("Arn"),
            "user_id": (answer or {}).get("UserId")}
