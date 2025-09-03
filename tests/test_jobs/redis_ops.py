from testit import helpers as th
import time
import json
import uuid
from datetime import datetime, timedelta


@th.django_unit_setup()
def setup_redis_test_data(opts):
    """Setup Redis test environment."""
    from mojo.apps.jobs.adapters import get_adapter, reset_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Reset adapter to ensure clean state
    reset_adapter()

    # Get fresh adapter and keys
    opts.redis = get_adapter()
    opts.keys = JobKeys()

    # Test data
    opts.test_channel = 'redis_test'
    opts.test_job_id = uuid.uuid4().hex
    opts.test_stream_key = opts.keys.stream(opts.test_channel)
    opts.test_sched_key = opts.keys.sched(opts.test_channel)

    # Clean up any existing test data
    try:
        opts.redis.delete(opts.test_stream_key)
        opts.redis.delete(opts.test_sched_key)
        opts.redis.delete(opts.keys.job(opts.test_job_id))
    except:
        pass


@th.django_unit_test()
def test_redis_adapter_connection(opts):
    """Test Redis adapter connection and ping."""
    assert opts.redis.ping() is True, "Redis connection should be established"

    # Test client retrieval
    client = opts.redis.get_client()
    assert client is not None, "Should get Redis client"

    # Direct ping through client
    assert client.ping() is True, "Client ping should work"


@th.django_unit_test()
def test_key_builder_prefixes(opts):
    """Test JobKeys generates correct key patterns."""
    from django.conf import settings

    # Test default prefix
    expected_prefix = getattr(settings, 'JOBS_REDIS_PREFIX', 'mojo:jobs')

    # Stream keys
    assert opts.keys.stream('test') == f"{expected_prefix}:stream:test"
    assert opts.keys.stream_broadcast('test') == f"{expected_prefix}:stream:test:broadcast"

    # Consumer group keys
    assert opts.keys.group_workers('test') == f"{expected_prefix}:cg:test:workers"
    assert opts.keys.group_runner('test', 'runner1') == f"{expected_prefix}:cg:test:runner:runner1"

    # Scheduled jobs key
    assert opts.keys.sched('test') == f"{expected_prefix}:sched:test"

    # Job metadata key
    assert opts.keys.job('abc123') == f"{expected_prefix}:job:abc123"

    # Runner keys
    assert opts.keys.runner_ctl('runner1') == f"{expected_prefix}:runner:runner1:ctl"
    assert opts.keys.runner_hb('runner1') == f"{expected_prefix}:runner:runner1:hb"

    # Scheduler lock
    assert opts.keys.scheduler_lock() == f"{expected_prefix}:lock:scheduler"


@th.django_unit_test()
def test_custom_prefix(opts):
    """Test JobKeys with custom prefix."""
    from mojo.apps.jobs.keys import JobKeys

    custom_keys = JobKeys(prefix="custom:prefix")

    assert custom_keys.stream('test') == "custom:prefix:stream:test"
    assert custom_keys.job('123') == "custom:prefix:job:123"
    assert custom_keys.scheduler_lock() == "custom:prefix:lock:scheduler"


@th.django_unit_test()
def test_stream_operations(opts):
    """Test Redis Stream operations."""
    stream_key = opts.test_stream_key

    # Test XADD
    entry_id = opts.redis.xadd(stream_key, {
        'job_id': opts.test_job_id,
        'func': 'test_func',
        'created': datetime.now().isoformat()
    }, maxlen=1000)

    assert entry_id is not None
    assert '-' in entry_id  # Stream IDs have format timestamp-sequence

    # Test XADD with dict payload (should be JSON serialized)
    complex_data = {
        'nested': {'key': 'value'},
        'list': [1, 2, 3]
    }
    entry_id2 = opts.redis.xadd(stream_key, {
        'job_id': 'test2',
        'data': complex_data
    })
    assert entry_id2 is not None

    # Test stream info
    info = opts.redis.xinfo_stream(stream_key)
    assert info['length'] >= 2
    assert 'first-entry' in info
    assert 'last-entry' in info


