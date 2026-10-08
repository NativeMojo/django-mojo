"""#4567: the per-identity connection cap counts live connections only.

An identity's online set (`realtime:online:<type>:<id>`) holds one member per
connection, and the cap counted its members. The set's expiry is renewed by
every live sibling, so a member whose cleanup was missed was counted for as
long as the identity kept any socket open: eight dead ids and two live ones
refused every new socket at a cap of ten.

A member is alive while its `realtime:connections:<id>` record exists. These
tests drive the real WebSocketHandler in-process over a fake socket, against
the checkout's Redis, with their own user.
"""
import asyncio
import json
import time
import uuid

from testit import helpers as th

USERNAME = "ws_presence_4567"
PASSWORD = "testit##mojo"
USER_TYPE = "user"


@th.django_unit_setup()
def setup_presence_user(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.jwtoken import JWToken

    User.objects.filter(username=USERNAME).delete()
    user = User(username=USERNAME, display_name=USERNAME,
                email=f"{USERNAME}@example.com", is_email_verified=True)
    user.save_password(PASSWORD)
    user.save()
    opts.pc_uid = user.pk
    opts.pc_token = JWToken(user.get_auth_key()).create_access_token(uid=user.pk)
    _reset(user.pk)


def _redis():
    from mojo.helpers.redis import get_connection
    return get_connection()


def _online_key(uid):
    return f"realtime:online:{USER_TYPE}:{uid}"


def _record_key(connection_id):
    return f"realtime:connections:{connection_id}"


def _members(uid):
    return {m.decode() if isinstance(m, bytes) else m for m in _redis().smembers(_online_key(uid))}


def _refusals(uid):
    """The cap's incident events for this user. They carry no uid: a refused
    socket has no identity."""
    from mojo.apps.incident.models import Event
    return Event.objects.filter(
        category="traffic:ws_maxconn", details__contains=f"for {USER_TYPE}:{uid} ")


def _reset(uid):
    """Remove this user's presence, its refusal marker and its incidents."""
    redis = _redis()
    redis.delete(_online_key(uid))
    redis.delete(f"rl:ws_maxconn:{USER_TYPE}:{uid}")
    _refusals(uid).delete()


def _seed_stale(uid, count):
    """Add `count` members that have no connection record, as a missed
    cleanup leaves them. Returns their ids."""
    ids = [f"stale-{uuid.uuid4()}" for _ in range(count)]
    redis = _redis()
    redis.sadd(_online_key(uid), *ids)
    assert not any(redis.exists(_record_key(cid)) for cid in ids), "a seeded stale id must have no record"
    return set(ids)


class _ClientSocket:
    """In-process stand-in for the ASGI socket wrapper (see keepalive.py)."""

    def __init__(self, answer_pings=False):
        self.inbound = asyncio.Queue()
        self.sent = []
        self.answer_pings = answer_pings
        self.server_closed = False

    def client_send(self, frame):
        self.inbound.put_nowait(json.dumps(frame))

    def client_hang_up(self):
        self.inbound.put_nowait(None)

    def client_reset(self):
        """The transport dies under the handler: no close frame, an error."""
        self.inbound.put_nowait(ConnectionResetError("connection reset by peer (simulated)"))

    async def __aiter__(self):
        while True:
            message = await self.inbound.get()
            if message is None:
                return
            if isinstance(message, Exception):
                raise message
            yield message

    async def send(self, message):
        frame = json.loads(message)
        self.sent.append(frame)
        if self.answer_pings and frame.get("type") == "ping":
            self.client_send({"type": "pong", "ts": frame.get("ts")})

    async def close(self, code=1000):
        self.server_closed = True
        self.client_hang_up()

    def frames(self, kind):
        return [frame for frame in self.sent if frame.get("type") == kind]


class _Session:
    """One in-process socket: opened and authenticated, held, then hung up."""

    def __init__(self, token, answer_pings=False, **seams):
        from mojo.apps.realtime.handler import WebSocketHandler
        seams.setdefault("idle_timeout", 30)
        seams.setdefault("ping_seconds", 0)
        self.token = token
        self.socket = _ClientSocket(answer_pings=answer_pings)
        self.handler = WebSocketHandler(self.socket, "/ws/realtime/", **seams)
        self.task = None

    @property
    def connection_id(self):
        return self.handler.connection_id

    async def open(self, timeout=10.0):
        """Authenticate and return "admitted" or the refusal text."""
        self.socket.client_send({"type": "authenticate", "token": self.token, "prefix": "bearer"})
        self.task = asyncio.create_task(self.handler.handle_connection())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.socket.frames("auth_success"):
                return "admitted"
            errors = self.socket.frames("error")
            if errors:
                return errors[0].get("message")
            await asyncio.sleep(0.01)
        return f"no answer in {timeout}s: {self.socket.sent}"

    async def close(self):
        if self.task is None:
            return
        if not self.task.done():
            self.socket.client_hang_up()
        await asyncio.wait_for(self.task, timeout=15)


async def _close_all(sessions):
    for session in sessions:
        try:
            await session.close()
        except Exception:
            pass


async def _wait_until(condition, timeout):
    """Poll a blocking condition off the event loop until it holds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(condition):
            return True
        await asyncio.sleep(0.05)
    return await asyncio.to_thread(condition)


# ---------------------------------------------------------------------------
# Part A: the count
# ---------------------------------------------------------------------------

@th.django_unit_test("#4567: stale members do not consume the connection cap")
def test_stale_members_do_not_consume_the_cap(opts):
    """The regression. One live socket keeps the set alive; dead ids fill the
    rest of the cap; the next socket must still authenticate. Uses the
    process's own cap and no test seam, so it runs unchanged on older code."""
    from mojo.apps.realtime import handler as handler_module
    uid = opts.pc_uid
    cap = handler_module.WS_MAX_CONNECTIONS
    assert cap >= 2, f"this test needs the connection cap enabled, WS_MAX_CONNECTIONS={cap}"
    _reset(uid)

    async def scenario():
        live = _Session(opts.pc_token)
        second = _Session(opts.pc_token)
        try:
            assert await live.open() == "admitted", f"the first socket must authenticate: {live.socket.sent}"
            stale = await asyncio.to_thread(_seed_stale, uid, cap - 1)
            outcome = await second.open()
            members = await asyncio.to_thread(_members, uid)
            return outcome, stale, members, live.connection_id, second.connection_id
        finally:
            await _close_all([second, live])

    try:
        outcome, stale, members, live_id, second_id = asyncio.run(scenario())
        assert outcome == "admitted", (
            f"one live socket and {cap - 1} ids with no connection record must leave "
            f"room under a cap of {cap}; the next socket got: {outcome}")
        assert not (members & stale), f"the dead ids must be removed at admission, still present: {members & stale}"
        assert members == {live_id, second_id}, f"the set holds the two live sockets only, got {members}"
    finally:
        _reset(uid)


@th.django_unit_test("#4567: a full cap of live connections still refuses the next socket")
def test_live_cap_still_refuses(opts):
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        sessions = [_Session(opts.pc_token, max_connections=2) for _ in range(3)]
        try:
            outcomes = [await session.open() for session in sessions]
            closed = await _wait_until(lambda: sessions[2].socket.server_closed, 5)
            members = await asyncio.to_thread(_members, uid)
            return outcomes, closed, members, [s.connection_id for s in sessions]
        finally:
            await _close_all(sessions)

    try:
        outcomes, closed, members, ids = asyncio.run(scenario())
        assert outcomes[:2] == ["admitted", "admitted"], f"two sockets fit a cap of 2: {outcomes}"
        assert outcomes[2] == "Too many connections", f"the third live socket must be refused, got {outcomes[2]}"
        assert closed, "a refused socket is closed by the server"
        assert members == set(ids[:2]), f"a refused socket is not a member, got {members}"
        events = _refusals(uid).count()
        assert events == 1, f"a refusal reports one incident, got {events}"
    finally:
        _reset(uid)


@th.django_unit_test("#4567: simultaneous admissions never exceed the cap")
def test_concurrent_admissions_respect_the_cap(opts):
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        sessions = [_Session(opts.pc_token, max_connections=3) for _ in range(8)]
        try:
            outcomes = await asyncio.gather(*(session.open() for session in sessions))
            members = await asyncio.to_thread(_members, uid)
            admitted = {s.connection_id for s, o in zip(sessions, outcomes) if o == "admitted"}
            return outcomes, members, admitted
        finally:
            await _close_all(sessions)

    try:
        outcomes, members, admitted = asyncio.run(scenario())
        assert outcomes.count("admitted") == 3, f"exactly 3 of 8 simultaneous sockets fit a cap of 3: {outcomes}"
        assert outcomes.count("Too many connections") == 5, f"the other 5 are refused: {outcomes}"
        assert members == admitted, f"the set holds exactly the admitted sockets: {members} != {admitted}"
    finally:
        _reset(uid)


@th.django_unit_test("#4567: a stale member ages out while a sibling keeps refreshing")
def test_stale_member_ages_out_while_a_sibling_refreshes(opts):
    """No new admission: the live sibling's own server ping timer removes it."""
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        live = _Session(opts.pc_token, ping_seconds=0.2)
        try:
            assert await live.open() == "admitted", f"the socket must authenticate: {live.socket.sent}"
            stale = await asyncio.to_thread(_seed_stale, uid, 3)
            healed = await _wait_until(lambda: not (_members(uid) & stale), 5)
            return healed, await asyncio.to_thread(_members, uid), live.connection_id
        finally:
            await _close_all([live])

    try:
        healed, members, live_id = asyncio.run(scenario())
        assert healed, f"dead ids must leave the set within a heartbeat of a live sibling, still: {members}"
        assert members == {live_id}, f"the live socket stays, alone: {members}"
    finally:
        _reset(uid)


@th.django_unit_test("#4567: pruning never removes a live connection")
def test_prune_never_removes_a_live_connection(opts):
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        sessions = [_Session(opts.pc_token, ping_seconds=0.05) for _ in range(2)]
        try:
            for session in sessions:
                assert await session.open() == "admitted", f"the socket must authenticate: {session.socket.sent}"
            expected = {s.connection_id for s in sessions}
            samples = []
            for _ in range(20):
                samples.append(await asyncio.to_thread(_members, uid))
                await asyncio.sleep(0.05)
            return expected, samples
        finally:
            await _close_all(sessions)

    try:
        expected, samples = asyncio.run(scenario())
        missing = [sample for sample in samples if sample != expected]
        assert not missing, (
            f"two live sockets pruning on every heartbeat must both stay members "
            f"({expected}); saw {missing[:3]}")
    finally:
        _reset(uid)


@th.django_unit_test("#4567: a healthy session stays exactly one member")
def test_healthy_session_stays_one_member(opts):
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        live = _Session(opts.pc_token, answer_pings=True, ping_seconds=0.1)
        try:
            assert await live.open() == "admitted", f"the socket must authenticate: {live.socket.sent}"
            await asyncio.sleep(0.8)
            members = await asyncio.to_thread(_members, uid)
            ttl = await asyncio.to_thread(_redis().ttl, _record_key(live.connection_id))
            return members, ttl, live.connection_id, len(live.socket.frames("ping"))
        finally:
            await _close_all([live])

    try:
        members, ttl, live_id, pings = asyncio.run(scenario())
        assert pings >= 4, f"the session must have gone through several heartbeats, got {pings} pings"
        assert members == {live_id}, f"one multiplexed connection is one member, got {members}"
        assert ttl > 0, f"its connection record is alive and expiring, ttl={ttl}"
    finally:
        _reset(uid)


@th.django_unit_test("#4567: a client that sends nothing keeps its connection record")
def test_silent_client_keeps_its_record(opts):
    """The record is refreshed by the server's timer, not by client frames,
    and is written again if it was lost."""
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        live = _Session(opts.pc_token, ping_seconds=0.2)
        try:
            assert await live.open() == "admitted", f"the socket must authenticate: {live.socket.sent}"
            key = _record_key(live.connection_id)
            redis = _redis()
            await asyncio.to_thread(redis.expire, key, 20)
            extended = await _wait_until(lambda: redis.ttl(key) > 20, 5)
            await asyncio.to_thread(redis.delete, key)
            rewritten = await _wait_until(lambda: redis.exists(key) == 1, 5)
            await asyncio.sleep(0.5)
            members = await asyncio.to_thread(_members, uid)
            return extended, rewritten, members, live.connection_id
        finally:
            await _close_all([live])

    try:
        extended, rewritten, members, live_id = asyncio.run(scenario())
        assert extended, "the server timer must extend a silent client's record"
        assert rewritten, "a live handler must write its record again when it is gone"
        assert members == {live_id}, f"and the socket stays a member, got {members}"
    finally:
        _reset(uid)


@th.django_unit_test("#4567: a refresh after cleanup does not bring a closed connection back")
def test_closed_handler_is_not_resurrected(opts):
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        session = _Session(opts.pc_token)
        assert await session.open() == "admitted", f"the socket must authenticate: {session.socket.sent}"
        await session.close()
        await session.handler.refresh_presence(force=True)
        record = await asyncio.to_thread(_redis().exists, _record_key(session.connection_id))
        return record, await asyncio.to_thread(_members, uid)

    try:
        record, members = asyncio.run(scenario())
        assert record == 0, "a late refresh must not write the record of a closed connection"
        assert members == set(), f"or put it back in the online set, got {members}"
    finally:
        _reset(uid)


@th.django_unit_test("#4567: a cap of zero admits every socket")
def test_cap_disabled(opts):
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        sessions = [_Session(opts.pc_token, max_connections=0) for _ in range(4)]
        try:
            outcomes = [await session.open() for session in sessions]
            return outcomes, await asyncio.to_thread(_members, uid), {s.connection_id for s in sessions}
        finally:
            await _close_all(sessions)

    try:
        outcomes, members, ids = asyncio.run(scenario())
        assert outcomes == ["admitted"] * 4, f"a cap of 0 disables the limit: {outcomes}"
        assert members == ids, f"every socket is a member: {members}"
    finally:
        _reset(uid)


@th.django_unit_test("#4567: a live connection removed from the set cannot pass the cap on its way back")
def test_removed_connection_cannot_pass_the_cap(opts):
    """Review 84761: a prune removes live A on a stale read, B takes the
    place, and A's next heartbeat must not add A back over the cap."""
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        first = _Session(opts.pc_token, max_connections=1)
        second = _Session(opts.pc_token, max_connections=1)
        try:
            assert await first.open() == "admitted", f"A must authenticate: {first.socket.sent}"
            redis = _redis()
            assert redis.exists(_record_key(first.connection_id)), "A is live: it has a record"
            # What a prune working from a stale read does to a live member.
            redis.srem(_online_key(uid), first.connection_id)
            outcome = await second.open()
            assert outcome == "admitted", f"B takes the free place: {outcome}"
            await first.handler.refresh_presence(force=True)
            after_refresh = _members(uid)
            # A refused heartbeat closes the socket; only then is there a
            # cleanup to wait for.
            if first.socket.server_closed:
                await asyncio.wait_for(first.task, timeout=15)
            return {
                "after_refresh": after_refresh,
                "closing": first.handler._closing,
                "errors": [frame.get("message") for frame in first.socket.frames("error")],
                "members": _members(uid),
                "first_record": redis.exists(_record_key(first.connection_id)),
                "second_record": redis.exists(_record_key(second.connection_id)),
                "second": second.connection_id,
            }
        finally:
            await _close_all([first, second])

    try:
        seen = asyncio.run(scenario())
        assert seen["after_refresh"] == {seen["second"]}, (
            f"at a cap of 1 the set holds only B after A's heartbeat, got {seen['after_refresh']}")
        assert seen["closing"], "A is over the cap and must be closing"
        assert seen["errors"] == ["Too many connections"], f"A is told why: {seen['errors']}"
        assert seen["members"] == {seen["second"]}, f"after A's cleanup the set is still only B: {seen['members']}"
        assert seen["first_record"] == 0 and seen["second_record"] == 1, (
            f"exactly one live record is left, B's: A {seen['first_record']}, B {seen['second_record']}")
    finally:
        _reset(uid)


@th.django_unit_test("#4567: a live connection removed from the set is re-admitted when there is room")
def test_removed_connection_is_readmitted_with_room(opts):
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        session = _Session(opts.pc_token, max_connections=2)
        try:
            assert await session.open() == "admitted", f"the socket must authenticate: {session.socket.sent}"
            _redis().srem(_online_key(uid), session.connection_id)
            assert _members(uid) == set(), "the connection was removed from the set"
            await session.handler.refresh_presence(force=True)
            return (_members(uid), session.connection_id, session.handler._closing,
                    session.socket.frames("error"), session.task.done())
        finally:
            await _close_all([session])

    try:
        members, cid, closing, errors, done = asyncio.run(scenario())
        assert members == {cid}, f"with room under the cap the heartbeat puts it back, got {members}"
        assert not closing and not done and errors == [], (
            f"and the connection stays open: closing {closing}, done {done}, errors {errors}")
    finally:
        _reset(uid)


class _FailingRedis:
    """The checkout's Redis client with chosen commands made to raise."""

    def __init__(self, *failing):
        self._client = _redis()
        self._failing = set(failing)
        self.failed = []

    def __getattr__(self, name):
        if name in self._failing:
            def fail(*args, **kwargs):
                self.failed.append(name)
                raise RuntimeError(f"redis {name} failed (simulated)")
            return fail
        return getattr(self._client, name)


@th.django_unit_test("#4567: a failing admission fails open")
def test_admit_failure_fails_open(opts):
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        redis = _FailingRedis("eval")
        session = _Session(opts.pc_token, max_connections=1, redis_client=redis)
        try:
            outcome = await session.open()
            return outcome, redis.failed, await asyncio.to_thread(_members, uid), session.connection_id
        finally:
            await _close_all([session])

    try:
        outcome, failed, members, cid = asyncio.run(scenario())
        assert "eval" in failed, f"the admission script must have been attempted, failed calls: {failed}"
        assert outcome == "admitted", f"a Redis error at admission must not refuse the socket, got {outcome}"
        assert members == {cid}, f"and the socket is still registered online, got {members}"
    finally:
        _reset(uid)


@th.django_unit_test("#4567: an online key that is not a set is left alone")
def test_non_set_key_is_left_alone(opts):
    from mojo.apps.realtime import presence
    uid = opts.pc_uid
    _reset(uid)
    redis = _redis()
    key = _online_key(uid)
    try:
        redis.set(key, "legacy", ex=60)
        assert presence.prune(redis, key) == 0, "a key that is not a set counts as no live connection"
        value = redis.get(key)
        value = value.decode() if isinstance(value, bytes) else value
        assert value == "legacy", f"and is not rewritten or removed, got {value!r}"
    finally:
        _reset(uid)


# ---------------------------------------------------------------------------
# Part B: the lifecycle
# ---------------------------------------------------------------------------

def _topic_key(topic):
    return f"realtime:topic:{topic}"


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


async def _open_live(token, **seams):
    """An authenticated session with its server ping timer running, plus the
    names of what it holds: its topics, its pub/sub and its background tasks."""
    seams.setdefault("ping_seconds", 0.2)
    session = _Session(token, answer_pings=True, **seams)
    outcome = await session.open()
    assert outcome == "admitted", f"the socket must authenticate: {outcome}"
    handler = session.handler
    topics = set(handler.subscribed_topics)
    assert topics, "an authenticated socket is subscribed to its own topic"
    assert handler.pubsub is not None and handler._redis_task is not None, "pub/sub must be running"
    assert handler._ping_task is not None, "the server ping timer must be running"
    return session, topics


async def _leftovers(session, topics, uid, own_tasks):
    """Everything a finished handler may still hold, as a list of findings.
    Empty means released. `own_tasks` are the scenario's tasks that are
    allowed to be alive."""
    handler = session.handler
    cid = session.connection_id
    redis = _redis()

    def redis_state():
        found = []
        if redis.exists(_record_key(cid)):
            found.append("the connection record still exists")
        if redis.sismember(_online_key(uid), cid):
            found.append("the id is still in the online set")
        for topic in sorted(topics):
            if redis.sismember(_topic_key(topic), cid):
                found.append(f"the id is still in the topic set of {topic}")
        return found

    found = await asyncio.to_thread(redis_state)
    if not handler._ping_task.done():
        found.append("the ping task is still running")
    if not handler._redis_task.done():
        found.append("the pub/sub task is still running")
    if handler.pubsub.connection is not None:
        found.append("the pub/sub connection is still open")
    # The two child tasks of handle_connection, and anything else the handler
    # started: after it returns, no task but the scenario's own may be alive.
    await asyncio.sleep(0.05)
    alive = [task for task in asyncio.all_tasks()
             if task not in own_tasks and not task.done()]
    for task in alive:
        found.append(f"a task is still running: {task.get_coro().__qualname__}")
        task.cancel()
    return found


@th.django_unit_test("#4567: a clean disconnect releases the record, the memberships, the tasks and pub/sub")
def test_clean_disconnect_releases_everything(opts):
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        session, topics = await _open_live(opts.pc_token)
        session.socket.client_hang_up()
        await asyncio.wait_for(session.task, timeout=15)
        return await _leftovers(session, topics, uid, {asyncio.current_task()})

    try:
        found = asyncio.run(scenario())
        assert found == [], f"a clean disconnect must release everything, left: {found}"
    finally:
        _reset(uid)


@th.django_unit_test("#4567: an abrupt disconnect releases the record, the memberships, the tasks and pub/sub")
def test_abrupt_disconnect_releases_everything(opts):
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        session, topics = await _open_live(opts.pc_token)
        session.socket.client_reset()
        await asyncio.wait_for(session.task, timeout=15)
        return await _leftovers(session, topics, uid, {asyncio.current_task()})

    try:
        found = asyncio.run(scenario())
        assert found == [], f"a reset transport must release everything, left: {found}"
    finally:
        _reset(uid)


@th.django_unit_test("#4567: a cancelled handler releases the record, the memberships, the tasks and pub/sub")
def test_cancelled_handler_releases_everything(opts):
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        session, topics = await _open_live(opts.pc_token)
        session.task.cancel()
        try:
            await asyncio.wait_for(session.task, timeout=15)
        except asyncio.CancelledError:
            pass
        assert session.task.done(), "the cancelled handler must finish"
        return await _leftovers(session, topics, uid, {asyncio.current_task()})

    try:
        found = asyncio.run(scenario())
        assert found == [], f"a cancelled handler must release everything, left: {found}"
    finally:
        _reset(uid)


@th.django_unit_test("#4567: one failing Redis call at cleanup does not skip the others")
def test_one_failed_redis_call_does_not_skip_the_rest(opts):
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        redis = _FailingRedis("delete")
        session, topics = await _open_live(opts.pc_token, redis_client=redis)
        session.socket.client_hang_up()
        await asyncio.wait_for(session.task, timeout=15)
        found = await _leftovers(session, topics, uid, {asyncio.current_task()})
        return found, redis.failed, session.connection_id

    cid = None
    try:
        found, failed, cid = asyncio.run(scenario())
        assert "delete" in failed, f"the record removal must have been attempted and failed: {failed}"
        assert found == ["the connection record still exists"], (
            f"only the record, whose removal failed, may be left (it expires): {found}")
    finally:
        if cid:
            _redis().delete(_record_key(cid))
        _reset(uid)


@th.django_unit_test("#4567: a dead worker leaves only state that expires or is pruned")
def test_worker_death_leaves_only_expiring_state(opts):
    from mojo.apps.realtime import presence
    uid = opts.pc_uid
    _reset(uid)

    async def scenario():
        session, topics = await _open_live(opts.pc_token)
        cid = session.connection_id
        redis = _redis()
        keys = [_record_key(cid), _online_key(uid)] + [_topic_key(topic) for topic in sorted(topics)]

        def expiries():
            return {key: redis.ttl(key) for key in keys}

        def record_expired_then_pruned():
            # The worker died: no cleanup ran, and the record ran out.
            redis.delete(_record_key(cid))
            left = presence.prune(redis, _online_key(uid))
            return left, redis.sismember(_online_key(uid), cid)

        try:
            ttls = await asyncio.to_thread(expiries)
            # Stop the handler's own heartbeat without any cleanup, as a
            # killed process would.
            session.handler._closing = True
            left, still_member = await asyncio.to_thread(record_expired_then_pruned)
            return ttls, left, still_member
        finally:
            await _close_all([session])

    try:
        ttls, left, still_member = asyncio.run(scenario())
        without = {key: ttl for key, ttl in ttls.items() if ttl <= 0}
        assert not without, f"every key of a live socket must carry an expiry, without one: {without}"
        assert left == 0 and not still_member, (
            f"once the record is gone a prune removes the member, left {left}, member {still_member}")
    finally:
        _reset(uid)
