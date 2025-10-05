from . import utils
from mojo.helpers import redis, dates
from mojo.helpers.settings import settings
import datetime
from objict import objict, nobjict

# =========================
# Hash-slot tagging helpers
# =========================

def _tag(account: str) -> str:
    """Constant hash tag per account so all that account's keys share one slot."""
    # Anything inside {...} is the cluster hash-tag.
    return f"{{mets:{account}}}"

def tkey(account: str, key: str) -> str:
    """Prefix a key with the account tag if not already tagged."""
    if key.startswith("{"):
        return key
    return f"{_tag(account)}:{key}"

def tkeys(account: str, keys):
    return [tkey(account, k) for k in keys]


# ==================================
# Cluster-safe multi-key read helper
# ==================================

def mget_any(r, keys):
    """
    Cluster-safe MGET that preserves order:
      - Standalone: r.mget(keys)
      - Cluster: if keys span slots, run MGET per slot and stitch results
    """
    if not keys:
        return []
    try:
        return r.mget(keys)
    except Exception as e:
        msg = str(e)
        if ("same key slot" not in msg) and ("CROSSSLOT" not in msg):
            # Not a slot error; surface it.
            raise

    # Fallback path for cluster: group keys by slot and scatter/gather.
    try:
        from redis.cluster import keyslot
    except Exception:
        # Old redis-py without cluster helpers: re-raise original
        raise

    groups = {}
    for idx, k in enumerate(keys):
        groups.setdefault(keyslot(k), []).append((idx, k))

    out = [None] * len(keys)
    for items in groups.values():
        idxs, ks = zip(*items)
        vals = r.mget(list(ks))
        for i, v in zip(idxs, vals):
            out[i] = v
    return out


# =========
# Recording
# =========

def record(slug, when=None, count=1, category=None, account="global",
           min_granularity="hours", max_granularity="years", timezone=None):
    """
    Records metrics by incrementing counters for various time granularities.
    Keys are hash-tagged per account to keep them in a single cluster slot.
    """
    when = utils.normalize_datetime(when, timezone)
    r = redis.get_connection()
    p = r.pipeline(transaction=False)

    if category is not None:
        add_category_slug(category, slug, p, account)

    add_metrics_slug(slug, p, account)

    granularities = utils.generate_granularities(min_granularity, max_granularity)
    for granularity in granularities:
        base_key = utils.generate_slug(slug, when, granularity, account)
        k = tkey(account, base_key)
        p.incr(k, count)
        exp_at = utils.get_expires_at(granularity, slug, category)
        if exp_at:
            p.expireat(k, exp_at)
    p.execute()


# ========
# Reading
# ========

def fetch(slug, dt_start=None, dt_end=None, granularity="hours",
          redis_con=None, account="global", with_labels=False, dr_slugs=None):
    """
    Fetches metrics for a slug/list of slugs. Uses cluster-safe MGET.
    """
    if redis_con is None:
        redis_con = redis.get_connection()

    if not slug:
        slug = "no_slugs_found"

    if isinstance(slug, (list, set)):
        resp = nobjict()
        if with_labels:
            resp.data = {}
            resp.labels = utils.periods_from_dr_slugs(
                utils.generate_slugs_for_range(slug[0], dt_start, dt_end, granularity, account)
            )
        for s in slug:
            values = fetch(s, dt_start, dt_end, granularity, redis_con, account)
            if with_labels:
                resp.data[s] = values
            else:
                resp[s] = values
        return resp

    dr_slugs = utils.generate_slugs_for_range(slug, dt_start, dt_end, granularity, account)
    tagged = tkeys(account, dr_slugs)
    raw = mget_any(redis_con, tagged)
    values = [int(v) if v is not None else 0 for v in raw]

    if not with_labels:
        return values
    return nobjict(labels=utils.periods_from_dr_slugs(dr_slugs), data={slug: values})