@th.django_unit_test()
def test_consumer_group_operations(opts):
    """Test consumer group creation and reading."""
    stream_key = opts.test_stream_key
    group_name = 'test_group'
    consumer_name = 'test_consumer'

    # Create consumer group
    created = opts.redis.xgroup_create(stream_key, group_name)
    assert created is True

    # Try to create again (should return False, not error)
    created_again = opts.redis.xgroup_create(stream_key, group_name)
    assert created_again is False

    # Add a message
    opts.redis.xadd(stream_key, {'test': 'message'})

    # Read from group
    messages = opts.redis.xreadgroup(
        group=group_name,
        consumer=consumer_name,
        streams={stream_key: '>'},
        count=10,
        block=100  # 100ms timeout
    )

    # Should have messages
    assert len(messages) > 0
    stream_name, stream_messages = messages[0]
    assert len(stream_messages) > 0

    msg_id, msg_data = stream_messages[0]

    # ACK the message
    ack_count = opts.redis.xack(stream_key, group_name, msg_id)
    assert ack_count == 1

    # Check pending
    pending_info = opts.redis.xpending(stream_key, group_name)
    # After ACK, pending should be 0 or reduced
    assert isinstance(pending_info, dict)


@th.django_unit_test()
def test_zset_operations(opts):
    """Test sorted set operations for scheduling."""
    zset_key = opts.test_sched_key

    # Add scheduled jobs
    now_ms = time.time() * 1000
    future_ms = (time.time() + 60) * 1000

    # Add multiple items
    mapping = {
        'job1': now_ms,
        'job2': now_ms + 1000,
        'job3': future_ms
    }
    added = opts.redis.zadd(zset_key, mapping)
    assert added >= 3  # At least 3 added (might be less if they existed)

    # Check cardinality
    count = opts.redis.zcard(zset_key)
    assert count >= 3

    # Pop minimum (should be job1)
    results = opts.redis.zpopmin(zset_key, count=1)
    assert len(results) == 1
    member, score = results[0]
    assert member == 'job1'
    assert abs(score - now_ms) < 1  # Close to expected score

    # Pop multiple
    results = opts.redis.zpopmin(zset_key, count=2)
    assert len(results) == 2

    # Cardinality should be 0 now
    count = opts.redis.zcard(zset_key)
    assert count == 0


@th.django_unit_test()
def test_hash_operations(opts):
    """Test hash operations for job metadata."""
    hash_key = opts.keys.job(opts.test_job_id)

    # Set multiple fields
    job_data = {
        'status': 'pending',
        'channel': 'test',
        'func': 'test_func',
        'payload': {'key': 'value'},  # Dict should be JSON serialized
        'created_at': datetime.now().isoformat(),
        'attempt': 0,
        'max_retries': 3,
        'broadcast': True,  # Bool should be converted to '1'
        'max_exec_seconds': None  # None should be converted to ''
    }

    fields_set = opts.redis.hset(hash_key, job_data)
    assert fields_set >= 0  # Returns number of new fields

    # Get single field
    status = opts.redis.hget(hash_key, 'status')
    assert status == 'pending'

    # Get all fields
    all_data = opts.redis.hgetall(hash_key)
    assert all_data['status'] == 'pending'
    assert all_data['channel'] == 'test'
    assert all_data['broadcast'] == '1'  # Bool converted to string
    assert all_data['max_exec_seconds'] == ''  # None converted to empty string

    # Parse JSON payload
    payload = json.loads(all_data['payload'])
    assert payload['key'] == 'value'

    # Delete fields
    deleted = opts.redis.hdel(hash_key, 'attempt', 'max_retries')
    assert deleted == 2

    # Verify deletion
    attempt = opts.redis.hget(hash_key, 'attempt')
    assert attempt is None


@th.django_unit_test()
def test_key_value_operations(opts):
    """Test basic key-value operations."""
    test_key = f"{opts.keys.prefix}:test:key"

    # Set with expiration
    result = opts.redis.set(test_key, "test_value", ex=60)
    assert result is True

    # Get value
    value = opts.redis.get(test_key)
    assert value == "test_value"

    # Set with NX (only if not exists)
    result = opts.redis.set(test_key, "new_value", nx=True)
    assert result is False  # Should fail since key exists

    # Set with XX (only if exists)
    result = opts.redis.set(test_key, "updated_value", xx=True)
    assert result is True

    value = opts.redis.get(test_key)
    assert value == "updated_value"

    # Check TTL
    ttl = opts.redis.ttl(test_key)
    assert ttl > 0  # Has expiration
    assert ttl <= 60  # Within our set expiration

    # Check existence
    exists = opts.redis.exists(test_key)
    assert exists == 1

    # Delete
    deleted = opts.redis.delete(test_key)
    assert deleted == 1

    # Check non-existence
    exists = opts.redis.exists(test_key)
    assert exists == 0

    value = opts.redis.get(test_key)
    assert value is None


