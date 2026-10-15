# Presence Checks — Django Developer Reference

A user (or any realtime identity) is online while its Redis presence key
`realtime:online:<user_type>:<id>` exists. The socket writes it on login and
removes it when the last connection closes.

## is_online()

```python
from mojo.apps import realtime

if realtime.is_online("user", 42):
    ...
```

One Redis `EXISTS` for one id. `User.is_online` uses the same check.

## count_online()

Count how many of many ids are online:

```python
from mojo.apps import realtime

online = realtime.count_online("door", door_ids)              # int
online = realtime.count_online("door", door_ids, chunk_size=500)
```

- Ids are compared as text and de-duplicated: `5` and `"5"` count once.
- An id counts exactly when `is_online` would say it is online.
- Empty input returns `0` without touching Redis.
- `chunk_size` must be an `int` of at least 1, else `ValueError`. A bare `str` or
  `bytes` as `user_ids` raises `TypeError` (it would otherwise be split into
  characters).
- The count is exact: no cache, cap or estimate, and no KEYS or SCAN.
- Each chunk is read at its own moment; the total is not atomic across chunks.
- Redis errors are raised, never returned as a partial sum or a zero.

### Cost

| Topology | Round trips |
|---|---|
| Single Redis node | One multi-key `EXISTS` per chunk: `ceil(unique ids / chunk_size)`, so 10 for 10,000 ids. |
| Redis Cluster (`REDIS_CLUSTER=True`) | redis-py splits each chunk by hash slot and pipelines it: up to one exchange per node the chunk touches, plus retries while slots move. Still exact. |

On a cluster with replica reads on (the default, `REDIS_READ_FROM_REPLICAS`),
a count can trail a just-connected or just-disconnected user by replication
lag, as `is_online` can.

A cluster-mode server must run with `REDIS_CLUSTER=True`. A plain client sends
one `EXISTS` across hash slots, the server refuses it with `CROSSSLOT`, and
`count_online` raises; single-key `is_online` still works there.
