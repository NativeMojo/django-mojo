"""
Simplified tests for Redis operations.
Focus on core Redis functionality without decorator complexity.
"""
from testit import helpers as th
import time
import json
import uuid
from datetime import datetime, timedelta


@th.django_unit_setup()
def setup_redis_tests(opts):
    """Setup Redis test environment."""
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Get Redis adapter and keys
    opts.redis = get_adapter()
    opts.keys = JobKeys()

    # Test configuration
    opts.test_prefix = 'test_redis_'
    opts.test_channel = 'redis_test_channel'

    # Clean up any existing test keys
    test_patterns = [
        f"{opts.keys.prefix}:{opts.test_prefix}*",
        opts.keys.stream(opts.test_channel),
        opts.keys.sched(opts.test_channel),
    ]

    for pattern in test_patterns:
        try:
            # Clean up test keys
            for key in opts.redis.get_client().scan_iter(match=pattern):
                opts.redis.delete(key)
        except:
            pass


@th.django_unit_test()
def test_redis_connection(opts):
    """Test basic Redis connectivity."""
    # Test ping
    assert opts.redis.ping() is True, f"Redis ping failed - connection may be down. Redis adapter: {type(opts.redis)}"

    # Test basic set/get
    test_key = f"{opts.test_prefix}connection_test"
    opts.redis.set(test_key, "test_value", ex=60)
    value = opts.redis.get(test_key)
    assert value == "test_value", f"Redis get/set failed: expected 'test_value', got '{value}'. Test key: {test_key}"

    # Clean up
    opts.redis.delete(test_key)


@th.django_unit_test()
def test_key_generation(opts):
    """Test key generation patterns."""
    # Test stream keys
    stream_key = opts.keys.stream('test_channel')
    assert 'stream:test_channel' in stream_key, f"Stream key pattern incorrect: expected 'stream:test_channel' in '{stream_key}'"

    broadcast_key = opts.keys.stream_broadcast('test_channel')
    assert 'stream:test_channel:broadcast' in broadcast_key, f"Broadcast key pattern incorrect: expected 'stream:test_channel:broadcast' in '{broadcast_key}'"

    # Test scheduled jobs key
    sched_key = opts.keys.sched('test_channel')
    assert 'sched:test_channel' in sched_key, f"Scheduled jobs key pattern incorrect: expected 'sched:test_channel' in '{sched_key}'"

    # Test job metadata key
    job_id = uuid.uuid4().hex
    job_key = opts.keys.job(job_id)
    assert f'job:{job_id}' in job_key, f"Job metadata key pattern incorrect: expected 'job:{job_id}' in '{job_key}'"

    # Test runner keys
    runner_id = 'test_runner_001'
    heartbeat_key = opts.keys.runner_hb(runner_id)
    assert f'runner:{runner_id}:hb' in heartbeat_key, f"Runner heartbeat key pattern incorrect: expected 'runner:{runner_id}:hb' in '{heartbeat_key}'"

    control_key = opts.keys.runner_ctl(runner_id)
    assert f'runner:{runner_id}:ctl' in control_key, f"Runner control key pattern incorrect: expected 'runner:{runner_id}:ctl' in '{control_key}'"


@th.django_unit_test()
def test_stream_add_and_read(opts):
    """Test adding to and reading from Redis streams."""
    stream_key = opts.keys.stream(opts.test_channel)

    # Add entries to stream
    job_ids = []
    for i in range(3):
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        entry_id = opts.redis.xadd(stream_key, {
            'job_id': job_id,
            'func': f'test.function_{i}',
            'created': datetime.now().isoformat()
        })
        job_ids.append(job_id)
        assert entry_id is not None, f"Failed to add entry to stream {stream_key}. Job ID: {job_id}, func: test.function_{i}"

    # Read from stream
    client = opts.redis.get_client()
    messages = client.xrange(stream_key, count=10)

    assert len(messages) >= 3, f"Expected at least 3 messages from stream, got {len(messages)}. Stream: {stream_key}, job_ids added: {job_ids}"

    # Verify message content
    found_jobs = []
    for msg_id, data in messages:
        job_id = data.get(b'job_id')
        if job_id:
            found_jobs.append(job_id.decode('utf-8'))

    for job_id in job_ids:
        assert job_id in found_jobs, f"Job ID {job_id} not found in stream messages. Found jobs: {found_jobs}, expected: {job_ids}"

    # Clean up
    opts.redis.delete(stream_key)


