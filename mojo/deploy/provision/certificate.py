"""HTTPS on a single node, in an order chosen to survive being re-run.

Let's Encrypt allows FIVE FAILED AUTHORIZATIONS PER HOSTNAME PER HOUR. A
provisioning tool that retries certbot on every run burns that in one
afternoon and then cannot issue a certificate for the rest of the hour no
matter what the operator fixes. So every cheap check happens first, and the
only two steps that talk to Let's Encrypt at all are the last two — one of
which is deliberately aimed at the staging endpoint, where failures cost
nothing.

THE ORDER, and what each step is protecting:

    1. server_name       certbot's --nginx installer looks for a virtual host
                         matching -d. The shipped vhost says `yourdomain.com`,
                         so without this certbot exits with "could not find a
                         virtual host" and never gets as far as ACME.
    2. skip-if-valid     expiry AND a SAN match. This is what makes a re-run
                         safe against the rate limit — not the dry run.
    3. DNS resolves      pure DNS, zero LE traffic.
    4. probe file        proves the real ACME path works end to end over the
                         public name, using the :80 block that is already
                         serving /.well-known/acme-challenge.
    5. staging dry-run   real ACME, wrong endpoint. Failures here do not count.
    6. the real one      only reached when 1-5 all passed.
    7. verify            nginx -t, reload, and an actual HTTPS 200.

WHY STEP 1 IS UNCONDITIONAL, AND STEP 2 REWRITES PATHS EVEN WHEN IT SKIPS.
Both come from the same discovery: `ec2_deploy.sh` does an unconditional
`cp -f` of the shipped nginx configs on EVERY run. So

  - guarding the server_name rewrite behind "only if it still says
    yourdomain.com" protects nothing — an operator's edit was already
    destroyed one step earlier — and it breaks the resumed node, whose
    server_name has just been reset to the placeholder; and
  - a node that already holds a perfectly good Let's Encrypt lineage comes
    back from a re-deploy pointing at the SNAKEOIL certificate, because the
    `cp -f` reset those two lines too. Skipping certbot without re-pointing
    them leaves the node serving a self-signed certificate with a valid one
    sitting unused on disk.

MULTI-NODE DOES NOT COME HERE AT ALL. `certbot --nginx` rewrites app.conf in a
way (`include /etc/letsencrypt/options-ssl-nginx.conf`) that fails `nginx -t`
on any node where certbot never ran, so copying the mutated file to a second
node breaks it. A fleet's certificates belong to the dnsman/edge plane; the
caller prints that hand-off instead of calling this module.
"""

from mojo.deploy.provision import report


STEP = "certificate"

NGINX_CONF = "/etc/nginx/conf.d/app.conf"
LETSENCRYPT_LIVE = "/etc/letsencrypt/live"
WEBROOT = "/var/www/certbot"
ACME_PATH = ".well-known/acme-challenge"

# Renew a week before expiry rather than on the day. certbot's own cron renews
# at 30 days; this window only decides whether a PROVISIONING run should reach
# for a new certificate, and a week is enough margin for an operator to notice.
EXPIRY_WINDOW_SECONDS = 604800

RATE_LIMIT_NOTE = (
    "Let's Encrypt allows five failed authorizations per hostname per hour. "
    "Steps 1-4 of this sequence (server_name, the expiry/SAN check, DNS, the "
    "probe file) cost nothing against that limit, and step 5 runs against the "
    "STAGING endpoint whose failures are free — so a fix-and-re-run is safe. "
    "Only the final issuance counts."
)


def lineage(apex):
    return f"{LETSENCRYPT_LIVE}/{apex}"


def fullchain(apex):
    return f"{lineage(apex)}/fullchain.pem"


def privkey(apex):
    return f"{lineage(apex)}/privkey.pem"


def _fail(findings, code, message, remedy):
    findings.append(report.Finding(STEP, report.BLIND, code, message, remedy))
    return False


def rewrite_server_name(run, apex, findings, conf=NGINX_CONF):
    """Point the :443 vhost at the real apex. EVERY RUN — see the docstring.

    The `[^_]` in the pattern is what leaves the `:80` block's `server_name _;`
    alone: that one is the catch-all which serves the ACME challenge, and
    renaming it would break the very path step 4 tests.
    """
    expression = (r"s/^\([[:space:]]*\)server_name[[:space:]]\+[^_].*;"
                  rf"/\1server_name {apex};/")
    rc, out, err = run(f"sudo sed -i '{expression}' {conf}")
    if rc != 0:
        return _fail(findings, "nginx.server_name",
                     f"could not set server_name in {conf} ({err or out})",
                     f"check {conf} exists — ec2_deploy.sh installs it")
    findings.append(report.existing(
        STEP, "nginx.server_name", f"{conf} serves {apex}"))
    return reload_nginx(run, findings, "nginx.server_name_reload")


