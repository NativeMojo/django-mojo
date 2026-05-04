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
            'test_init_pool*',
            'test_skip_predicate*',
            'test_retry_after*'
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

    # Test that pool auto-initializes on first add_to_pool
    # Since the pool is not ready, add_to_pool(instance1) will call init_pool()
    # which adds both instance1 and instance2 (matching the query)
    assert pool.add_to_pool(instance1) == True, "Failed to add instance1 to pool (should trigger init)"

    # Verify pool is now initialized with both instances
    all_items = pool.list_all()
    assert '1' in all_items, f"Instance '1' not found in pool after auto-init: {all_items}"
    assert '2' in all_items, f"Instance '2' not found in pool after auto-init: {all_items}"

    # Second add should return False since instance2 was already added during init_pool
    assert pool.add_to_pool(instance2) == False, "instance2 should already be in pool from auto-init"

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
def test_redis_base_pool_skip_predicate(opts):
    """skip_predicate hides eligible items until the predicate flips."""
    from mojo.helpers.redis.pool import RedisBasePool

    skipped = {'item2'}

    def predicate(str_id):
        return str_id in skipped

    pool = RedisBasePool('test_skip_predicate_base', skip_predicate=predicate)
    pool.clear()
    pool.add('item1')
    pool.add('item2')
    pool.add('item3')

    seen = []
    for _ in range(2):
        item = pool.get_next_available(timeout=1)
        assert item is not None, "Expected non-None item while two non-skipped items are in pool"
        seen.append(item)
        pool.checkin(item)

    assert 'item2' not in seen, f"item2 should have been skipped, got: {seen}"
    assert set(seen) <= {'item1', 'item3'}, f"Only non-skipped items should surface, got: {seen}"

    skipped.clear()
    item = pool.get_next_available(timeout=1)
    assert item is not None, "After clearing skip set, predicate should allow an item"

    pool.clear()


@th.django_unit_test()
def test_redis_base_pool_skip_predicate_all_skipped_returns_none(opts):
    """When predicate skips every member, get_next_available returns None bounded by scard."""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_skip_predicate_all', skip_predicate=lambda x: True)
    pool.clear()
    pool.add('a')
    pool.add('b')
    pool.add('c')

    start = time.time()
    item = pool.get_next_available(timeout=1)
    elapsed = time.time() - start

    assert item is None, f"All-skipped pool should return None, got: {item}"
    assert elapsed < 5.0, f"Bounded skip retries should not block long, took {elapsed:.2f}s"
    available = pool.list_available()
    assert len(available) == 3, f"All items should remain in pool after skip cycle, got: {available}"

    pool.clear()


@th.django_unit_test()
def test_redis_base_pool_skip_predicate_raises(opts):
    """A raising predicate is treated as skip, never poisons the pool."""
    from mojo.helpers.redis.pool import RedisBasePool

    call_count = {'n': 0}

    def boom(str_id):
        call_count['n'] += 1
        raise RuntimeError("boom")

    pool = RedisBasePool('test_skip_predicate_raises', skip_predicate=boom)
    pool.clear()
    pool.add('alpha')
    pool.add('beta')

    item = pool.get_next_available(timeout=1)
    assert item is None, f"Raising predicate should drain budget and return None, got: {item}"
    assert call_count['n'] >= 2, f"Predicate should have been invoked for each item, got {call_count['n']}"

    available = pool.list_available()
    assert len(available) == 2, f"All items should remain after raising predicate, got: {available}"

    pool.clear()


@th.django_unit_test()
def test_redis_base_pool_skip_predicate_default_none_no_change(opts):
    """skip_predicate=None preserves original FIFO behaviour exactly."""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_skip_predicate_none')
    pool.clear()
    assert pool.skip_predicate is None, "Default skip_predicate should be None"

    pool.add('one')
    pool.add('two')
    pool.add('three')

    fetched = []
    for _ in range(3):
        item = pool.get_next_available(timeout=1)
        assert item is not None, "Default pool should yield each added item"
        fetched.append(item)

    assert set(fetched) == {'one', 'two', 'three'}, f"All items should surface, got: {fetched}"

    pool.clear()


def _seed_model_pool(pool, pks):
    """Directly seed pool with specific pks (avoids init_pool's broad filter)."""
    pool.clear()
    for pk in pks:
        pool.redis_client.sadd(pool.all_items_set_key, str(pk))
        pool.redis_client.lpush(pool.available_list_key, str(pk))


