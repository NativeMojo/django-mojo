"""Split out of tests/test_deploy/provision_ensure.py (maestro #1839).

`cli._resolve_identity` resolves the operator's SSH key under $HOME with no
injectable home, so these tests patch.dict os.environ — process-global, and
unsafe under the parallel default tier.
"""

from testit import helpers as th


REGION = "us-west-2"


PROJECT = "wmx"


ENV = "prod"


ACCOUNT = "123456789012"


ZONES = ("us-west-2a", "us-west-2b", "us-west-2c")


def _spec(**overrides):
    from mojo.deploy.provision import spec as spec_module
    preset = overrides.pop("preset", "small")
    overrides.setdefault("account_id", ACCOUNT)
    return spec_module.build(PROJECT, ENV, REGION, preset=preset, **overrides)


def _observed(**overrides):
    from mojo.deploy.provision import discover
    observed = discover.blank()
    observed.account_id = ACCOUNT
    observed.region = REGION
    observed.azs = [{"ZoneName": zone} for zone in ZONES]
    observed.offered_zone_names = list(ZONES)
    observed.update(overrides)
    return observed


PRIVATE_KEY_BODY = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
                    "b3BlbnNzaC1rZXktdjEAAAAA-not-a-real-key\n"
                    "-----END OPENSSH PRIVATE KEY-----")


class _Console:
    """Records what the CLI would print, so a test can assert what it did NOT."""

    def __init__(self):
        self.lines = []

    def say(self, text=""):
        self.lines.append(text)

    def text(self):
        return "\n".join(self.lines)


@th.unit_test("configure and admin find the generated key themselves")
def test_cli_resolves_the_identity_from_the_secrets_object(opts):
    import os
    import tempfile
    from unittest import mock

    from objict import objict

    from mojo.deploy.provision import __main__ as cli

    spec = _spec()
    observed = _observed(secrets={"ssh_private_key": PRIVATE_KEY_BODY,
                                  "db_password": "p" * 40})
    console = _Console()

    with tempfile.TemporaryDirectory() as home:
        with mock.patch.dict(os.environ, {"HOME": home}):
            identity = cli._resolve_identity(
                objict(identity=None), spec, observed, console)

        th.assert_eq(identity, os.path.join(home, ".ssh",
                                            f"{PROJECT}-{ENV}.pem"),
                     "with no --identity, the key generated for this "
                     "environment must be resolved automatically — extracting "
                     "it from bootstrap-secrets.json by hand is exactly the "
                     "manual step this removes")
        th.assert_true(os.path.exists(identity),
                       "the resolved path must be a file ssh can actually use")
        th.assert_eq(PRIVATE_KEY_BODY in console.text(), False,
                     "the key material must never be printed — this console "
                     "output goes to a terminal and, through the portal, into "
                     "a browser. The path is fine; the contents are not")
        th.assert_true(identity in console.text(),
                       "the operator must be told which key is being used")


@th.unit_test("an explicit --identity always wins and writes nothing")
def test_cli_identity_flag_wins_over_the_stored_key(opts):
    import os
    import tempfile
    from unittest import mock

    from objict import objict

    from mojo.deploy.provision import __main__ as cli

    spec = _spec()
    observed = _observed(secrets={"ssh_private_key": PRIVATE_KEY_BODY})
    console = _Console()

    with tempfile.TemporaryDirectory() as home:
        with mock.patch.dict(os.environ, {"HOME": home}):
            identity = cli._resolve_identity(
                objict(identity="/keys/mine.pem"), spec, observed, console)

        th.assert_eq(identity, "/keys/mine.pem",
                     "an operator who named a key has said something this "
                     "cannot know better than")
        th.assert_eq(os.path.exists(os.path.join(home, ".ssh")), False,
                     "an explicit identity must not cause a key to be written "
                     "anywhere")


@th.unit_test("no stored private key falls back to the agent, and says so")
def test_cli_falls_back_to_the_ssh_agent_when_the_key_was_imported(opts):
    import os
    import tempfile
    from unittest import mock

    from objict import objict

    from mojo.deploy.provision import __main__ as cli

    spec = _spec()
    observed = _observed(secrets={"db_password": "p" * 40})
    console = _Console()

    with tempfile.TemporaryDirectory() as home:
        with mock.patch.dict(os.environ, {"HOME": home}):
            identity = cli._resolve_identity(
                objict(identity=None), spec, observed, console)

    th.assert_eq(identity, None,
                 "None is what build_runner reads as 'no -i flag', which is "
                 "the agent fallback that worked before this existed")
    th.assert_true("agent" in console.text(),
                   f"an imported key pair is not a failure — the operator must "
                   f"be told why no key file is being used rather than watching "
                   f"a silent SSH failure: {console.text()!r}")

