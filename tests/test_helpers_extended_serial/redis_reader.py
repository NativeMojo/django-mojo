"""Redis reader-singleton test moved out of tests/test_helpers/redis_reader.py.

It rebinds the module-global `_READER_CLIENT` / `_READER_URL` singletons on
`mojo.helpers.redis.client` — process-global state every parallel module reads
through `get_connection()` — so it runs only in the opt-in serial tier
(maestro item #2558).

There is no seam to convert it to: the assertions ARE about that module state
(an unconfigured reader must hand back the primary singleton, and the primary
must never be aliased into `_READER_CLIENT`), and the reset is what makes the
resolution run at all. The pure `_resolve_reader_url` coverage — including the
credential-encoding contracts — stays in the default tier, where it already
injects its settings getter.
"""
from testit import helpers as th


@th.django_unit_test()
def test_unconfigured_reader_returns_primary_singleton(opts):
    from mojo.helpers.redis import client

    prior_reader_client = client._READER_CLIENT
    prior_reader_url = client._READER_URL
    try:
        client._READER_CLIENT = None
        client._READER_URL = -1
        primary = client.get_connection()
        reader = client.get_connection(reader=True)
        assert reader is primary, (
            "An unconfigured reader request must return the primary singleton")
        assert client._READER_CLIENT is None, (
            "The primary singleton must not be aliased into _READER_CLIENT")
    finally:
        client._READER_CLIENT = prior_reader_client
        client._READER_URL = prior_reader_url
