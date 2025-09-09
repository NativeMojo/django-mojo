from testit import helpers as th
from testit import faker
import time
import threading
from unittest.mock import Mock, patch

@th.django_unit_setup()
def setup_redis_pools(opts):
    """Setup function to ensure Redis is available and clean for pool tests"""
    from mojo.helpers.redis import get_connection
    from mojo.helpers import logit
    from mojo.apps.account.models import Group

    for i in range(1, 4):
        i, _ =Group.objects.get_or_create(id=i, defaults={'name': f"Group {i}", 'is_active': True})

    try:
        # Test Redis connection
        redis_conn = get_connection()
        assert redis_conn is not None, "Redis connection should not be None"

        # Test ping
        ping_result = redis_conn.ping()
        assert ping_result == True, f"Redis ping should return True, got: {ping_result}"

        # Clean up any test keys that might exist
        test_patterns = [
            'test_pool*',
            'test_add_remove*',
            'test_checkout*',
            'test_next_available*',
            'test_destroy*',
            'test_model_*',
            'test_specific_instance*',
            'test_concurrent*',
            'test_edge_cases*',
            'test_performance*',
            'test_shared_connection*',
            'test_init_pool*'
        ]

        keys_cleaned = 0
        for pattern in test_patterns:
            keys = redis_conn.keys(pattern)
            if keys:
                deleted = redis_conn.delete(*keys)
                keys_cleaned += deleted
                assert deleted == len(keys), f"Expected to delete {len(keys)} keys for pattern {pattern}, but deleted {deleted}"

        logit.info(f"Redis connection OK (using MOJO settings helper), cleaned up {keys_cleaned} test keys")

    except AssertionError as e:
        logit.error(f"Redis setup assertion failed: {e}")
        raise Exception(f"Redis setup assertion failed: {e}")
    except Exception as e:
        logit.error(f"Redis setup failed: {e}")
        raise Exception("Redis is not available for testing. Please ensure Redis is running and MOJO settings are configured properly.")


@th.django_unit_test()
def test_redis_base_pool_initialization(opts):
    """Test basic RedisBasePool initialization"""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_pool', default_timeout=15)
    assert pool.pool_key == 'test_pool', f"Expected pool_key 'test_pool', got '{pool.pool_key}'"
    assert pool.default_timeout == 15, f"Expected default_timeout 15, got {pool.default_timeout}"
    assert pool.available_list_key == 'test_pool:list', f"Expected available_list_key 'test_pool:list', got '{pool.available_list_key}'"
    assert pool.all_items_set_key == 'test_pool:set', f"Expected all_items_set_key 'test_pool:set', got '{pool.all_items_set_key}'"
    assert pool.redis_client is not None, "Redis client should not be None"


@th.django_unit_test()
def test_redis_base_pool_add_remove(opts):
    """Test adding and removing items from pool"""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_add_remove')
    pool.clear()  # Start clean

    # Test adding items
    assert pool.add('item1') == True, "Failed to add 'item1' to pool"
    assert pool.add('item2') == True, "Failed to add 'item2' to pool"
    assert pool.add('item3') == True, "Failed to add 'item3' to pool"

    # Test adding duplicate (should return False)
    assert pool.add('item1') == False, "Adding duplicate 'item1' should return False"

    # Test listing items
    all_items = pool.list_all()
    assert len(all_items) == 3, f"Expected 3 items in pool, got {len(all_items)}: {all_items}"
    assert 'item1' in all_items, f"'item1' not found in all_items: {all_items}"
    assert 'item2' in all_items, f"'item2' not found in all_items: {all_items}"
    assert 'item3' in all_items, f"'item3' not found in all_items: {all_items}"

    available_items = pool.list_available()
    assert len(available_items) == 3, f"Expected 3 available items, got {len(available_items)}: {available_items}"

    # Test removing items
    assert pool.remove('item2') == True, "Failed to remove 'item2' from pool"
    assert pool.remove('item2') == False, "Removing 'item2' again should return False (already removed)"

    all_items = pool.list_all()
    assert len(all_items) == 2, f"Expected 2 items after removal, got {len(all_items)}: {all_items}"
    assert 'item2' not in all_items, f"'item2' should not be in all_items after removal: {all_items}"

    pool.clear()