def rewrite_cert_paths(run, apex, findings, conf=NGINX_CONF):
    """Point ssl_certificate/ssl_certificate_key at the live lineage.

    Run on the SKIP path too. `ec2_deploy.sh` resets both lines to the
    snakeoil placeholder on every deploy, so a node with a valid certificate
    and no rewrite serves a self-signed one.
    """
    expressions = (
        rf"s#^\([[:space:]]*\)ssl_certificate[[:space:]]\+.*;"
        rf"#\1ssl_certificate {fullchain(apex)};#",
        rf"s#^\([[:space:]]*\)ssl_certificate_key[[:space:]]\+.*;"
        rf"#\1ssl_certificate_key {privkey(apex)};#",
    )
    script = " ".join(f"-e '{expression}'" for expression in expressions)
    rc, out, err = run(f"sudo sed -i {script} {conf}")
    if rc != 0:
        return _fail(findings, "nginx.cert_paths",
                     f"could not point {conf} at {lineage(apex)} "
                     f"({err or out})",
                     f"edit {conf} by hand: ssl_certificate "
                     f"{fullchain(apex)}, ssl_certificate_key "
                     f"{privkey(apex)}")
    findings.append(report.existing(
        STEP, "nginx.cert_paths", f"{conf} points at {lineage(apex)}"))
    return reload_nginx(run, findings, "nginx.cert_paths_reload")


def reload_nginx(run, findings, code):
    rc, out, err = run("sudo nginx -t && sudo systemctl reload nginx")
    if rc != 0:
        return _fail(findings, code,
                     f"nginx refused the configuration ({err or out})",
                     "ssh in and run `sudo nginx -t` — the node is still "
                     "serving its previous configuration")
    findings.append(report.existing(STEP, code, "nginx reloaded"))
    return True


def certificate_is_usable(run, apex):
    """(usable, reason) — expiry AND a subjectAltName covering the apex.

    Expiry alone is not enough. An operator who changed `apex_domain` between
    runs has a perfectly unexpired certificate for the OLD name sitting in a
    lineage directory named after it, and skipping on expiry alone would leave
    the node serving a certificate for a domain nobody is asking for.
    """
    path = fullchain(apex)
    rc, _, _ = run(f"sudo test -s {path}")
    if rc != 0:
        return False, f"no certificate at {path}"

    rc, _, _ = run(
        f"sudo openssl x509 -checkend {EXPIRY_WINDOW_SECONDS} -noout -in {path}")
    if rc != 0:
        return False, (f"the certificate at {path} expires within "
                       f"{EXPIRY_WINDOW_SECONDS // 86400} days")

    rc, _, _ = run(
        f"sudo openssl x509 -ext subjectAltName -noout -in {path} "
        f"| grep -qF 'DNS:{apex}'")
    if rc != 0:
        return False, (f"the certificate at {path} does not cover {apex} — "
                       f"the apex domain changed since it was issued")
    return True, f"{path} is valid and covers {apex}"


def apex_resolves(run, apex, findings, expected_ip=None):
    rc, out, err = run(f"getent hosts {apex}")
    addresses = [line.split()[0] for line in (out or "").splitlines()
                 if line.split()]
    if rc != 0 or not addresses:
        detail = err or "no answer"
        return _fail(findings, "dns.unresolved",
                     f"{apex} does not resolve from the node ({detail})",
                     f"create the A record for {apex} and wait for it to "
                     f"propagate — Let's Encrypt resolves it the same way")
    if expected_ip and expected_ip not in addresses:
        return _fail(findings, "dns.mismatch",
                     f"{apex} resolves to {', '.join(addresses)}, not to this "
                     f"node's address {expected_ip}",
                     f"point {apex} at {expected_ip}; certbot validates by "
                     f"fetching a file from whatever that record names, so it "
                     f"would fail against the wrong host")
    findings.append(report.existing(
        STEP, "dns.ok", f"{apex} resolves to {', '.join(addresses)}"))
    return True


def probe_round_trip(run, apex, findings, nonce=None):
    """Write a file into the ACME webroot and fetch it through the public name.

    This is the whole of ACME's HTTP-01 challenge minus Let's Encrypt: same
    directory, same URL shape, same nginx location block. If this fails, the
    real challenge would fail too — for free, and with a far clearer error.
    """
    nonce = nonce or _nonce()
    path = f"{WEBROOT}/{ACME_PATH}/{nonce}"
    rc, out, err = run(
        f"sudo mkdir -p {WEBROOT}/{ACME_PATH} && "
        f"echo {nonce} | sudo tee {path} >/dev/null && "
        f"sudo chown -R www:www {WEBROOT}")
    if rc != 0:
        return _fail(findings, "probe.unwritable",
                     f"could not write {path} ({err or out})",
                     f"ec2_bootstrap.sh creates {WEBROOT} — check it ran")

    rc, out, err = run(
        f"curl -fsS --max-time 15 http://{apex}/{ACME_PATH}/{nonce}")
    run(f"sudo rm -f {path}")
    if rc != 0 or (out or "").strip() != nonce:
        return _fail(findings, "probe.unreachable",
                     f"http://{apex}/{ACME_PATH}/{nonce} did not return what "
                     f"was written ({err or out or 'no answer'})",
                     "port 80 must reach this node from the internet for "
                     "HTTP-01 validation: check the security group, any "
                     "upstream firewall, and that DNS points here. "
                     + RATE_LIMIT_NOTE)
    findings.append(report.existing(
        STEP, "probe.ok",
        f"the ACME challenge path round-trips through http://{apex}"))
    return True


