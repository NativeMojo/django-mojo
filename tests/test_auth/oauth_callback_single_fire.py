"""
Regression: the hosted OAuth ?code&state callback must fire completeOAuthLogin
exactly once.

Background (maestro item 315): login.html carried its own ?code&state handler
that duplicated the generic one in auth_base.html. Both fired on every callback,
so the single-use authorization code was POSTed twice — the second time to the
"google" endpoint, because auth_base's handler had already consumed the
sessionStorage "oauth_provider" value, leaving login.html's copy to fall back to
"google" for Apple/GitHub logins. These tests pin the invariant: exactly one
completion call site reaches a rendered page, it lives in auth_base (so every
page extending the base — including register.html — keeps it), and login.html's
template source carries none of its own.
"""
import os

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


def _render(template_name, group=None):
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context
    factory = RequestFactory()
    request = factory.get('/register')
    ctx = _auth_context(request, group=group)
    ctx['page_mode'] = 'register'
    ctx['page_title'] = 'Create Account'
    return render(request, template_name, ctx).content.decode('utf-8')


@th.django_unit_test("rendered login page fires completeOAuthLogin exactly once")
def test_login_html_single_oauth_completion(opts):
    html = _render('account/login.html')
    count = html.count('completeOAuthLogin(')
    assert_eq(count, 1,
              f"the rendered hosted login page must contain exactly ONE "
              f"completeOAuthLogin( call site (the generic auth_base handler); a "
              f"second call site double-spends the single-use OAuth code — got {count}")


@th.django_unit_test("rendered register page fires completeOAuthLogin exactly once")
def test_register_html_single_oauth_completion(opts):
    html = _render('account/register.html')
    count = html.count('completeOAuthLogin(')
    assert_eq(count, 1,
              f"the rendered register page must contain exactly ONE "
              f"completeOAuthLogin( call site — it comes from the shared auth_base "
              f"handler, proving the OAuth completion path stays in the base for "
              f"every page that extends it — got {count}")


@th.django_unit_test("login.html template source declares no completeOAuthLogin call of its own")
def test_login_html_source_has_no_oauth_completion(opts):
    import mojo.apps.account as account_pkg
    tmpl_path = os.path.join(
        os.path.dirname(account_pkg.__file__),
        'templates', 'account', 'login.html')
    assert_true(os.path.exists(tmpl_path),
                f"expected the hosted login template at {tmpl_path}")
    with open(tmpl_path, 'r') as fh:
        source = fh.read()
    assert_true('completeOAuthLogin' not in source,
                "login.html must NOT declare its own completeOAuthLogin call — the "
                "single completion path lives in auth_base.html; a copy here "
                "reintroduces the double-fire regression regardless of render path")
