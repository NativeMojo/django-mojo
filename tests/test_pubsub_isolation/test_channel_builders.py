"""Channel-builder contract: byte-identical legacy names when the isolation
prefix is empty, consistent namespacing when one is injected. These are the
empty-prefix production-compatibility controls — no Redis involved."""
from testit import helpers as th


@th.django_unit_test()
def test_empty_prefix_names_are_legacy_literals(opts):
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.realtime import channels as rt

    keys = JobKeys(prefix="mojo:jobs", pubsub_prefix="")
    assert keys.runners_broadcast() == "mojo:jobs:runners:broadcast", \
        f"broadcast channel moved with empty prefix: {keys.runners_broadcast()}"
    assert keys.reply_channel("abc123") == "mojo:jobs:replies:abc123", \
        f"reply channel moved with empty prefix: {keys.reply_channel('abc123')}"
    assert keys.ping_reply("abc123") == "mojo:jobs:ping:abc123", \
        f"ping channel moved with empty prefix: {keys.ping_reply('abc123')}"
    assert keys.runner_ctl("r1") == "mojo:jobs:runner:r1:ctl", \
        f"runner ctl channel moved with empty prefix: {keys.runner_ctl('r1')}"

    assert rt.broadcast_channel(prefix="") == "realtime:broadcast", \
        f"realtime broadcast moved with empty prefix: {rt.broadcast_channel(prefix='')}"
    assert rt.topic_channel("orders", prefix="") == "realtime:topic:orders", \
        f"realtime topic moved with empty prefix: {rt.topic_channel('orders', prefix='')}"
    assert rt.messages_channel("c1", prefix="") == "realtime:messages:c1", \
        f"realtime messages moved with empty prefix: {rt.messages_channel('c1', prefix='')}"


@th.django_unit_test()
def test_custom_jobs_prefix_retained_with_empty_isolation(opts):
    from mojo.apps.jobs.keys import JobKeys

    keys = JobKeys(prefix="custom", pubsub_prefix="")
    # Storage keys keep honoring the custom prefix exactly as before.
    assert keys.queue("ch") == "custom:{jobs}:queue:ch", \
        f"custom-prefix queue key changed: {keys.queue('ch')}"
    assert keys.runner_hb("r1") == "custom:runner:r1:hb", \
        f"custom-prefix heartbeat key changed: {keys.runner_hb('r1')}"
    # runner_ctl honored the custom prefix on both sides before; still does.
    assert keys.runner_ctl("r1") == "custom:runner:r1:ctl", \
        f"custom-prefix runner ctl changed: {keys.runner_ctl('r1')}"
    # Broadcast/replies/ping were literal-rooted before this feature (they
    # ignored a custom JOBS_REDIS_PREFIX); that wire behavior is preserved.
    assert keys.runners_broadcast() == "mojo:jobs:runners:broadcast", \
        f"broadcast rendezvous moved for custom prefix: {keys.runners_broadcast()}"
    assert keys.reply_channel("t") == "mojo:jobs:replies:t", \
        f"reply root moved for custom prefix: {keys.reply_channel('t')}"


@th.django_unit_test()
def test_injected_prefixes_namespace_all_channels(opts):
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.realtime import channels as rt

    a = JobKeys(prefix="mojo:jobs", pubsub_prefix="iso_a")
    assert a.runners_broadcast() == "iso_a:mojo:jobs:runners:broadcast", \
        f"prefixed broadcast wrong: {a.runners_broadcast()}"
    assert a.runner_ctl("r1") == "iso_a:mojo:jobs:runner:r1:ctl", \
        f"prefixed runner ctl wrong: {a.runner_ctl('r1')}"
    assert a.reply_channel("t") == "iso_a:mojo:jobs:replies:t", \
        f"prefixed reply wrong: {a.reply_channel('t')}"
    assert a.ping_reply("t") == "iso_a:mojo:jobs:ping:t", \
        f"prefixed ping wrong: {a.ping_reply('t')}"

    assert rt.broadcast_channel(prefix="iso_a") == "iso_a:realtime:broadcast", \
        f"prefixed realtime broadcast wrong: {rt.broadcast_channel(prefix='iso_a')}"
    assert rt.topic_channel("orders", prefix="iso_a") == "iso_a:realtime:topic:orders", \
        f"prefixed realtime topic wrong: {rt.topic_channel('orders', prefix='iso_a')}"
    assert rt.messages_channel("c1", prefix="iso_a") == "iso_a:realtime:messages:c1", \
        f"prefixed realtime messages wrong: {rt.messages_channel('c1', prefix='iso_a')}"

    b = JobKeys(prefix="mojo:jobs", pubsub_prefix="iso_b")
    pairs = [
        (a.runners_broadcast(), b.runners_broadcast()),
        (a.runner_ctl("r1"), b.runner_ctl("r1")),
        (a.reply_channel("t"), b.reply_channel("t")),
        (a.ping_reply("t"), b.ping_reply("t")),
        (rt.broadcast_channel(prefix="iso_a"), rt.broadcast_channel(prefix="iso_b")),
        (rt.topic_channel("x", prefix="iso_a"), rt.topic_channel("x", prefix="iso_b")),
        (rt.messages_channel("c", prefix="iso_a"), rt.messages_channel("c", prefix="iso_b")),
    ]
    for left, right in pairs:
        assert left != right, \
            f"two isolation prefixes produced the same channel name: {left}"