@th.django_unit_test()
def test_consumer_group(opts):
    """Test consumer group operations."""
    stream_key = opts.keys.stream(opts.test_channel)
    group_name = 'test_consumer_group'
    consumer_name = 'test_consumer'

    # Create consumer group
    created = opts.redis.xgroup_create(stream_key, group_name)
    assert created is True, f"Failed to create consumer group '{group_name}' for stream {stream_key}. Create result: {created}"

    # Add messages to stream
    for i in range(3):
        opts.redis.xadd(stream_key, {
            'job_id': f'job_{i}',
            'data': f'test_data_{i}'
        })

    # Read from consumer group
    messages = opts.redis.xreadgroup(
        group=group_name,
        consumer=consumer_name,
        streams={stream_key: '>'},
        count=2,
        block=100
    )

    assert len(messages) > 0, f"No messages received from consumer group. Group: {group_name}, consumer: {consumer_name}, stream: {stream_key}"
    stream_name, stream_messages = messages[0]
    assert len(stream_messages) == 2, f"Expected 2 messages from consumer group, got {len(stream_messages)}. Group: {group_name}, consumer: {consumer_name}"

    # Acknowledge messages
    for msg_id, data in stream_messages:
        ack_count = opts.redis.xack(stream_key, group_name, msg_id)
        assert ack_count == 1, f"Failed to acknowledge message {msg_id}. Ack count: {ack_count}, group: {group_name}"

    # Check pending (should be 1 unread message)
    pending_info = opts.redis.xpending(stream_key, group_name)
    assert pending_info is not None, f"Failed to get pending messages info. Group: {group_name}, stream: {stream_key}"

    # Clean up
    opts.redis.delete(stream_key)


@th.django_unit_test()
def test_scheduled_jobs_zset(opts):
    """Test sorted set operations for scheduled jobs."""
    sched_key = opts.keys.sched(opts.test_channel)

    # Add scheduled jobs with timestamps as scores
    now = time.time() * 1000
    future_times = {
        'job_immediate': now - 1000,  # Past (ready to run)
        'job_soon': now + 5000,       # 5 seconds future
        'job_later': now + 60000,     # 1 minute future
        'job_much_later': now + 3600000  # 1 hour future
    }

    # Add all jobs
    added = opts.redis.zadd(sched_key, future_times)
    assert added >= 4, f"Failed to add all scheduled jobs. Expected >=4, got {added}. ZSET key: {sched_key}, jobs: {list(future_times.keys())}"

    # Check total count
    count = opts.redis.zcard(sched_key)
    assert count == 4, f"Expected 4 jobs in scheduled ZSET, got {count}. ZSET key: {sched_key}"

    # Pop the earliest (should be job_immediate)
    results = opts.redis.zpopmin(sched_key, count=1)
    assert len(results) == 1, f"Expected 1 result from zpopmin, got {len(results)}. Results: {results}, ZSET: {sched_key}"

    job_id, score = results[0]
    assert job_id == 'job_immediate', f"Expected 'job_immediate' to be earliest job, got '{job_id}'. Results: {results}"
    assert score < now, f"Expected job score {score} to be < now {now} (was in past). Job: {job_id}"

    # Check remaining count
    count = opts.redis.zcard(sched_key)
    assert count == 3, f"Expected 3 jobs remaining after zpopmin, got {count}. ZSET key: {sched_key}"

    # Get all remaining without removing
    client = opts.redis.get_client()
    remaining = client.zrange(sched_key, 0, -1, withscores=True)
    assert len(remaining) == 3, f"Expected 3 remaining jobs from zrange, got {len(remaining)}. Remaining: {remaining}, ZSET: {sched_key}"

    # Clean up
    opts.redis.delete(sched_key)


@th.django_unit_test()
def test_job_metadata_hash(opts):
    """Test hash operations for job metadata."""
    job_id = uuid.uuid4().hex
    job_key = opts.keys.job(job_id)

    # Set job metadata
    job_data = {
        'status': 'pending',
        'channel': opts.test_channel,
        'func': 'test.sample_function',
        'created_at': datetime.now().isoformat(),
        'attempt': 0,
        'max_retries': 3,
        'cancel_requested': False,  # Will be converted to '0'
        'payload': json.dumps({'key': 'value', 'count': 42})
    }

    fields_set = opts.redis.hset(job_key, job_data)
    assert fields_set >= 0, f"Failed to set job metadata hash. Fields set: {fields_set}, job_key: {job_key}, data: {list(job_data.keys())}"

    # Get individual field
    status = opts.redis.hget(job_key, 'status')
    assert status == 'pending', f"Expected status='pending', got '{status}'. Job key: {job_key}"

    # Get all fields
    all_data = opts.redis.hgetall(job_key)
    assert all_data['status'] == 'pending', f"Expected status='pending' in hash, got '{all_data.get('status')}'. Job: {job_id}, all_data keys: {list(all_data.keys())}"
    assert all_data['channel'] == opts.test_channel, f"Expected channel='{opts.test_channel}', got '{all_data.get('channel')}'. Job: {job_id}"
    assert all_data['attempt'] == '0', f"Expected attempt='0', got '{all_data.get('attempt')}'. Job: {job_id}"
    assert all_data['cancel_requested'] == '0', f"Expected cancel_requested='0' (bool converted), got '{all_data.get('cancel_requested')}'. Job: {job_id}"

    # Parse JSON payload
    payload = json.loads(all_data['payload'])
    assert payload['key'] == 'value', f"Expected payload key='value', got '{payload.get('key')}'. Full payload: {payload}"
    assert payload['count'] == 42, f"Expected payload count=42, got {payload.get('count')}. Full payload: {payload}"

    # Update fields
    opts.redis.hset(job_key, {'status': 'running', 'attempt': '1'})

    updated_status = opts.redis.hget(job_key, 'status')
    assert updated_status == 'running', f"Expected updated status='running', got '{updated_status}'. Job: {job_id}"

    updated_attempt = opts.redis.hget(job_key, 'attempt')
    assert updated_attempt == '1', f"Expected updated attempt='1', got '{updated_attempt}'. Job: {job_id}"

    # Clean up
    opts.redis.delete(job_key)