@th.django_unit_test()
def test_redis_base_pool_checkout_checkin(opts):
    """Test checking out and checking in items"""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_checkout')
    pool.clear()

    # Add items
    pool.add('worker1')
    pool.add('worker2')
    pool.add('worker3')

    # Test checkout
    assert pool.checkout('worker1') == True, "Failed to checkout 'worker1'"
    assert pool.checkout('worker1') == False, "Checking out 'worker1' again should return False (already checked out)"
    assert pool.checkout('nonexistent') == False, "Checking out non-existent item should return False"

    # Check available vs checked out
    available = pool.list_available()
    checked_out = pool.list_checked_out()

    assert len(available) == 2, f"Expected 2 available items, got {len(available)}: {available}"
    assert len(checked_out) == 1, f"Expected 1 checked out item, got {len(checked_out)}: {checked_out}"
    assert 'worker1' in checked_out, f"'worker1' should be in checked_out: {checked_out}"
    assert 'worker1' not in available, f"'worker1' should not be in available: {available}"

    # Test checkin
    assert pool.checkin('worker1') == True, "Failed to checkin 'worker1'"
    assert pool.checkin('nonexistent') == False, "Checking in non-existent item should return False"

    # Verify item is back in available pool
    available = pool.list_available()
    checked_out = pool.list_checked_out()

    assert len(available) == 3, f"Expected 3 available items after checkin, got {len(available)}: {available}"
    assert len(checked_out) == 0, f"Expected 0 checked out items after checkin, got {len(checked_out)}: {checked_out}"
    assert 'worker1' in available, f"'worker1' should be back in available after checkin: {available}"

    pool.clear()


@th.django_unit_test()
def test_redis_base_pool_get_next_available(opts):
    """Test getting next available item from pool"""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_next_available')
    pool.clear()

    # Test empty pool
    item = pool.get_next_available(timeout=1)
    assert item is None, f"Empty pool should return None, got: {item}"

    # Add items and test getting them
    pool.add('task1')
    pool.add('task2')
    pool.add('task3')

    # Get items (should be FIFO due to lpush/brpop)
    item1 = pool.get_next_available(timeout=1)
    item2 = pool.get_next_available(timeout=1)
    item3 = pool.get_next_available(timeout=1)

    assert item1 is not None, f"First item should not be None, got: {item1}"
    assert item2 is not None, f"Second item should not be None, got: {item2}"
    assert item3 is not None, f"Third item should not be None, got: {item3}"

    # All items should be different
    items = {item1, item2, item3}
    assert len(items) == 3, f"Expected 3 unique items, got {len(items)} unique from [{item1}, {item2}, {item3}]"

    # Pool should be empty now
    available = pool.list_available()
    assert len(available) == 0, f"Pool should be empty after getting all items, but has: {available}"

    pool.clear()


@th.django_unit_test()
def test_redis_base_pool_clear_and_destroy(opts):
    """Test clearing and destroying the pool"""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_destroy')

    # Add items
    pool.add('item1')
    pool.add('item2')
    pool.checkout('item1')

    # Verify items exist
    assert len(pool.list_all()) == 2, f"Expected 2 total items, got {len(pool.list_all())}: {pool.list_all()}"
    assert len(pool.list_available()) == 1, f"Expected 1 available item, got {len(pool.list_available())}: {pool.list_available()}"
    assert len(pool.list_checked_out()) == 1, f"Expected 1 checked out item, got {len(pool.list_checked_out())}: {pool.list_checked_out()}"

    # Clear pool
    pool.clear()

    assert len(pool.list_all()) == 0, f"Expected 0 items after clear, got {len(pool.list_all())}: {pool.list_all()}"
    assert len(pool.list_available()) == 0, f"Expected 0 available items after clear, got {len(pool.list_available())}: {pool.list_available()}"
    assert len(pool.list_checked_out()) == 0, f"Expected 0 checked out items after clear, got {len(pool.list_checked_out())}: {pool.list_checked_out()}"

    # Test destroy (should be same as clear for this implementation)
    pool.add('item1')
    pool.destroy_pool()
    assert len(pool.list_all()) == 0, f"Expected 0 items after destroy, got {len(pool.list_all())}: {pool.list_all()}"