@th.django_unit_test()
def test_redis_model_pool_skip_predicate(opts):
    """Instance-level skip_predicate honored by get_next_instance."""
    from mojo.helpers.redis.pool import RedisModelPool
    from mojo.apps.account.models import Group

    skip_pks = {2}

    def predicate(instance):
        return instance.pk in skip_pks

    pool = RedisModelPool(
        Group,
        {'is_active': True},
        'test_skip_predicate_model',
        skip_predicate=predicate,
    )
    _seed_model_pool(pool, [1, 2, 3])

    seen_pks = []
    for _ in range(2):
        instance = pool.get_next_instance(timeout=1)
        assert instance is not None, "Expected an eligible instance when 2 of 3 are eligible"
        seen_pks.append(instance.pk)
        pool.return_instance(instance)

    assert 2 not in seen_pks, f"pk=2 was marked skip, should not have surfaced; got {seen_pks}"
    assert set(seen_pks) <= {1, 3}, f"Only eligible pks should surface, got {seen_pks}"

    pool.clear()


@th.django_unit_test()
def test_redis_model_pool_skip_predicate_blocks_all(opts):
    """When every instance is skipped, get_next_instance returns None within scard sweep."""
    from mojo.helpers.redis.pool import RedisModelPool
    from mojo.apps.account.models import Group

    pool = RedisModelPool(
        Group,
        {'is_active': True},
        'test_skip_predicate_model_all',
        skip_predicate=lambda i: True,
    )
    _seed_model_pool(pool, [1, 2, 3])

    start = time.time()
    instance = pool.get_next_instance(timeout=1)
    elapsed = time.time() - start

    assert instance is None, f"All-skipped pool should return None, got: {instance}"
    assert elapsed < 5.0, f"Bounded sweep should not block long, took {elapsed:.2f}s"

    available = pool.list_available()
    assert len(available) == 3, f"All instances should remain in pool, got: {available}"

    pool.clear()


@th.django_unit_test()
def test_redis_model_pool_skip_predicate_specific_bypasses(opts):
    """get_specific_instance and checkout_specific_instance ignore the predicate."""
    from mojo.helpers.redis.pool import RedisModelPool
    from mojo.apps.account.models import Group

    pool = RedisModelPool(
        Group,
        {'is_active': True},
        'test_skip_predicate_model_specific',
        skip_predicate=lambda i: True,
    )
    _seed_model_pool(pool, [1, 2, 3])

    target = Group.objects.get(pk=1)
    assert pool.get_specific_instance(target) is True, "get_specific_instance must bypass predicate"
    pool.return_instance(target)

    with pool.checkout_specific_instance(target) as inst:
        assert inst.pk == 1, f"checkout_specific_instance must bypass predicate; got {inst.pk}"

    pool.clear()


@th.django_unit_test()
def test_redis_model_pool_skip_predicate_default_none(opts):
    """No predicate configured = identical behaviour to today (regression guard)."""
    from mojo.helpers.redis.pool import RedisModelPool
    from mojo.apps.account.models import Group

    pool = RedisModelPool(
        Group,
        {'is_active': True},
        'test_skip_predicate_model_default',
    )
    _seed_model_pool(pool, [1, 2, 3])

    assert pool.instance_skip_predicate is None, "Default instance_skip_predicate should be None"
    assert pool.skip_predicate is None, "Base skip_predicate should remain None for model pool"

    seen = []
    for _ in range(3):
        instance = pool.get_next_instance(timeout=1)
        assert instance is not None, "Expected each pool member to surface"
        seen.append(instance.pk)
        pool.return_instance(instance)

    assert set(seen) == {1, 2, 3}, f"All pks should surface without predicate, got: {seen}"

    pool.clear()


@th.django_unit_test()
def test_retry_after_eligible_zero_returns_immediately(opts):
    """Predicate returning 0 → eligible, served immediately."""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_retry_after_zero', skip_predicate=lambda x: 0)
    pool.clear()
    pool.add('only')

    start = time.time()
    item = pool.get_next_available(timeout=2)
    elapsed = time.time() - start

    assert item == 'only', f"Expected 'only' served immediately, got: {item}"
    assert elapsed < 0.5, f"Predicate returning 0 should serve immediately, took {elapsed:.2f}s"

    pool.clear()


@th.django_unit_test()
def test_retry_after_served_after_wait(opts):
    """Predicate returns 0.5 once then False; pool waits and serves within budget."""
    from mojo.helpers.redis.pool import RedisBasePool

    state = {'calls': 0}

    def predicate(str_id):
        state['calls'] += 1
        return 0.5 if state['calls'] == 1 else False

    pool = RedisBasePool('test_retry_after_wait', skip_predicate=predicate)
    pool.clear()
    pool.add('only')

    start = time.time()
    item = pool.get_next_available(timeout=3)
    elapsed = time.time() - start

    assert item == 'only', f"Expected 'only' served after wait, got: {item}"
    assert 0.4 <= elapsed <= 1.5, f"Should wait ~0.5s and then serve, took {elapsed:.2f}s"

    pool.clear()