@th.django_unit_test()
def test_expiration_and_ttl(opts):
    """Test key expiration and TTL operations."""
    test_key = f"{opts.test_prefix}expiring_key"

    # Set key with expiration
    opts.redis.set(test_key, "expires_soon", ex=5)

    # Check TTL
    ttl = opts.redis.ttl(test_key)
    assert ttl > 0, f"Expected TTL > 0 for expiring key, got {ttl}. Key: {test_key}"
    assert ttl <= 5, f"Expected TTL <= 5 seconds, got {ttl}. Key: {test_key}"

    # Set key without expiration
    test_key2 = f"{opts.test_prefix}persistent_key"
    opts.redis.set(test_key2, "no_expiry")

    ttl2 = opts.redis.ttl(test_key2)
    assert ttl2 == -1, f"Expected TTL = -1 for persistent key, got {ttl2}. Key: {test_key2}"

    # Add expiration to existing key
    opts.redis.expire(test_key2, 10)

    ttl2_updated = opts.redis.ttl(test_key2)
    assert ttl2_updated > 0, f"Expected TTL > 0 after setting expiration, got {ttl2_updated}. Key: {test_key2}"
    assert ttl2_updated <= 10, f"Expected TTL <= 10 after expire command, got {ttl2_updated}. Key: {test_key2}"

    # Clean up
    opts.redis.delete(test_key, test_key2)


@th.django_unit_test()
def test_pipeline_operations(opts):
    """Test Redis pipeline for batch operations."""
    test_keys = [f"{opts.test_prefix}pipe_{i}" for i in range(5)]

    # Use pipeline to set multiple keys atomically
    with opts.redis.pipeline() as pipe:
        for i, key in enumerate(test_keys):
            pipe.set(key, f"value_{i}", ex=60)
            # Manually serialize data for pipeline (raw Redis client doesn't auto-serialize)
            pipe.hset(f"{key}_hash", mapping={'field1': f'data_{i}', 'field2': str(i)})

    # Verify all keys were set
    for i, key in enumerate(test_keys):
        value = opts.redis.get(key)
        assert value == f"value_{i}", f"Pipeline set failed for key {key}: expected 'value_{i}', got '{value}'"

        hash_data = opts.redis.hgetall(f"{key}_hash")
        assert hash_data['field1'] == f'data_{i}', f"Pipeline hash set failed for {key}_hash field1: expected 'data_{i}', got '{hash_data.get('field1')}'"
        assert hash_data['field2'] == str(i), f"Pipeline hash set failed for {key}_hash field2: expected '{i}', got '{hash_data.get('field2')}'"

    # Clean up with pipeline
    with opts.redis.pipeline() as pipe:
        for key in test_keys:
            pipe.delete(key)
            pipe.delete(f"{key}_hash")


@th.django_unit_test()
def test_pubsub_messages(opts):
    """Test pub/sub for control messages."""
    import threading

    channel = opts.keys.runner_ctl('test_runner')
    received_messages = []

    def subscriber_thread():
        """Subscribe and receive messages."""
        pubsub = opts.redis.pubsub()
        pubsub.subscribe(channel)

        # Wait for subscription confirmation
        for _ in range(10):
            msg = pubsub.get_message(timeout=0.1)
            if msg and msg['type'] == 'subscribe':
                break

        # Wait for actual message
        for _ in range(10):
            msg = pubsub.get_message(timeout=0.1)
            if msg and msg['type'] == 'message':
                data = json.loads(msg['data'])
                received_messages.append(data)
                break

        pubsub.close()

    # Start subscriber
    thread = threading.Thread(target=subscriber_thread)
    thread.daemon = True
    thread.start()

    # Give subscriber time to connect
    time.sleep(0.2)

    # Publish message
    message = {'command': 'ping', 'timestamp': time.time()}
    subscribers = opts.redis.publish(channel, json.dumps(message))
    assert subscribers >= 0, f"Failed to publish message to channel {channel}. Subscribers: {subscribers}, message: {message}"

    # Wait for subscriber to receive
    thread.join(timeout=2.0)

    # Verify message was received
    assert len(received_messages) == 1, f"Expected 1 received message, got {len(received_messages)}. Messages: {received_messages}"
    assert received_messages[0]['command'] == 'ping', f"Expected command='ping', got '{received_messages[0].get('command') if received_messages else 'no messages'}'. Full message: {received_messages[0] if received_messages else None}"