def _nonce():
    import secrets as randomness

    return "mojo-" + randomness.token_hex(8)


def _certbot_failure(findings, code, message, out, err):
    detail = (err or out or "").strip().splitlines()
    tail = " ".join(detail[-4:]) if detail else "no output"
    return _fail(findings, code, f"{message}: {tail}", RATE_LIMIT_NOTE)


def issue(run, apex, email, findings):
    """Staging first, production only on its success."""
    rc, out, err = run(
        f"sudo certbot certonly --nginx --dry-run -d {apex} -m {email} "
        f"--agree-tos --non-interactive", timeout=600)
    if rc != 0:
        return _certbot_failure(
            findings, "certbot.dry_run_failed",
            f"the Let's Encrypt STAGING dry run for {apex} failed, so nothing "
            f"was requested from the production endpoint", out, err)
    findings.append(report.existing(
        STEP, "certbot.dry_run", f"the staging dry run for {apex} succeeded"))

    rc, out, err = run(
        f"sudo certbot --nginx -d {apex} -m {email} --agree-tos "
        f"--non-interactive --redirect", timeout=600)
    if rc != 0:
        return _certbot_failure(
            findings, "certbot.failed",
            f"issuing a certificate for {apex} failed even though the staging "
            f"dry run passed", out, err)
    findings.append(report.existing(
        STEP, "certbot.issued", f"Let's Encrypt issued a certificate for {apex}"))
    return True


def verify_https(run, apex, findings):
    rc, out, err = run(
        f"curl -fsS -o /dev/null -w '%{{http_code}}' https://{apex}/api/version",
        timeout=60)
    if rc != 0 or (out or "").strip() != "200":
        return _fail(findings, "https.unverified",
                     f"https://{apex}/api/version did not answer 200 with a "
                     f"validating chain ({err or out or 'no answer'})",
                     "the certificate was issued; this is now an nginx or app "
                     "problem — `sudo nginx -t`, then "
                     "`journalctl -u mojo-asgi -n 100`")
    findings.append(report.existing(
        STEP, "https.ok", f"https://{apex}/api/version answers 200"))
    return True


def configure_certificate(run, apex, email, expected_ip=None, nonce=None):
    """The whole sequence. Returns findings; stops at the first failure."""
    findings = []

    # 1 — every run, before anything reads the vhost.
    if not rewrite_server_name(run, apex, findings):
        return findings

    # 2 — the rate-limit guard, and the resumed-node fix.
    usable, reason = certificate_is_usable(run, apex)
    if usable:
        findings.append(report.existing(
            STEP, "certbot.skipped",
            f"{reason} — skipping certbot entirely; only the nginx paths need "
            f"re-pointing after ec2_deploy.sh reset them"))
        rewrite_cert_paths(run, apex, findings)
        verify_https(run, apex, findings)
        return findings
    findings.append(report.missing(
        STEP, "certbot.needed", reason,
        f"configure requests one from Let's Encrypt for {apex}"))

    # 3, 4 — free checks that catch the two common causes of a failed challenge.
    if not apex_resolves(run, apex, findings, expected_ip=expected_ip):
        return findings
    if not probe_round_trip(run, apex, findings, nonce=nonce):
        return findings

    # 5, 6 — the only steps that talk to Let's Encrypt.
    if not issue(run, apex, email, findings):
        return findings

    # 7 — certbot reloads nginx itself, but prove it rather than assume it.
    if not reload_nginx(run, findings, "nginx.post_certbot"):
        return findings
    verify_https(run, apex, findings)
    return findings


FLEET_HANDOFF = """\
This environment has more than one node, so certificates are NOT issued here.

`certbot --nginx` rewrites the vhost with an `include
/etc/letsencrypt/options-ssl-nginx.conf` line, and that file exists only on the
node certbot ran on — so the moment the mutated config reaches a second node,
`nginx -t` fails there and that node serves nothing.

A fleet's certificates belong to the dnsman/edge plane instead: dnsman issues
them over DNS-01 with the keys held in KMS, and mojo.apps.edge renders a vhost
per domain into a generation under /opt/api/var/edge, validates it, and
installs it on every node through the one-line conf.d/mojo.conf include.

  1. issue the certificate for {apex} in dnsman
  2. turn on EDGE_CONVERGE_ENABLED for this environment
  3. the edge plane installs it fleet-wide on its next convergence

See docs/django_developer/deploy/provision.md for the full hand-off, including
what to do when GROWING a single node into a fleet — the certbot-mutated
app.conf must not be copied; move the certificate to the edge plane instead.
"""
