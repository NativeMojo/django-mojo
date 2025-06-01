import time

from objict import objict

from mojo.ws4redis.redis import RedisMessage, RedisStore, getRedisClient, getPoolStatus


def buildEventMessage(name=None, message=None, priority=0, model=None, model_pk=None, custom=None):
    msg = objict(priority=priority)
    if name:
        msg["name"] = name

    if message:
        msg["message"] = message

    if model:
        msg["component"] = objict(pk=model_pk, model=model)

    if custom:
        msg.update(custom)
    return msg.toJSON(as_string=True)


def ping():
    return getRedisClient().ping()


def exists(key, default=None):
    c = getRedisClient()
    return c.exists(key)


def keys(keys):
    c = getRedisClient()
    return [v.decode() for v in c.keys(keys)]


def get(key, default=None, field_type=None):
    c = getRedisClient()
    v = c.get(key)
    if v is None:
        return default
    if field_type is not None:
        if field_type == "json":
            return objict.fromJSON(v, ignore_errors=True)
        if field_type in [str, "str"]:
            v = v.decode()
        return field_type(v)
    return v


def set(key, value, expire=None):
    c = getRedisClient()
    v = c.set(key, value)
    if expire:
        c.expire(key, expire)
    return v


def incr(key, amount=1, expire=None):
    c = getRedisClient()
    v = c.incr(key, amount)
    if expire:
        c.expire(key, expire)
    return v


def expire(key, expire):
    c = getRedisClient()
    return c.expire(key, expire)


def decr(key, amount=1):
    c = getRedisClient()
    return c.decr(key, amount)


def delete(key):
    c = getRedisClient()
    return c.delete(key)


# SET FUNCTIONS
def sadd(name, *values):
    # add value to set
    c = getRedisClient()
    return c.sadd(name, *values)


def srem(name, *values):
    # remove value from set
    c = getRedisClient()
    return c.srem(name, *values)


def sismember(name, value):
    # return items in set
    c = getRedisClient()
    return c.sismember(name, value)


def scard(name):
    # count items in set
    c = getRedisClient()
    return c.scard(name)


def smembers(name):
    # return items in set
    c = getRedisClient()
    return c.smembers(name)


# HASH FUNCTIONS
def hget(name, field, default=None):
    c = getRedisClient()
    v = c.hget(name, field)
    if v is None:
        return default
    return v


def hgetall(name):
    c = getRedisClient()
    return c.hgetall(name)


def hset(name, field, value):
    c = getRedisClient()
    return c.hset(name, field, value)


def hdel(name, field):
    c = getRedisClient()
    return c.hdel(name, field)


def hincrby(name, field, inc=1):
    c = getRedisClient()
    return c.hincrby(name, field, inc)


def lpush(name, value, unique=False):
    c = getRedisClient()
    if isinstance(value, list):
        for v in value:
            if unique and value.encode() in c.lrange(name, 0, -1):
                return 0
            c.lpush(name, v)
        return len(value)
    if unique and value.encode() in c.lrange(name, 0, -1):
        return 0
    return c.lpush(name, value)


def rpush(name, value, unique=False):
    c = getRedisClient()
    if isinstance(value, list):
        for v in value:
            if unique and value.encode() in c.lrange(name, 0, -1):
                return 0
            c.rpush(name, v)
        return len(value)
    if unique and value.encode() in c.lrange(name, 0, -1):
        return 0
    return c.rpush(name, value)


def lpop(name, timeout=None):
    c = getRedisClient()
    if timeout is None:
        return c.lpop(name)
    r = c.blpop(name, timeout=timeout)
    if isinstance(r, tuple):
        return r[1].decode()


def rpop(name, timeout=None):
    c = getRedisClient()
    if timeout is None:
        return c.rpop(name)
    r = c.brpop(name, timeout=timeout)
    if isinstance(r, tuple):
        return r[1].decode()


def lrem(name, value, occurences=1):
    c = getRedisClient()
    return c.lrem(name, occurences, value.encode())


def vpop(name, value, timeout=None):
    c = getRedisClient()
    start = time.time()
    while c.lrem(name, 1, value) == 0:
        if timeout is None:
            return 0
        time.sleep(1.0)
        elapsed = time.time() - start
        if elapsed > timeout:
            return 0
    return 1


def lrange(name, start, end):
    c = getRedisClient()
    return [v.decode() for v in c.lrange(name, start, end)]


def sendToUser(user, name, message=None, priority=0, model=None, model_pk=None, custom=None):
    return sendMessageToUsers([user], buildEventMessage(name, message, priority, model, model_pk, custom))


def sendToUsers(users, name, message=None, priority=0, model=None, model_pk=None, custom=None):
    return sendMessageToUsers(users, buildEventMessage(name, message, priority, model, model_pk, custom))


def sendMessageToUsers(users, msg):
    return RedisStore().publish(RedisMessage(msg), channel="user", pk=[u.username for u in users])


def sendToGroup(group, name, message=None, priority=0, model=None, model_pk=None, custom=None):
    return sendMessageToModels("group", [group], buildEventMessage(name, message, priority, model, model_pk, custom))


def sendToGroups(groups, name, message=None, priority=0, model=None, model_pk=None, custom=None):
    return sendMessageToModels("group", groups, buildEventMessage(name, message, priority, model, model_pk, custom))


def sendToModels(channel, models, name, message=None, priority=0, model=None, model_pk=None, custom=None):
    return sendMessageToModels(channel, models, buildEventMessage(name, message, priority, model, model_pk, custom))


def sendMessageToModels(channel, models, msg):
    return RedisStore().publish(RedisMessage(msg), channel=channel, pk=[g.pk for g in models])


def sendMessageToPK(channel, pk, msg):
    return RedisStore().publish(RedisMessage(msg), channel=channel, pk=pk)


def broadcast(name, message=None, priority=0, model=None, model_pk=None, custom=None):
    return broadcastMessage(buildEventMessage(name, message, priority, model, model_pk, custom))


def broadcastMessage(msg):
    return RedisStore().publish(RedisMessage(msg), channel="broadcast")


def publish(key, data, c=None):
    if c is None:
        c = getRedisClient()
    if isinstance(data, dict):
        if not isinstance(data, objict):
            data = objict(data)
        data = data.toJSON(as_string=True)
    return c.publish(key, data)


def subscribe(channel):
    c = getRedisClient()
    pubsub = c.pubsub()
    pubsub.subscribe(channel)
    return pubsub


def waitForMessage(pubsub, msg_filter, timeout=55):
    timeout_at = time.time() + timeout
    while time.time() < timeout_at:
        message = pubsub.get_message()
        if message is not None:
            if message.get("type") == "message":
                msg = objict.fromJSON(message.get("data"))
                if msg_filter(msg):
                    pubsub.unsubscribe()
                    return msg
        time.sleep(1.0)
    pubsub.unsubscribe()
    return None


def isOnline(name, pk):
    return sismember(f"{name}:online", pk)
