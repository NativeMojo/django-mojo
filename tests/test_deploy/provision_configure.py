"""The certificate sequence, driven against a recorded shell.

WHY THIS FILE IS ORDERING ASSERTIONS RATHER THAN OUTCOME ASSERTIONS. Every
step in `certificate.configure_certificate` "works" on its own. What can go
wrong is the arrangement, and two of the arrangements are things a reasonable
person would write by accident:

    Guarding the server_name rewrite behind "only if it still says
    yourdomain.com" looks like caution. It is not: `ec2_deploy.sh` does an
    unconditional `cp -f` of the shipped nginx config on EVERY run, so by the
    time this code looks, the placeholder is always back — and the guard only
    ever fires on the run that needed it least.

    Skipping certbot when a certificate already exists looks like the whole
    point of a rate-limit guard. On its own it leaves a resumed node serving
    the SNAKEOIL certificate, because that same `cp -f` reset the
    ssl_certificate paths too. The skip branch has to re-point them.

Both are asserted below, along with the skip check needing expiry AND a SAN
match (an operator who changed apex_domain has an unexpired certificate for
the wrong name), and the production certbot never being reached unless the
staging dry run passed.

The shell is a recorder: every command is captured, and responses are keyed by
substring, so a test says "openssl -ext subjectAltName fails" and nothing else.
"""

import types

from testit import helpers as th


APEX = "example.com"
EMAIL = "ops@example.com"
NODE_IP = "203.0.113.10"


