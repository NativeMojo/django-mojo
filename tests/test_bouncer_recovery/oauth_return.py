"""Regression: an OAuth provider's return is not challenged by Bouncer.

Apple returns with a cross-site form POST, and browsers drop the SameSite=Lax
Bouncer pass across that hop, so /auth?code=…&state=… served the challenge and
the sign-in looped back to the login page (maestromojo.com, 2026-09-24). /begin
now records the pass in the OAuth state; the gated page honours it.
"""
from testit import helpers as th
from testit.client import RestClient

from .integration import config, pass_gate


def _begin(client):
    resp = client.get('/api/auth/oauth/github/begin')
    assert resp.status_code == 200 and resp.json.data.state, f'/begin must mint a state: {resp.status_code}'
    return resp.json.data.state


def _page(client, state):
    return client.get(f'/auth?code=provider-code&state={state}')


@th.django_unit_test('an OAuth return from a visitor who passed Bouncer skips the challenge')
def test_oauth_return_skips_gate(opts):
    passed, returning = RestClient(opts.client.host), RestClient(opts.client.host)
    try:
        pass_gate(passed)
        state = _begin(passed)
        # A fresh cookie jar is the provider's cross-site return: no pass rides it.
        page = _page(returning, state)
        config(page, 'mat-hosted-bouncer')
        assert 'mbg-config' not in page.text, 'a passed visitor returning from the provider was challenged again'
    finally:
        passed.session.close()
        returning.session.close()


@th.django_unit_test('a state minted without a Bouncer pass does not open the gate, and the challenge keeps code/state')
def test_oauth_state_without_pass_is_challenged(opts):
    client = RestClient(opts.client.host)
    try:
        state = _begin(client)
        challenge = config(_page(client, state))
        target = challenge.get('redirect_url', '')
        assert 'code=provider-code' in target and f'state={state}' in target, (
            f'the challenge must return to the OAuth landing with code and state: {target!r}')
    finally:
        client.session.close()