@th.django_unit_test()
def test_json_serialization(opts):
    """Test JSON serialization in operations."""
    test_key = f"{opts.keys.prefix}:test:json"

    # Set dict value (should be JSON serialized)
    data = {
        'name': 'test',
        'values': [1, 2, 3],
        'nested': {'key': 'value'}
    }

    result = opts.redis.set(test_key, data, ex=30)
    assert result is True

    # Get and parse
    raw_value = opts.redis.get(test_key)
    assert raw_value is not None

    parsed = json.loads(raw_value)
    assert parsed['name'] == 'test'
    assert parsed['values'] == [1, 2, 3]
    assert parsed['nested']['key'] == 'value'

    opts.redis.delete(test_key)


@th.django_unit_test()
def test_pipeline_operations(opts):
    """Test pipeline for atomic operations."""
    test_keys = [f"{opts.keys.prefix}:test:pipe:{i}" for i in range(5)]

    # Use pipeline to set multiple keys
    with opts.redis.pipeline() as pipe:
        for i, key in enumerate(test_keys):
            pipe.set(key, f"value_{i}")
        # Pipeline executes on context exit

    # Verify all keys were set
    for i, key in enumerate(test_keys):
        value = opts.redis.get(key)
        assert value == f"value_{i}"

    # Clean up
    opts.redis.delete(*test_keys)


@th.django_unit_test()
def test_pubsub_operations(opts):
    """Test pub/sub functionality."""
    channel = opts.keys.runner_ctl('test_runner')

    # Create pubsub connection
    pubsub = opts.redis.pubsub()

    # Subscribe to channel
    pubsub.subscribe(channel)

    # Publish message
    message_data = {'command': 'ping', 'timestamp': time.time()}
    subscribers = opts.redis.publish(channel, message_data)
    assert subscribers >= 0  # Number of subscribers that received message

    # Read message (with timeout to avoid hanging)
    message = pubsub.get_message(timeout=1.0)
    # First message is subscription confirmation
    while message and message['type'] != 'message':
        message = pubsub.get_message(timeout=1.0)

    if message:
        # Parse message data
        data = json.loads(message['data'])
        assert data['command'] == 'ping'

    # Cleanup
    pubsub.close()


@th.django_unit_test()
def test_expire_operations(opts):
    """Test key expiration operations."""
    test_key = f"{opts.keys.prefix}:test:expire"

    # Set key without expiration
    opts.redis.set(test_key, "test")

    # Check TTL (should be -1 for no expiration)
    ttl = opts.redis.ttl(test_key)
    assert ttl == -1

    # Set expiration
    result = opts.redis.expire(test_key, 30)
    assert result is True

    # Check TTL
    ttl = opts.redis.ttl(test_key)
    assert ttl > 0
    assert ttl <= 30

    # Set expiration in milliseconds
    result = opts.redis.pexpire(test_key, 5000)
    assert result is True

    ttl = opts.redis.ttl(test_key)
    assert ttl > 0
    assert ttl <= 5

    # Clean up
    opts.redis.delete(test_key)


@th.django_unit_test()
def test_connection_recovery(opts):
    """Test that adapter handles connection issues gracefully."""
    from mojo.apps.jobs.adapters import RedisAdapter

    # Create adapter with short timeout
    adapter = RedisAdapter(
        connect_timeout=1,
        socket_timeout=1,
        max_retries=2
    )

    # Should be able to ping
    assert adapter.ping() is True

    # Test with invalid operation (adapter should handle gracefully)
    try:
        # Try to get info on non-existent stream
        info = adapter.xinfo_stream('nonexistent:stream:key')
    except Exception as e:
        # Should get an error but not crash
        assert 'no such key' in str(e).lower() or 'not found' in str(e).lower()

    # Adapter should still be functional
    assert adapter.ping() is True

    # Clean up
    adapter.close()