@th.django_unit_test()
def test_retry_after_respects_timeout(opts):
    """Predicate returns 5.0 always; timeout=1 returns None within ~1.2s."""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_retry_after_timeout', skip_predicate=lambda x: 5.0)
    pool.clear()
    pool.add('cooler')

    start = time.time()
    item = pool.get_next_available(timeout=1)
    elapsed = time.time() - start

    assert item is None, f"Should return None when retry-after exceeds timeout, got: {item}"
    assert 0.9 <= elapsed <= 1.5, f"Should honour timeout=1 wallclock, took {elapsed:.2f}s"

    available = pool.list_available()
    assert 'cooler' in available, f"Deferred item should be republished on None return, got: {available}"

    pool.clear()


@th.django_unit_test()
def test_retry_after_picks_soonest(opts):
    """Multi-member pool with mixed retry-after values; soonest is served first."""
    from mojo.helpers.redis.pool import RedisBasePool

    delays = {'fast': 0.3, 'slow': 2.0, 'slowest': 5.0}

    def predicate(str_id):
        return delays.get(str_id, True)

    pool = RedisBasePool('test_retry_after_soonest', skip_predicate=predicate)
    pool.clear()
    pool.add('slow')
    pool.add('fast')
    pool.add('slowest')

    # When the fast member matures, predicate keeps returning 0.3 — flip to eligible after first wait.
    flipped = {'done': False}
    original = predicate

    def predicate_with_flip(str_id):
        if str_id == 'fast' and flipped['done']:
            return False
        if str_id == 'fast':
            flipped['done'] = True
            return 0.3
        return delays.get(str_id, True)

    pool.skip_predicate = predicate_with_flip

    start = time.time()
    item = pool.get_next_available(timeout=3)
    elapsed = time.time() - start

    assert item == 'fast', f"Expected 'fast' (shortest retry-after) to be served, got: {item}"
    assert elapsed < 1.0, f"Should serve within ~0.4s, took {elapsed:.2f}s"

    pool.clear()


@th.django_unit_test()
def test_retry_after_mixed_bool_and_numeric(opts):
    """Mix of True / numeric / False — eligible served immediately, bool-skip never selected."""
    from mojo.helpers.redis.pool import RedisBasePool

    def predicate(str_id):
        if str_id == 'eligible':
            return False
        if str_id == 'cooling':
            return 0.5
        return True  # 'banned'

    pool = RedisBasePool('test_retry_after_mixed', skip_predicate=predicate)
    pool.clear()
    pool.add('banned')
    pool.add('cooling')
    pool.add('eligible')

    start = time.time()
    item = pool.get_next_available(timeout=3)
    elapsed = time.time() - start

    assert item == 'eligible', f"Expected 'eligible' served immediately, got: {item}"
    assert elapsed < 0.5, f"Eligible should be returned without waiting, took {elapsed:.2f}s"

    # banned must remain in pool, not held off
    available = pool.list_available()
    assert 'banned' in available, f"'banned' should be back in pool, got: {available}"
    assert 'cooling' in available, f"'cooling' (deferred) should be republished on exit, got: {available}"

    pool.clear()


@th.django_unit_test()
def test_retry_after_negative_treated_as_skip(opts):
    """Negative numeric returns are conservatively treated as bool-skip."""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_retry_after_negative', skip_predicate=lambda x: -1)
    pool.clear()
    pool.add('a')
    pool.add('b')

    start = time.time()
    item = pool.get_next_available(timeout=1)
    elapsed = time.time() - start

    assert item is None, f"Negative return should map to skip; got: {item}"
    assert elapsed < 1.0, f"Skip path should not wait the full budget, took {elapsed:.2f}s"

    available = pool.list_available()
    assert len(available) == 2, f"Both items should remain in pool after skip cycle, got: {available}"

    pool.clear()