@th.django_unit_test()
def test_redis_base_pool_checkout_with_timeout(opts):
    """Test checkout with timeout functionality"""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_checkout_timeout')
    pool.clear()

    pool.add('item1')

    # First checkout should succeed immediately
    assert pool.checkout('item1', timeout=1) == True, "First checkout of 'item1' should succeed"

    # Second checkout should timeout
    start_time = time.time()
    result = pool.checkout('item1', timeout=2)
    elapsed = time.time() - start_time

    assert result == False, f"Second checkout should timeout and return False, got: {result}"
    assert elapsed >= 2, f"Checkout should have waited for timeout (2s), but elapsed time was {elapsed:.2f}s"

    pool.clear()


@th.django_unit_test()
def test_redis_model_pool_initialization(opts):
    """Test RedisModelPool initialization"""
    from mojo.helpers.redis.pool import RedisModelPool
    from django.db import models

    # Create mock model class
    class MockModel(models.Model):
        name = models.CharField(max_length=100)
        status = models.CharField(max_length=20)

        class Meta:
            app_label = 'test'

    query_dict = {'status': 'active'}
    pool = RedisModelPool(MockModel, query_dict, 'test_model_pool')

    assert pool.model_cls == MockModel, f"Expected model_cls to be MockModel, got {pool.model_cls}"
    assert pool.query_dict == query_dict, f"Expected query_dict {query_dict}, got {pool.query_dict}"
    assert pool.pool_key == 'test_model_pool', f"Expected pool_key 'test_model_pool', got '{pool.pool_key}'"


@th.django_unit_test()
def test_redis_model_pool_mock_operations(opts):
    """Test RedisModelPool operations with mocked Django models"""
    from mojo.helpers.redis.pool import RedisModelPool
    from mojo.apps.account.models import Group
    from django.db import models

    # Create mock model class


    # Mock some model instances
    instance1 = Group.objects.get(pk=1)
    instance2 = Group.objects.get(pk=2)

    query_dict = {'is_active': True, "pk__in": [1, 2]}
    pool = RedisModelPool(Group, query_dict, 'test_model_operations')
    pool.clear()

    # Test adding instances
    assert pool.add_to_pool(instance1) == True, "Failed to add instance1 to pool"
    assert pool.add_to_pool(instance2) == True, "Failed to add instance2 to pool"

    # Test pool contents
    all_items = pool.list_all()
    assert '1' in all_items, f"Instance '1' not found in pool: {all_items}"
    assert '2' in all_items, f"Instance '2' not found in pool: {all_items}"

    # Test removing instance
    assert pool.remove_from_pool(instance1) == True, "Failed to remove instance1 from pool"
    assert pool.remove_from_pool(instance1) == False, "Removing instance1 again should return False"

    all_items = pool.list_all()
    assert '1' not in all_items, f"Instance '1' should not be in pool after removal: {all_items}"
    assert '2' in all_items, f"Instance '2' should still be in pool: {all_items}"

    pool.clear()


@th.django_unit_test()
def test_redis_model_pool_get_specific_instance(opts):
    """Test getting specific instances from model pool"""
    from mojo.helpers.redis.pool import RedisModelPool
    from django.db import models
    from mojo.apps.account.models import Group

    instance1 = Group.objects.get(pk=1)

    pool = RedisModelPool(Group, {'is_active': True, "pk__in": [1, 2]}, 'test_specific_instance')
    pool.clear()

    # Add instance
    pool.add_to_pool(instance1)

    # Test getting specific instance
    assert pool.get_specific_instance(instance1) == True, "Failed to get specific instance1 from pool"
    assert pool.get_specific_instance(instance1) == False, "Getting specific instance1 again should return False (already taken)"

    # Test returning instance
    assert pool.return_instance(instance1) == True, "Failed to return instance1 to pool"

    # Should be available again
    assert pool.get_specific_instance(instance1) == True, "Should be able to get specific mock_instance again after returning it"

    pool.clear()


