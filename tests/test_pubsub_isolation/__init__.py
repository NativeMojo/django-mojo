"""Pub/Sub checkout isolation (maestro #3365).

REDIS_PUBSUB_PREFIX must namespace every framework Pub/Sub channel (jobs
broadcast/ctl/replies/ping, realtime broadcast/topic/messages) so parallel
test checkouts sharing one Redis server never hear each other — Redis
Pub/Sub ignores database numbers, so the per-checkout db index does not
cover messages.

Serial for execution reasons, not state ones: test_production_paths_prefixed
publishes on the ACTIVE-prefix broadcast channel, which every authenticated
websocket in this checkout subscribes to (handler.py), and tests/test_realtime
asserts on the first "message" frame each socket receives — running
concurrently would inject stray frames in both directions. No settings are
mutated and nothing is patched; foreign prefixes are injected through
builder arguments and inline name construction only.
"""

TESTIT = {
    "default_core": True,  # framework bucket
    "requires_apps": ["mojo.apps.jobs", "mojo.apps.realtime"],
    "serial": True,
}