@th.django_unit_test()
def test_retry_after_non_finite_treated_as_skip(opts):
    """float('inf') / float('nan') are conservatively mapped to skip, not held forever."""
    from mojo.helpers.redis.pool import RedisBasePool

    for bad in (float('inf'), float('nan')):
        pool = RedisBasePool('test_retry_after_nonfinite', skip_predicate=lambda x, b=bad: b)
        pool.clear()
        pool.add('a')
        pool.add('b')

        start = time.time()
        item = pool.get_next_available(timeout=1)
        elapsed = time.time() - start

        assert item is None, f"{bad!r} should map to skip → None, got: {item}"
        assert elapsed < 1.0, f"{bad!r} should bound the sweep, took {elapsed:.2f}s"

        available = pool.list_available()
        assert set(available) == {'a', 'b'}, \
            f"{bad!r}: items must remain available, got: {available}"

        pool.clear()


@th.django_unit_test()
def test_retry_after_predicate_raises_treated_as_skip(opts):
    """Exception from predicate is caught, logged, and treated as skip."""
    from mojo.helpers.redis.pool import RedisBasePool

    def boom(str_id):
        raise RuntimeError("predicate exploded")

    pool = RedisBasePool('test_retry_after_raises', skip_predicate=boom)
    pool.clear()
    pool.add('x')
    pool.add('y')

    item = pool.get_next_available(timeout=1)
    assert item is None, f"Raising predicate should drain to None, got: {item}"

    available = pool.list_available()
    assert len(available) == 2, f"Items should remain in pool after exceptions, got: {available}"

    pool.clear()


@th.django_unit_test()
def test_retry_after_deferred_republished_on_none(opts):
    """All items return retry-after > timeout — pool returns None and republishes deferred items."""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_retry_after_republish_none', skip_predicate=lambda x: 60.0)
    pool.clear()
    pool.add('p1')
    pool.add('p2')
    pool.add('p3')

    start = time.time()
    item = pool.get_next_available(timeout=1)
    elapsed = time.time() - start

    assert item is None, f"Should return None when nothing matures within timeout, got: {item}"
    assert 0.9 <= elapsed <= 2.0, f"Should honour timeout=1, took {elapsed:.2f}s"

    available = pool.list_available()
    assert set(available) == {'p1', 'p2', 'p3'}, \
        f"All deferred items must be republished on exit, got: {available}"

    pool.clear()


@th.django_unit_test()
def test_retry_after_deferred_republished_on_eligible(opts):
    """Mixed pool — when an eligible candidate is served, deferred items are republished."""
    from mojo.helpers.redis.pool import RedisBasePool

    def predicate(str_id):
        if str_id == 'served':
            return False
        return 30.0  # everyone else is cooling for ages

    pool = RedisBasePool('test_retry_after_republish_eligible', skip_predicate=predicate)
    pool.clear()
    pool.add('cooler1')
    pool.add('cooler2')
    pool.add('served')

    item = pool.get_next_available(timeout=2)
    assert item == 'served', f"Expected 'served' to be returned, got: {item}"

    available = pool.list_available()
    assert set(available) == {'cooler1', 'cooler2'}, \
        f"Deferred coolers should be back in the pool after eligible served, got: {available}"

    pool.clear()


@th.django_unit_test()
def test_retry_after_back_compat_bool_only_sweep(opts):
    """Pure bool predicate path — all True returns; behaviour identical to today."""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_retry_after_back_compat', skip_predicate=lambda x: True)
    pool.clear()
    pool.add('one')
    pool.add('two')
    pool.add('three')

    start = time.time()
    item = pool.get_next_available(timeout=10)
    elapsed = time.time() - start

    assert item is None, f"Bool-only sweep with all True should return None, got: {item}"
    assert elapsed < 3.0, \
        f"Bool-only carve-out: must NOT wait the full timeout, took {elapsed:.2f}s"

    available = pool.list_available()
    assert set(available) == {'one', 'two', 'three'}, \
        f"All items must be back in pool, got: {available}"

    pool.clear()


@th.django_unit_test()
def test_retry_after_no_predicate_byte_identical(opts):
    """skip_predicate=None — byte-identical to today's brpop-only fast path."""
    from mojo.helpers.redis.pool import RedisBasePool

    pool = RedisBasePool('test_retry_after_no_predicate')
    pool.clear()
    assert pool.skip_predicate is None, "Default skip_predicate should be None"

    pool.add('alpha')
    pool.add('beta')

    item1 = pool.get_next_available(timeout=1)
    item2 = pool.get_next_available(timeout=1)
    assert {item1, item2} == {'alpha', 'beta'}, \
        f"Both items should surface in the no-predicate fast path, got: {item1}, {item2}"

    # Empty pool — should block then return None
    start = time.time()
    item = pool.get_next_available(timeout=1)
    elapsed = time.time() - start
    assert item is None, f"Empty pool fast path should return None, got: {item}"
    assert elapsed >= 0.9, f"Empty pool should block on brpop for the timeout, took {elapsed:.2f}s"

    pool.clear()