@th.django_unit_test()
def test_redis_pool_concurrent_access(opts):
    """Test pool behavior under concurrent access"""
    from mojo.helpers.redis.pool import RedisBasePool
    import threading
    import time

    pool = RedisBasePool('test_concurrent')
    pool.clear()

    # Add items
    for i in range(10):
        pool.add(f'item{i}')

    results = []
    errors = []

    def worker():
        try:
            item = pool.get_next_available(timeout=5)
            if item:
                results.append(item)
                time.sleep(0.1)  # Simulate work
                pool.checkin(item)
        except Exception as e:
            errors.append(e)

    # Create multiple threads
    threads = []
    for _ in range(5):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()

    # Wait for all threads
    for t in threads:
        t.join()

    # Check results
    assert len(errors) == 0, f"Concurrent access caused errors: {errors}"
    assert len(results) == 5, f"Expected 5 results from concurrent workers, got {len(results)}: {results}"
    assert len(set(results)) == 5, f"Some workers got the same item - results: {results}, unique: {set(results)}"

    # All items should be back in the pool
    available = pool.list_available()
    assert len(available) == 10, f"Expected all 10 items back in pool after concurrent test, got {len(available)}: {available}"

    pool.clear()


@th.django_unit_test()
def test_redis_pool_edge_cases(opts):
    """Test edge cases and error conditions"""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_edge_cases')
    pool.clear()

    # Test operations on empty pool
    assert pool.checkout('nonexistent') == False, "Checkout on empty pool should return False"
    assert pool.checkin('nonexistent') == False, "Checkin on empty pool should return False"
    assert pool.remove('nonexistent') == False, "Remove on empty pool should return False"
    assert len(pool.list_all()) == 0, f"Empty pool should have 0 items, got {len(pool.list_all())}: {pool.list_all()}"
    assert len(pool.list_available()) == 0, f"Empty pool should have 0 available items, got {len(pool.list_available())}: {pool.list_available()}"
    assert len(pool.list_checked_out()) == 0, f"Empty pool should have 0 checked out items, got {len(pool.list_checked_out())}: {pool.list_checked_out()}"

    # Test with empty string
    assert pool.add('') == True, "Should be able to add empty string to pool"
    assert pool.checkout('') == True, "Should be able to checkout empty string from pool"
    assert pool.checkin('') == True, "Should be able to checkin empty string to pool"
    assert pool.remove('') == True, "Should be able to remove empty string from pool"

    # Test clearing empty pool
    pool.clear()
    pool.clear()  # Should not error

    pool.destroy_pool()


@th.django_unit_test()
def test_redis_pool_performance(opts):
    """Test pool performance with many items"""
    from mojo.helpers.redis.pool import RedisBasePool
    import time

    pool = RedisBasePool('test_performance')
    pool.clear()

    # Test adding many items
    start_time = time.time()
    for i in range(100):
        pool.add(f'item{i}')
    add_time = time.time() - start_time

    # Test getting all items
    start_time = time.time()
    items = []
    for i in range(100):
        item = pool.get_next_available(timeout=1)
        if item:
            items.append(item)
    get_time = time.time() - start_time

    assert len(items) == 100, f"Expected to retrieve 100 items, got {len(items)}"
    assert add_time < 5.0, f"Adding 100 items took too long: {add_time:.2f}s (should be < 5.0s)"
    assert get_time < 5.0, f"Getting 100 items took too long: {get_time:.2f}s (should be < 5.0s)"

    pool.clear()


@th.django_unit_test()
def test_redis_pool_uses_shared_connection(opts):
    """Test that Redis pools use the shared connection from client.py"""
    from mojo.helpers.redis.pool import RedisBasePool
    from mojo.helpers.redis import get_connection

    # Create pool and get its Redis client
    pool = RedisBasePool('test_shared_connection')

    # Get shared connection
    shared_conn = get_connection()

    # Both should have decode_responses=True (indicating shared config)
    # We can test this by checking if string operations return strings, not bytes
    test_key = 'test_decode_check'

    pool.redis_client.set(test_key, 'test_value')
    pool_result = pool.redis_client.get(test_key)

    shared_result = shared_conn.get(test_key)

    # Both should return strings (not bytes) due to decode_responses=True
    assert isinstance(pool_result, str), f"Pool client should return decoded strings, got {type(pool_result)}: {pool_result}"
    assert isinstance(shared_result, str), f"Shared client should return decoded strings, got {type(shared_result)}: {shared_result}"
    assert pool_result == shared_result == 'test_value', f"Both clients should return same value 'test_value', pool: {pool_result}, shared: {shared_result}"

    # Clean up
    pool.redis_client.delete(test_key)
    pool.clear()