class _Runner:
    """A shell that records and answers by substring. Default: success."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(cmd)
        for pattern, result in self.responses:
            if pattern in cmd:
                return result
        return 0, "", ""

    def ran(self, fragment):
        return [cmd for cmd in self.calls if fragment in cmd]

    def acme(self):
        """Only the two commands that actually talk to Let's Encrypt.

        NOT a search for "certbot": the ACME webroot is /var/www/certbot, so
        the probe-file step names it too and a loose match would report ACME
        traffic that never happened.
        """
        return [cmd for cmd in self.calls
                if "certbot certonly" in cmd or "certbot --nginx" in cmd]

    def index_of(self, fragment):
        for position, cmd in enumerate(self.calls):
            if fragment in cmd:
                return position
        return -1


def _healthy(extra=()):
    """A node whose DNS, probe and https all work; certificate state is set per test."""
    return list(extra) + [
        ("getent hosts", (0, f"{NODE_IP}  {APEX}", "")),
        (f"http://{APEX}/.well-known", (0, "", "")),
        (f"https://{APEX}/api/version", (0, "200", "")),
    ]


def _with_probe_echo(runner):
    """The probe compares what came back with the nonce it wrote, so the
    stubbed curl has to echo it."""
    original = runner.__call__

    def call(cmd, timeout=None):
        if "curl" in cmd and ".well-known/acme-challenge/" in cmd:
            runner.calls.append(cmd)
            return 0, cmd.rsplit("/", 1)[-1], ""
        return original(cmd, timeout=timeout)

    return call


# ── the skip branch ─────────────────────────────────────────────────────────

@th.django_unit_test("a valid, matching certificate skips certbot entirely")
def test_skip_on_valid_and_matching_certificate(opts):
    from mojo.deploy.provision import certificate

    runner = _Runner(_healthy())
    findings = certificate.configure_certificate(
        runner, APEX, EMAIL, expected_ip=NODE_IP)

    th.assert_eq(runner.acme(), [],
                 "certbot must not run at all when the node already holds a "
                 "valid certificate for this name — that skip, not the staging "
                 "dry run, is what makes a re-run safe against Let's Encrypt's "
                 "five-failures-per-hour limit")
    codes = [finding.code for finding in findings]
    th.assert_true("certbot.skipped" in codes,
                   f"the skip must be reported, not silent — got {codes}")


@th.django_unit_test("the skip branch still re-points nginx at the live lineage")
def test_skip_rewrites_cert_paths(opts):
    from mojo.deploy.provision import certificate

    runner = _Runner(_healthy())
    certificate.configure_certificate(runner, APEX, EMAIL)

    rewrites = runner.ran("ssl_certificate")
    th.assert_true(bool(rewrites),
                   "ec2_deploy.sh reset ssl_certificate to the snakeoil "
                   "placeholder on this run, so skipping certbot without "
                   "re-pointing the paths leaves the node serving a "
                   "self-signed certificate with a valid one on disk")
    joined = " ".join(rewrites)
    th.assert_true(certificate.fullchain(APEX) in joined,
                   f"the rewrite must name {certificate.fullchain(APEX)}")
    th.assert_true(certificate.privkey(APEX) in joined,
                   f"the rewrite must name {certificate.privkey(APEX)}")
    th.assert_true(bool(runner.ran("nginx -t")),
                   "and nginx must be tested and reloaded afterwards, or the "
                   "rewrite is a file nobody is serving")


@th.django_unit_test("an expired certificate does not qualify for the skip")
def test_no_skip_when_expired(opts):
    from mojo.deploy.provision import certificate

    runner = _Runner(_healthy([("openssl x509 -checkend", (1, "", ""))]))
    runner_call = _with_probe_echo(runner)
    certificate.configure_certificate(runner_call, APEX, EMAIL)

    th.assert_true(bool(runner.acme()),
                   "an expiring certificate must be renewed, not skipped over")


@th.django_unit_test("expiry alone is not enough — the SAN must cover the apex")
def test_no_skip_when_san_does_not_match(opts):
    from mojo.deploy.provision import certificate

    # Unexpired, but issued for a different name: exactly what an operator who
    # changed apex_domain between runs is left holding.
    runner = _Runner(_healthy([("subjectAltName", (1, "", ""))]))
    runner_call = _with_probe_echo(runner)
    findings = certificate.configure_certificate(runner_call, APEX, EMAIL)

    th.assert_true(bool(runner.acme()),
                   "a certificate that does not cover this apex must be "
                   "replaced — checking expiry alone would leave the node "
                   "serving a certificate for a domain nobody is asking for")
    messages = " ".join(finding.message for finding in findings)
    th.assert_true("does not cover" in messages,
                   f"and the reason must say so — got: {messages}")


# ── the unconditional rewrite ───────────────────────────────────────────────

@th.django_unit_test("server_name is rewritten on every run, including the skip path")
def test_server_name_rewrite_is_unconditional(opts):
    from mojo.deploy.provision import certificate

    for label, responses in (
            ("skip path", _healthy()),
            ("issue path", _healthy([("subjectAltName", (1, "", ""))]))):
        runner = _Runner(responses)
        certificate.configure_certificate(_with_probe_echo(runner), APEX, EMAIL)

        rewrites = runner.ran("server_name")
        th.assert_true(bool(rewrites),
                       f"{label}: server_name must be set every run — "
                       f"ec2_deploy.sh resets it to the shipped placeholder "
                       f"each time, and certbot --nginx cannot find a virtual "
                       f"host to install into without it")
        th.assert_true(APEX in rewrites[0],
                       f"{label}: the rewrite must name the real apex")
        th.assert_eq(runner.index_of("server_name"), 0,
                     f"{label}: it must happen FIRST, before anything else "
                     f"reads or reloads the vhost")


@th.django_unit_test("the :80 catch-all vhost is left alone by the rewrite")
def test_server_name_rewrite_spares_the_challenge_vhost(opts):
    from mojo.deploy.provision import certificate

    runner = _Runner(_healthy())
    certificate.configure_certificate(runner, APEX, EMAIL)

    expression = runner.ran("server_name")[0]
    th.assert_true("[^_]" in expression,
                   f"the sed must exclude `server_name _;` — that is the :80 "
                   f"block serving /.well-known/acme-challenge, and renaming "
                   f"it breaks the very validation path certbot needs. Got: "
                   f"{expression}")


# ── the Let's Encrypt steps ─────────────────────────────────────────────────

@th.django_unit_test("staging runs before production, and production only on its success")
def test_staging_gate_precedes_production(opts):
    from mojo.deploy.provision import certificate

    runner = _Runner(_healthy([("subjectAltName", (1, "", ""))]))
    certificate.configure_certificate(_with_probe_echo(runner), APEX, EMAIL)

    dry = runner.index_of("--dry-run")
    real = runner.index_of("certbot --nginx")
    th.assert_true(dry >= 0, "the staging dry run must happen")
    th.assert_true(real > dry,
                   f"production issuance must come after the staging dry run "
                   f"(dry at {dry}, real at {real}) — staging failures are "
                   f"free, production failures are five per hour")


@th.django_unit_test("a failed staging dry run never reaches the production endpoint")
def test_failed_dry_run_stops_before_production(opts):
    from mojo.deploy.provision import certificate

    runner = _Runner(_healthy([
        ("subjectAltName", (1, "", "")),
        ("--dry-run", (1, "", "Challenge failed for domain example.com")),
    ]))
    findings = certificate.configure_certificate(
        _with_probe_echo(runner), APEX, EMAIL)

    th.assert_eq([c for c in runner.calls if "certbot --nginx" in c], [],
                 "nothing may be requested from the production endpoint after "
                 "the staging dry run failed — that is the one request that "
                 "counts against the rate limit")
    remedies = " ".join(finding.remedy or "" for finding in findings)
    th.assert_true("five failed authorizations" in remedies,
                   f"the failure must tell the operator the limit and that "
                   f"re-running is safe — got: {remedies}")


@th.django_unit_test("DNS and the probe file are checked before any ACME traffic")
def test_free_checks_precede_acme(opts):
    from mojo.deploy.provision import certificate

    runner = _Runner(_healthy([("subjectAltName", (1, "", ""))]))
    certificate.configure_certificate(_with_probe_echo(runner), APEX, EMAIL)

    dns = runner.index_of("getent hosts")
    probe = runner.index_of(".well-known/acme-challenge")
    certbot = runner.index_of("certbot certonly")
    th.assert_true(0 < dns < probe < certbot,
                   f"the free checks must come first: dns at {dns}, probe at "
                   f"{probe}, certbot at {certbot}. Both catch the common "
                   f"causes of a failed challenge at no cost against the limit")


@th.django_unit_test("a node whose apex points elsewhere is not sent to Let's Encrypt")
def test_dns_mismatch_stops_before_acme(opts):
    from mojo.deploy.provision import certificate

    runner = _Runner(_healthy([
        ("subjectAltName", (1, "", "")),
        ("getent hosts", (0, f"198.51.100.4  {APEX}", "")),
    ]))
    findings = certificate.configure_certificate(
        _with_probe_echo(runner), APEX, EMAIL, expected_ip=NODE_IP)

    th.assert_eq(runner.acme(), [],
                 "certbot would fail its challenge against the wrong host, and "
                 "that failure counts — so it is not attempted")
    codes = [finding.code for finding in findings]
    th.assert_true("dns.mismatch" in codes,
                   f"and the mismatch is named — got {codes}")


# ── the fleet ───────────────────────────────────────────────────────────────

@th.django_unit_test("a multi-node environment gets the hand-off, not an attempt")
def test_multi_node_skips_the_certificate_block(opts):
    from mojo.deploy.provision import __main__ as cli
    from mojo.deploy.provision import inputs, remote

    said = []
    console = inputs.Console(reader=lambda prompt: "", writer=said.append,
                             interactive=False)
    args = types.SimpleNamespace(skip_certificate=False, ssh_user="ec2-user",
                                 identity=None, nlb=False, project_root=".")
    answers = {"apex_domain": APEX, "operator_email": EMAIL,
               "project": "demo", "env": "prod", "region": "us-west-2",
               "preset": "small", "github_repo": "acme/demo"}
    topology = inputs.to_spec(answers)

    def _refuse(*a, **kw):
        raise AssertionError(
            "a fleet must not be SSHed for certificates: certbot --nginx "
            "rewrites app.conf with an include that exists only where certbot "
            "ran, so the mutated file fails nginx -t on every other node")

    original = remote.build_runner
    remote.build_runner = _refuse
    try:
        findings = cli._finish_https(
            args, answers, topology, ["203.0.113.10", "203.0.113.11"], console)
    finally:
        remote.build_runner = original

    th.assert_eq(findings, [],
                 "the certificate block produces no findings on a fleet — it "
                 "is not attempted, rather than attempted and failed")
    printed = "\n".join(said)
    th.assert_true("dnsman" in printed,
                   f"the operator must be told where fleet certificates come "
                   f"from instead — got: {printed}")
    th.assert_true(APEX in printed,
                   "and the hand-off must name the domain it is about")


@th.unit_test("the convergence probe goes through nginx's https, not the :80 redirect")
def test_probe_is_https_with_snakeoil_tolerance(opts):
    import inspect
    from mojo.deploy.provision import remote

    th.assert_true(remote.PROBE_URL.startswith("https://127.0.0.1"),
                   "the shipped :80 vhost 301s everything except ACME, so a "
                   "plain-http probe can only ever see nginx's redirect — the "
                   "live MojoLand run failed convergence on exactly this")
    source = inspect.getsource(remote)
    th.assert_true("-fsSk" in source,
                   "the probe must tolerate the snakeoil certificate (-k): "
                   "certificate validity is the NEXT step's subject, and "
                   "pre-certbot the :443 vhost can only serve self-signed")
