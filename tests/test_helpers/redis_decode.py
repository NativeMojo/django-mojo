from testit import helpers as th
from testit import faker


@th.django_unit_setup()
def setup_redis_decode_tests(opts):
    """Setup function to ensure Redis is available for decode tests"""
    from mojo.helpers.redis import get_connection
    from mojo.helpers import logit

    try:
        # Test Redis connection
        redis_conn = get_connection()
        assert redis_conn is not None, "Redis connection should not be None"

        # Test ping
        ping_result = redis_conn.ping()
        assert ping_result == True, f"Redis ping should return True, got: {ping_result}"

        # Clean up any decode test keys
        test_keys = [
            'decode_test_*',
            'test_decode_*',
            'string_test_*',
            'set_test_*',
            'list_test_*',
            'hash_test_*'
        ]

        keys_cleaned = 0
        for pattern in test_keys:
            keys = redis_conn.keys(pattern)
            if keys:
                deleted = redis_conn.delete(*keys)
                keys_cleaned += deleted

        logit.info(f"Redis decode tests setup complete, cleaned {keys_cleaned} test keys")

    except Exception as e:
        logit.error(f"Redis decode tests setup failed: {e}")
        raise Exception(f"Redis is not available for decode testing: {e}")


@th.django_unit_test()
def test_redis_connection_returns_strings(opts):
    """Test that basic Redis connection operations return strings, not bytes"""
    from mojo.helpers.redis import get_connection

    redis_conn = get_connection()

    # Test basic get/set
    test_key = 'string_test_basic'
    test_value = 'hello_world_123'

    redis_conn.set(test_key, test_value)
    result = redis_conn.get(test_key)

    assert isinstance(result, str), f"GET should return str, got {type(result)}: {result}"
    assert result == test_value, f"Expected '{test_value}', got '{result}'"

    # Test with unicode characters
    unicode_key = 'string_test_unicode'
    unicode_value = 'héllo_wørld_测试_🚀'

    redis_conn.set(unicode_key, unicode_value)
    unicode_result = redis_conn.get(unicode_key)

    assert isinstance(unicode_result, str), f"GET with unicode should return str, got {type(unicode_result)}: {unicode_result}"
    assert unicode_result == unicode_value, f"Expected '{unicode_value}', got '{unicode_result}'"

    # Clean up
    redis_conn.delete(test_key, unicode_key)


@th.django_unit_test()
def test_redis_set_operations_return_strings(opts):
    """Test that Redis set operations return strings"""
    from mojo.helpers.redis import get_connection

    redis_conn = get_connection()
    set_key = 'set_test_decode'

    # Add items to set
    test_items = ['item1', 'item2', 'item3', 'special_chars_!@#$%']
    for item in test_items:
        redis_conn.sadd(set_key, item)

    # Test SMEMBERS
    members = redis_conn.smembers(set_key)
    assert isinstance(members, set), f"SMEMBERS should return set, got {type(members)}: {members}"

    for member in members:
        assert isinstance(member, str), f"Set member should be str, got {type(member)}: {member}"
        assert member in test_items, f"Unexpected member '{member}', expected one of: {test_items}"

    assert len(members) == len(test_items), f"Expected {len(test_items)} members, got {len(members)}: {members}"

    # Test SISMEMBER
    for item in test_items:
        is_member = redis_conn.sismember(set_key, item)
        assert is_member == True, f"'{item}' should be member of set"

    # Clean up
    redis_conn.delete(set_key)


@th.django_unit_test()
def test_redis_list_operations_return_strings(opts):
    """Test that Redis list operations return strings"""
    from mojo.helpers.redis import get_connection

    redis_conn = get_connection()
    list_key = 'list_test_decode'

    # Push items to list
    test_items = ['first', 'second', 'third', 'unicode_测试']
    for item in test_items:
        redis_conn.lpush(list_key, item)

    # Test LRANGE
    list_items = redis_conn.lrange(list_key, 0, -1)
    assert isinstance(list_items, list), f"LRANGE should return list, got {type(list_items)}: {list_items}"

    for item in list_items:
        assert isinstance(item, str), f"List item should be str, got {type(item)}: {item}"
        assert item in test_items, f"Unexpected item '{item}', expected one of: {test_items}"

    # Test BRPOP (blocking pop)
    popped = redis_conn.brpop(list_key, timeout=1)
    assert popped is not None, f"BRPOP should return item, got None"
    assert isinstance(popped, tuple), f"BRPOP should return tuple, got {type(popped)}: {popped}"
    assert len(popped) == 2, f"BRPOP should return 2-tuple, got {len(popped)}: {popped}"

    key_name, item_value = popped
    assert isinstance(key_name, str), f"BRPOP key should be str, got {type(key_name)}: {key_name}"
    assert isinstance(item_value, str), f"BRPOP value should be str, got {type(item_value)}: {item_value}"
    assert key_name == list_key, f"Expected key '{list_key}', got '{key_name}'"
    assert item_value in test_items, f"Unexpected value '{item_value}', expected one of: {test_items}"

    # Clean up
    redis_conn.delete(list_key)