@th.django_unit_test()
def test_redis_model_pool_with_init_pool(opts):
    """Test RedisModelPool init_pool functionality with mocks"""
    from mojo.helpers.redis.pool import RedisModelPool
    from django.db import models
    from mojo.apps.account.models import Group
    from unittest.mock import Mock, patch

    # Create mock instances
    mock_instances = []
    for i in range(3):
        instance = Group.objects.get(pk=i+1)
        mock_instances.append(instance)

    pool = RedisModelPool(Group, {'is_active': True, "pk__in": [1, 2, 3]}, 'test_init_pool')
    pool.init_pool()

    # Check that all instances were added to pool
    all_items = pool.list_all()
    assert '1' in all_items, f"Instance '1' should be in pool after init: {all_items}"
    assert '2' in all_items, f"Instance '2' should be in pool after init: {all_items}"
    assert '3' in all_items, f"Instance '3' should be in pool after init: {all_items}"

    available_items = pool.list_available()
    assert len(available_items) == 3, f"Expected 3 available items after init, got {len(available_items)}: {available_items}"

    pool.clear()


@th.django_unit_test()
def test_redis_decode_responses_fix(opts):
    """Test that Redis operations return strings, not bytes (decode_responses=True fix)"""
    from mojo.helpers.redis.pool import RedisBasePool
    from mojo.helpers.redis import get_connection

    # Test both pool client and shared connection
    pool = RedisBasePool('test_decode_responses')
    pool.clear()

    shared_conn = get_connection()

    # Test key-value operations
    test_key = 'test_decode_string'
    test_value = 'test_string_value'

    shared_conn.set(test_key, test_value)
    result = shared_conn.get(test_key)
    assert isinstance(result, str), f"GET should return string, got {type(result)}: {result}"
    assert result == test_value, f"Expected '{test_value}', got '{result}'"

    # Test set operations (used by pool.list_all())
    pool.add('item1')
    pool.add('item2')
    pool.add('item3')

    all_items = pool.list_all()
    assert isinstance(all_items, set), f"list_all should return set, got {type(all_items)}: {all_items}"

    for item in all_items:
        assert isinstance(item, str), f"Set member should be string, got {type(item)}: {item}"
        assert item in ['item1', 'item2', 'item3'], f"Unexpected item in set: {item}"

    # Test list operations (used by pool.list_available())
    available_items = pool.list_available()
    assert isinstance(available_items, list), f"list_available should return list, got {type(available_items)}: {available_items}"

    for item in available_items:
        assert isinstance(item, str), f"List member should be string, got {type(item)}: {item}"
        assert item in ['item1', 'item2', 'item3'], f"Unexpected item in list: {item}"

    # Test brpop operation (used by pool.get_next_available())
    next_item = pool.get_next_available(timeout=1)
    assert isinstance(next_item, str), f"get_next_available should return string, got {type(next_item)}: {next_item}"
    assert next_item in ['item1', 'item2', 'item3'], f"Unexpected next_item: {next_item}"

    # Test that raw Redis operations also return strings
    raw_client = pool.redis_client

    # Test SMEMBERS (set members)
    raw_set_members = raw_client.smembers(pool.all_items_set_key)
    for member in raw_set_members:
        assert isinstance(member, str), f"SMEMBERS should return strings, got {type(member)}: {member}"

    # Test LRANGE (list range)
    raw_list_items = raw_client.lrange(pool.available_list_key, 0, -1)
    for item in raw_list_items:
        assert isinstance(item, str), f"LRANGE should return strings, got {type(item)}: {item}"

    # Clean up
    shared_conn.delete(test_key)
    pool.clear()