def fetch_values(slugs, when=None, granularity="hours", redis_con=None, account="global", timezone=None):
    """
    Fetch multiple slugs at a single point in time. Uses cluster-safe MGET.
    """
    if redis_con is None:
        redis_con = redis.get_connection()
    when = utils.normalize_datetime(when, timezone)

    # Normalize slugs to list
    if isinstance(slugs, str):
        slugs = [s.strip() for s in slugs.split(',')] if ',' in slugs else [slugs]

    # Build keys and tag them
    redis_keys = [utils.generate_slug(s, when, granularity, account) for s in slugs]
    tagged = tkeys(account, redis_keys)

    raw = mget_any(redis_con, tagged)
    data = {}
    for i, s in enumerate(slugs):
        v = raw[i]
        data[s] = int(v) if v is not None else 0

    return {
        'data': data,
        'slugs': slugs,
        'when': when.isoformat() if hasattr(when, 'isoformat') else str(when),
        'granularity': granularity,
        'account': account
    }


# ===========
# Key indices
# ===========

def add_metrics_slug(slug, redis_con=None, account="global"):
    """Index a metric slug for the account (set membership is single-key, so fine)."""
    if redis_con is None:
        redis_con = redis.get_connection()
    redis_con.sadd(utils.generate_slugs_key(account), slug)


def delete_metrics_slug(slug, account="global", redis_con=None):
    """Remove slug and delete all matching series keys by prefix."""
    if redis_con is None:
        redis_con = redis.get_connection()
    redis_con.srem(utils.generate_slugs_key(account), slug)

    # Delete all keys with the slug prefix (tag the prefix for slot locality).
    prefix = utils.generate_slug_prefix(slug, account)  # e.g., mets:<acct>:<slug>:...
    tagged_prefix = tkey(account, prefix)
    return __delete_keys_with_prefix(tagged_prefix, redis_con)


def __delete_keys_with_prefix(tagged_prefix, redis_conn):
    """
    Cluster-safe iteration: use scan_iter which fans out on cluster clients.
    Expect tagged_prefix to already include the {hash_tag}:...
    """
    total_deleted = 0
    pattern = f"{tagged_prefix}*"
    for key in redis_conn.scan_iter(match=pattern):
        redis_conn.delete(key)
        total_deleted += 1
    return total_deleted


def get_account_slugs(account, redis_con=None):
    """Return all slugs in the account set."""
    if redis_con is None:
        redis_con = redis.get_connection()
    return {s.decode() if isinstance(s, bytes) else s
            for s in redis_con.smembers(utils.generate_slugs_key(account))}


# ===============
# Categories API
# ===============

def add_category_slug(category, slug, redis_con=None, account="global"):
    if redis_con is None:
        redis_con = redis.get_connection()
    redis_con.sadd(utils.generate_category_slug(account, category), slug)
    redis_con.sadd(utils.generate_category_key(account), category)


def get_category_slugs(category, redis_con=None, account="global"):
    if redis_con is None:
        redis_con = redis.get_connection()
    return {s.decode() if isinstance(s, bytes) else s
            for s in redis_con.smembers(utils.generate_category_slug(account, category))}


def delete_category(category, redis_con=None, account="global"):
    if redis_con is None:
        redis_con = redis.get_connection()
    category_slug = utils.generate_category_slug(account, category)
    p = redis_con.pipeline(transaction=False)
    p.delete(category_slug)
    p.srem(utils.generate_category_key(account), category)
    p.execute()


def get_categories(redis_con=None, account="global"):
    if redis_con is None:
        redis_con = redis.get_connection()
    return {s.decode() if isinstance(s, bytes) else s
            for s in redis_con.smembers(utils.generate_category_key(account))}


# ============
# KV helpers
# ============

def set_value(slug, value, redis_con=None, account="global"):
    if redis_con is None:
        redis_con = redis.get_connection()
    base = utils.generate_value_key(slug, account)
    redis_con.set(tkey(account, base), str(value))