@th.django_unit_test()
def test_redis_hash_operations_return_strings(opts):
    """Test that Redis hash operations return strings"""
    from mojo.helpers.redis import get_connection

    redis_conn = get_connection()
    hash_key = 'hash_test_decode'

    # Set hash fields
    test_fields = {
        'name': 'John Doe',
        'email': 'john@example.com',
        'status': 'active',
        'unicode_field': 'value_with_测试_chars'
    }

    for field, value in test_fields.items():
        redis_conn.hset(hash_key, field, value)

    # Test HGET
    for field, expected_value in test_fields.items():
        result = redis_conn.hget(hash_key, field)
        assert isinstance(result, str), f"HGET should return str, got {type(result)}: {result}"
        assert result == expected_value, f"Expected '{expected_value}', got '{result}'"

    # Test HGETALL
    all_fields = redis_conn.hgetall(hash_key)
    assert isinstance(all_fields, dict), f"HGETALL should return dict, got {type(all_fields)}: {all_fields}"

    for field, value in all_fields.items():
        assert isinstance(field, str), f"Hash field should be str, got {type(field)}: {field}"
        assert isinstance(value, str), f"Hash value should be str, got {type(value)}: {value}"
        assert field in test_fields, f"Unexpected field '{field}', expected one of: {list(test_fields.keys())}"
        assert value == test_fields[field], f"For field '{field}', expected '{test_fields[field]}', got '{value}'"

    # Clean up
    redis_conn.delete(hash_key)


@th.django_unit_test()
def test_redis_pool_client_decode_consistency(opts):
    """Test that Redis pool client has same decode behavior as shared connection"""
    from mojo.helpers.redis import get_connection
    from mojo.helpers.redis.pool import RedisBasePool

    shared_conn = get_connection()
    pool = RedisBasePool('test_decode_consistency')
    pool_client = pool.redis_client

    test_key = 'decode_test_consistency'
    test_value = 'consistency_test_value_with_unicode_测试'

    # Set value with shared connection
    shared_conn.set(test_key, test_value)

    # Get value with both connections
    shared_result = shared_conn.get(test_key)
    pool_result = pool_client.get(test_key)

    # Both should return strings
    assert isinstance(shared_result, str), f"Shared connection should return str, got {type(shared_result)}: {shared_result}"
    assert isinstance(pool_result, str), f"Pool client should return str, got {type(pool_result)}: {pool_result}"

    # Both should return same value
    assert shared_result == test_value, f"Shared connection: expected '{test_value}', got '{shared_result}'"
    assert pool_result == test_value, f"Pool client: expected '{test_value}', got '{pool_result}'"
    assert shared_result == pool_result, f"Results should match: shared='{shared_result}', pool='{pool_result}'"

    # Test set operations consistency
    pool.add('decode_item1')
    pool.add('decode_item2')

    # Use shared connection to read pool data
    pool_set_key = pool.all_items_set_key
    set_members = shared_conn.smembers(pool_set_key)

    for member in set_members:
        assert isinstance(member, str), f"Set member from shared conn should be str, got {type(member)}: {member}"

    # Use pool methods to verify consistency
    pool_members = pool.list_all()
    for member in pool_members:
        assert isinstance(member, str), f"Pool list_all should return str, got {type(member)}: {member}"

    # Clean up
    shared_conn.delete(test_key)
    pool.clear()


