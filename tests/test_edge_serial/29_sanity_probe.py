"""The canary's local probe, and the redirect that made it fail every deploy.

`sanity_check`'s last check is the one that matters — four checks pass on
broken code, and only "answer one real request" does not. It probed
`http://127.0.0.1/api/version`, and the vhost django-mojo ships 301s everything
on :80 except the ACME path. So the probe could only ever see nginx's redirect,
never the app: EVERY successful deploy was reported as a sanity-check failure
and rolled back, on every project running the shipped vhost.

The fix is the one `remote.py`'s convergence probe already made — https, and
unverified on the loopback, because the certificate names the site and the
probe deliberately dials 127.0.0.1. These tests pin both halves, and pin that
the leniency stops at the loopback: a probe aimed at a real hostname still
verifies, or "TLS is checked somewhere" quietly stops being true.
"""

from testit import helpers as th


@th.django_unit_test("the default local probe is https, not the redirect it used to follow")
def test_local_probe_defaults_to_https(opts):
    from mojo.apps.edge.services import sanity

    th.assert_true(sanity.LOCAL_PROBE.startswith("https://"),
                   f"the shipped :80 vhost 301s everything except the ACME "
                   f"path, so a plain-http probe never reaches the app: "
                   f"{sanity.LOCAL_PROBE}")


@th.django_unit_test("the management command and the service agree on that default")
def test_command_default_matches_the_service(opts):
    from mojo.apps.edge.management.commands import sanity_check
    from mojo.apps.edge.services import sanity
    import argparse

    parser = argparse.ArgumentParser()
    sanity_check.Command().add_arguments(parser)
    th.assert_eq(parser.get_default("url"), sanity.LOCAL_PROBE,
                 "two defaults that drift apart mean the canary probes one "
                 "URL and a hand-run probes another")


@th.django_unit_test("certificate verification is skipped only on the loopback")
def test_verification_is_skipped_only_for_loopback(opts):
    from mojo.apps.edge.services import sanity

    for url in ("https://127.0.0.1/api/version",
                "https://localhost/api/version",
                "https://[::1]/api/version"):
        th.assert_eq(sanity._verify_for(url), False,
                     f"a public certificate names the site, never the "
                     f"loopback — verifying {url} fails by construction")

    for url in ("https://example.com/api/version",
                "https://portal.example.com/api/version"):
        th.assert_eq(sanity._verify_for(url), True,
                     f"a probe aimed at a real name must still verify, or the "
                     f"leniency stops being scoped: {url}")

    th.assert_eq(sanity._verify_for("http://127.0.0.1/api/version"), True,
                 "plain http has nothing to verify; the flag must not become "
                 "a general 'be lenient' switch")


@th.django_unit_test("a redirect is a failure, not a pass")
def test_a_redirect_does_not_satisfy_the_probe(opts):
    from mojo.apps.edge.services import sanity

    class _Response:
        status_code = 301

    class _Session:
        trust_env = True
        verify = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return _Response()

    original = sanity.requests.Session
    sanity.requests.Session = _Session
    try:
        raised = ""
        try:
            sanity.check_request({"retries": 1, "delay": 0})
        except RuntimeError as err:
            raised = str(err)
    finally:
        sanity.requests.Session = original

    th.assert_in("301", raised,
                 "the probe follows no redirects on purpose — a 301 means "
                 "nginx answered and the app did not, which is exactly the "
                 "state this check exists to catch")