def get_value(slug, redis_con=None, account="global", default=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    base = utils.generate_value_key(slug, account)
    v = redis_con.get(tkey(account, base))
    if v is not None and isinstance(v, bytes):
        v = v.decode("utf-8")
    return default if v is None else v


# ===============
# Accounts index
# ===============

def add_account(account, redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    return redis_con.sadd(utils.generate_accounts_key(), account) == 1


def list_accounts(redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    return [a.decode('utf-8') if isinstance(a, bytes) else a
            for a in redis_con.smembers(utils.generate_accounts_key())]


def delete_account(account, redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    set_view_perms(account, None, redis_con)
    set_write_perms(account, None, redis_con)
    return redis_con.srem(utils.generate_accounts_key(), account)


# ============================
# Permission helpers (tagged)
# ============================

def set_view_perms(account, perms, redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    base = utils.generate_perm_view_key(account)
    k = tkey(account, base)
    if perms is None:
        redis_con.delete(k)
    else:
        add_account(account, redis_con)
        if isinstance(perms, list):
            perms = ','.join(perms)
        redis_con.set(k, perms)


def set_write_perms(account, perms, redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    base = utils.generate_perm_write_key(account)
    k = tkey(account, base)
    if perms is None:
        redis_con.delete(k)
    else:
        add_account(account, redis_con)
        if isinstance(perms, list):
            perms = ','.join(perms)
        redis_con.set(k, perms)


def get_view_perms(account, redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    k = tkey(account, utils.generate_perm_view_key(account))
    perms = redis_con.get(k)
    if not perms:
        return None
    if isinstance(perms, bytes):
        perms = perms.decode('utf-8')
    return perms.split(',') if ',' in perms else perms


def get_write_perms(account, redis_con=None):
    if redis_con is None:
        redis_con = redis.get_connection()
    k = tkey(account, utils.generate_perm_write_key(account))
    perms = redis_con.get(k)
    if not perms:
        return None
    if isinstance(perms, bytes):
        perms = perms.decode('utf-8')
    return perms.split(',') if ',' in perms else perms


def get_accounts_with_permissions(redis_con=None):
    """
    Iterate across cluster safely (scan_iter fans out in RedisCluster).
    Returns list of {'account': ..., 'view_permissions': ..., 'write_permissions': ...}
    """
    if redis_con is None:
        redis_con = redis.get_connection()

    accounts = {}

    # View perms
    for key in redis_con.scan_iter(match="{mets:*}:mets:*:perm:v"):
        key_str = key.decode('utf-8') if isinstance(key, bytes) else key
        parts = key_str.split(':')
        # format after tagging: {mets:<acct>}:mets:<acct>:perm:v
        if len(parts) >= 4:
            acct = parts[1].split('}')[0].split('{')[-1].replace('mets:', '') if parts[0].startswith('{') else parts[1]
            accounts.setdefault(acct, {"account": acct, "view_permissions": None, "write_permissions": None})
            perms = redis_con.get(key)
            if perms:
                if isinstance(perms, bytes):
                    perms = perms.decode('utf-8')
                if ',' in perms:
                    perms = perms.split(',')
                accounts[acct]["view_permissions"] = perms

    # Write perms
    for key in redis_con.scan_iter(match="{mets:*}:mets:*:perm:w"):
        key_str = key.decode('utf-8') if isinstance(key, bytes) else key
        parts = key_str.split(':')
        if len(parts) >= 4:
            acct = parts[1].split('}')[0].split('{')[-1].replace('mets:', '') if parts[0].startswith('{') else parts[1]
            accounts.setdefault(acct, {"account": acct, "view_permissions": None, "write_permissions": None})
            perms = redis_con.get(key)
            if perms:
                if isinstance(perms, bytes):
                    perms = perms.decode('utf-8')
                if ',' in perms:
                    perms = perms.split(',')
                accounts[acct]["write_permissions"] = perms

    return list(accounts.values())

def fetch_by_category(category, dt_start=None, dt_end=None, granularity="hours",
                      redis_con=None, account="global", with_labels=False):
    """
    Fetch metrics for all slugs in a category over a range/granularity.
    Delegates to `fetch`, which is cluster-safe (hash tags + mget_any).
    """
    slugs = get_category_slugs(category, redis_con, account)
    return fetch(slugs, dt_start=dt_start, dt_end=dt_end,
                 granularity=granularity, redis_con=redis_con,
                 account=account, with_labels=with_labels)