@th.django_unit_test()
def test_redis_decode_edge_cases(opts):
    """Test Redis decode behavior with edge cases"""
    from mojo.helpers.redis import get_connection

    redis_conn = get_connection()

    # Test empty string
    empty_key = 'decode_test_empty'
    redis_conn.set(empty_key, '')
    empty_result = redis_conn.get(empty_key)
    assert isinstance(empty_result, str), f"Empty string should return str, got {type(empty_result)}: {empty_result}"
    assert empty_result == '', f"Expected empty string, got '{empty_result}'"

    # Test string with only whitespace
    whitespace_key = 'decode_test_whitespace'
    whitespace_value = '   \t\n   '
    redis_conn.set(whitespace_key, whitespace_value)
    whitespace_result = redis_conn.get(whitespace_key)
    assert isinstance(whitespace_result, str), f"Whitespace should return str, got {type(whitespace_result)}: {whitespace_result}"
    assert whitespace_result == whitespace_value, f"Expected '{repr(whitespace_value)}', got '{repr(whitespace_result)}'"

    # Test numeric strings
    numeric_key = 'decode_test_numeric'
    numeric_value = '12345'
    redis_conn.set(numeric_key, numeric_value)
    numeric_result = redis_conn.get(numeric_key)
    assert isinstance(numeric_result, str), f"Numeric string should return str, got {type(numeric_result)}: {numeric_result}"
    assert numeric_result == numeric_value, f"Expected '{numeric_value}', got '{numeric_result}'"

    # Test special characters
    special_key = 'decode_test_special'
    special_value = '!@#$%^&*()_+-=[]{}|;:,.<>?'
    redis_conn.set(special_key, special_value)
    special_result = redis_conn.get(special_key)
    assert isinstance(special_result, str), f"Special chars should return str, got {type(special_result)}: {special_result}"
    assert special_result == special_value, f"Expected '{special_value}', got '{special_result}'"

    # Test non-existent key
    nonexistent_result = redis_conn.get('nonexistent_key_decode_test')
    assert nonexistent_result is None, f"Non-existent key should return None, got {type(nonexistent_result)}: {nonexistent_result}"

    # Clean up
    redis_conn.delete(empty_key, whitespace_key, numeric_key, special_key)


@th.django_unit_test()
def test_redis_settings_integration_with_decode_responses(opts):
    """Test that Redis settings integration properly enforces decode_responses=True"""
    from mojo.helpers.redis.client import get_redis_config
    from mojo.helpers.redis import get_connection
    from mojo.helpers.settings import settings

    # Test that get_redis_config always includes decode_responses=True
    config = get_redis_config()
    assert isinstance(config, dict), f"get_redis_config should return dict, got {type(config)}: {config}"
    assert 'decode_responses' in config, f"Config should contain decode_responses key: {list(config.keys())}"
    assert config['decode_responses'] == True, f"decode_responses should be True, got: {config['decode_responses']}"

    # Test that connection created with this config works properly
    redis_conn = get_connection()
    test_key = 'settings_integration_test'
    test_value = 'test_with_settings_integration'

    redis_conn.set(test_key, test_value)
    result = redis_conn.get(test_key)

    assert isinstance(result, str), f"With settings integration, result should be str, got {type(result)}: {result}"
    assert result == test_value, f"Expected '{test_value}', got '{result}'"

    # Test that settings can be accessed properly
    redis_host = settings.get('REDIS_HOST', 'localhost')
    assert isinstance(redis_host, str), f"REDIS_HOST setting should be string, got {type(redis_host)}: {redis_host}"

    redis_port = settings.get('REDIS_PORT', 6379)
    assert isinstance(redis_port, int), f"REDIS_PORT setting should be int, got {type(redis_port)}: {redis_port}"

    redis_database = settings.get('REDIS_DATABASE', 0)
    assert isinstance(redis_database, int), f"REDIS_DATABASE setting should be int, got {type(redis_database)}: {redis_database}"

    # Verify that even with potential custom REDIS_DB config, decode_responses remains True
    custom_redis_db = settings.get('REDIS_DB', {})
    if custom_redis_db and isinstance(custom_redis_db, dict):
        # Even if custom config exists, our get_redis_config should still enforce decode_responses=True
        config_with_custom = get_redis_config()
        assert config_with_custom['decode_responses'] == True, f"decode_responses should be True even with custom config: {config_with_custom}"

    # Clean up
    redis_conn.delete(test_key)