@th.django_unit_test()
def test_retry_after_model_pool_served_after_wait(opts):
    """RedisModelPool: instance predicate returns numeric retry-after, served after wait."""
    from mojo.helpers.redis.pool import RedisModelPool
    from mojo.apps.account.models import Group

    state = {'calls': 0}

    def predicate(instance):
        state['calls'] += 1
        return 0.4 if state['calls'] == 1 else False

    pool = RedisModelPool(
        Group,
        {'is_active': True},
        'test_retry_after_model_wait',
        skip_predicate=predicate,
    )
    _seed_model_pool(pool, [1])

    start = time.time()
    instance = pool.get_next_instance(timeout=3)
    elapsed = time.time() - start

    assert instance is not None, f"Expected instance after retry-after wait, got: {instance}"
    assert instance.pk == 1, f"Expected pk=1, got pk={instance.pk}"
    assert 0.3 <= elapsed <= 1.5, f"Should wait ~0.4s and then serve, took {elapsed:.2f}s"

    pool.clear()


@th.django_unit_test()
def test_retry_after_model_pool_respects_timeout(opts):
    """RedisModelPool: predicate returns long retry-after; timeout caps the wait."""
    from mojo.helpers.redis.pool import RedisModelPool
    from mojo.apps.account.models import Group

    pool = RedisModelPool(
        Group,
        {'is_active': True},
        'test_retry_after_model_timeout',
        skip_predicate=lambda i: 10.0,
    )
    _seed_model_pool(pool, [1, 2])

    start = time.time()
    instance = pool.get_next_instance(timeout=1)
    elapsed = time.time() - start

    assert instance is None, f"Should return None when retry-after > timeout, got: {instance}"
    assert 0.9 <= elapsed <= 1.5, f"Should honour timeout=1, took {elapsed:.2f}s"

    available = pool.list_available()
    assert set(available) == {'1', '2'}, \
        f"Deferred instances must be republished on exit, got: {available}"

    pool.clear()


@th.django_unit_test()
def test_retry_after_model_pool_picks_soonest(opts):
    """RedisModelPool: multi-instance retry-after; soonest matures first."""
    from mojo.helpers.redis.pool import RedisModelPool
    from mojo.apps.account.models import Group

    flipped = {'done': False}

    def predicate(instance):
        if instance.pk == 2:
            if flipped['done']:
                return False
            flipped['done'] = True
            return 0.3
        return 5.0  # 1 and 3 are cooling for ages

    pool = RedisModelPool(
        Group,
        {'is_active': True},
        'test_retry_after_model_soonest',
        skip_predicate=predicate,
    )
    _seed_model_pool(pool, [1, 2, 3])

    start = time.time()
    instance = pool.get_next_instance(timeout=3)
    elapsed = time.time() - start

    assert instance is not None, f"Expected an instance, got: {instance}"
    assert instance.pk == 2, f"Expected pk=2 (soonest), got pk={instance.pk}"
    assert elapsed < 1.0, f"Should serve within ~0.4s, took {elapsed:.2f}s"

    pool.clear()


@th.django_unit_test()
def test_retry_after_model_pool_negative_treated_as_skip(opts):
    """RedisModelPool: negative numeric → skip."""
    from mojo.helpers.redis.pool import RedisModelPool
    from mojo.apps.account.models import Group

    pool = RedisModelPool(
        Group,
        {'is_active': True},
        'test_retry_after_model_negative',
        skip_predicate=lambda i: -3.0,
    )
    _seed_model_pool(pool, [1, 2])

    instance = pool.get_next_instance(timeout=1)
    assert instance is None, f"Negative numeric should map to skip → None, got: {instance}"

    available = pool.list_available()
    assert set(available) == {'1', '2'}, \
        f"Items should remain in pool after skip cycle, got: {available}"

    pool.clear()


@th.django_unit_test()
def test_retry_after_model_pool_back_compat_no_predicate(opts):
    """RedisModelPool with no predicate — fast path unchanged."""
    from mojo.helpers.redis.pool import RedisModelPool
    from mojo.apps.account.models import Group

    pool = RedisModelPool(
        Group,
        {'is_active': True},
        'test_retry_after_model_no_predicate',
    )
    _seed_model_pool(pool, [1, 2])

    instance1 = pool.get_next_instance(timeout=1)
    instance2 = pool.get_next_instance(timeout=1)
    pks = {instance1.pk, instance2.pk}
    assert pks == {1, 2}, f"Both pks should surface in fast path, got: {pks}"

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
