from testit import helpers as th


def _getter(values):
    return lambda key, default=None: values.get(key, default)


@th.django_unit_test()
def test_reader_url_resolution(opts):
    from mojo.helpers.redis import client

    assert client._resolve_reader_url(_getter({})) is None, (
        "An unconfigured reader must resolve to the primary fallback")

    explicit_url = "rediss://reader-user:reader-pass@reader.example:6380/4"
    assert client._resolve_reader_url(_getter({
        "REDIS_READER_URL": explicit_url,
    })) == explicit_url, "REDIS_READER_URL must be used verbatim"

    parts_url = client._resolve_reader_url(_getter({
        "REDIS_SERVER": "primary.example",
        "REDIS_PORT": 6381,
        "REDIS_DB_INDEX": 2,
        "REDIS_USERNAME": "primary-user",
        "REDIS_PASSWORD": "primary-pass",
        "REDIS_SCHEME": "rediss",
        "REDIS_READER_SERVER": "reader.example",
    }))
    assert parts_url == (
        "rediss://primary-user:primary-pass@reader.example:6381/2"
    ), "Reader parts should inherit the primary connection settings"

    primary_url_reader = client._resolve_reader_url(_getter({
        "REDIS_URL": "redis://:pw@primary.example:6379/3",
        "REDIS_READER_SERVER": "reader.example",
    }))
    assert primary_url_reader == "redis://:pw@reader.example:6379/3", (
        "Reader parts must inherit credentials and DB parsed from REDIS_URL")

    encoded_slash_reader = client._resolve_reader_url(_getter({
        "REDIS_URL": (
            "rediss://primary%2Fuser:primary%2Fcredential@"
            "primary.example:6379/3"),
        "REDIS_READER_SERVER": "reader.example",
    }))
    assert encoded_slash_reader == (
        "rediss://primary%2Fuser:primary%2Fcredential@reader.example:6379/3"
    ), "Inherited URL credentials must preserve encoded path separators"

    overridden_url = client._resolve_reader_url(_getter({
        "REDIS_URL": "redis://:primary-pass@primary.example:6379/3",
        "REDIS_READER_SERVER": "reader.example",
        "REDIS_READER_PORT": 6382,
        "REDIS_READER_DB_INDEX": 5,
        "REDIS_READER_USERNAME": "reader-user",
        "REDIS_READER_PASSWORD": "reader-pass",
        "REDIS_READER_SCHEME": "rediss",
    }))
    assert overridden_url == (
        "rediss://reader-user:reader-pass@reader.example:6382/5"
    ), "Explicit reader parts must override inherited connection values"

    slash_parts_url = client._resolve_reader_url(_getter({
        "REDIS_READER_SERVER": "reader.example",
        "REDIS_READER_USERNAME": "reader/user",
        "REDIS_READER_PASSWORD": "reader/credential",
    }))
    assert slash_parts_url == (
        "rediss://reader%2Fuser:reader%2Fcredential@reader.example:6379/0"
    ), "Explicit reader credentials must encode path separators"

    cluster_url = client._resolve_reader_url(_getter({
        "REDIS_CLUSTER": True,
        "REDIS_READER_SERVER": "reader.example",
    }))
    assert cluster_url is None, (
        "Cluster mode must ignore the standalone reader endpoint")


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


@th.django_unit_test()
def test_standalone_reader_client_reads_primary_data(opts):
    from mojo.helpers.redis import client

    primary = client.get_connection()
    reader = client._create_client(
        client._build_url(), max_conn=8, connect_timeout=2, socket_timeout=2)
    key = "test:helpers:redis_reader:constructed"
    primary.delete(key)
    try:
        assert reader.ping() is True, (
            "A separately constructed standalone reader client must connect")
        primary.set(key, "visible-through-reader")
        assert reader.get(key) == "visible-through-reader", (
            "The standalone reader client must read data written by primary")
    finally:
        primary.delete(key)
        reader.close()