@th.django_unit_test()
def test_redis_data_types(opts):
    """Test handling of different data types."""
    test_key = f"{opts.test_prefix}datatypes"

    # Test JSON serialization
    complex_data = {
        'string': 'hello',
        'number': 42,
        'float': 3.14,
        'boolean': True,
        'null': None,
        'list': [1, 2, 3],
        'nested': {'a': 1, 'b': 2}
    }

    # Store as JSON
    opts.redis.set(test_key, json.dumps(complex_data), ex=60)

    # Retrieve and parse
    raw_data = opts.redis.get(test_key)
    parsed_data = json.loads(raw_data)

    assert parsed_data['string'] == 'hello', f"Expected string='hello', got '{parsed_data.get('string')}'. Full data: {parsed_data}"
    assert parsed_data['number'] == 42, f"Expected number=42, got {parsed_data.get('number')}. Full data: {parsed_data}"
    assert parsed_data['float'] == 3.14, f"Expected float=3.14, got {parsed_data.get('float')}. Full data: {parsed_data}"
    assert parsed_data['boolean'] is True, f"Expected boolean=True, got {parsed_data.get('boolean')}. Full data: {parsed_data}"
    assert parsed_data['null'] is None, f"Expected null=None, got {parsed_data.get('null')}. Full data: {parsed_data}"
    assert parsed_data['list'] == [1, 2, 3], f"Expected list=[1, 2, 3], got {parsed_data.get('list')}. Full data: {parsed_data}"
    assert parsed_data['nested']['a'] == 1, f"Expected nested.a=1, got {parsed_data.get('nested', {}).get('a')}. Nested: {parsed_data.get('nested')}"

    # Clean up
    opts.redis.delete(test_key)


@th.django_unit_test()
def test_stream_trimming(opts):
    """Test stream maxlen trimming."""
    stream_key = f"{opts.test_prefix}trimmed_stream"

    # Clean up any existing stream first
    opts.redis.delete(stream_key)
    print(f"Cleaned up stream: {stream_key}")

    # Add entries first to create a full stream
    for i in range(20):
        opts.redis.xadd(stream_key, {
            'index': str(i),
            'data': f'entry_{i}'
        })
        if i == 0 or i == 19:
            info = opts.redis.xinfo_stream(stream_key)
            print(f"After adding entry {i}: stream has {info['length']} entries")

    # Verify we have 20 entries initially
    client = opts.redis.get_client()
    initial_info = client.xinfo_stream(stream_key)
    print(f"Final count after adding 20 entries: {initial_info['length']}")
    assert initial_info['length'] == 20, f"Expected 20 initial entries, got {initial_info['length']}. Stream key: {stream_key}"

    # Now trim the stream manually for more predictable behavior
    # Use exact trimming (approximate=False) to ensure it actually trims
    trimmed_count = client.xtrim(stream_key, maxlen=10, approximate=False)
    print(f"XTRIM removed {trimmed_count} entries (exact trim to 10)")

    # Check stream length after trimming
    info = client.xinfo_stream(stream_key)
    print(f"After trimming: stream has {info['length']} entries")

    # Should have exactly 10 entries (using exact trimming)
    assert info['length'] == 10, f"Stream length should be exactly 10 after exact trimming, got {info['length']}. Stream: {stream_key}, removed: {trimmed_count}"

    # Clean up
    opts.redis.delete(stream_key)


@th.django_unit_test()
def test_cleanup_redis_tests(opts):
    """Clean up any remaining test keys."""
    # Pattern for test keys
    pattern = f"{opts.keys.prefix}:{opts.test_prefix}*"

    # Scan and delete
    deleted_count = 0
    for key in opts.redis.get_client().scan_iter(match=pattern):
        opts.redis.delete(key)
        deleted_count += 1

    # Also clean up test channels
    opts.redis.delete(opts.keys.stream(opts.test_channel))
    opts.redis.delete(opts.keys.sched(opts.test_channel))

    print(f"Cleaned up {deleted_count} test Redis keys")
