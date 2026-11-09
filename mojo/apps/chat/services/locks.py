"""Stable account-key fences for transactions that will lock chat rooms.

Account closure/key changes take User FOR UPDATE before room locks. Chat
writers must acquire compatible User FOR KEY SHARE fences first as well;
waiting for an INSERT's later FK check while holding a room reverses that
order. These fences establish key existence only, never active-user policy.
"""
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.transaction import TransactionManagementError
from django.db.utils import NotSupportedError


def lock_users(user_ids, *, using=DEFAULT_DB_ALIAS):
    """Lock existing User keys in PK order until the OUTERMOST commit.

    Returns their sorted tuple of IDs so a host can revalidate its roster.
    Call inside atomic(default), before business/room locks. An existing host
    protocol's pair advisory fences precede this helper. Only primary is
    supported; callers may not place account-existence locks on a replica.
    """
    if using != DEFAULT_DB_ALIAS:
        raise ValueError("Chat user fences require the primary default database")
    connection = connections[using]
    if not connection.in_atomic_block:
        raise TransactionManagementError("lock_users requires an active primary transaction.atomic block")
    if connection.vendor != "postgresql":
        raise NotSupportedError("Chat user key-share fences require PostgreSQL")
    ids = sorted({int(pk) for pk in user_ids if pk is not None})
    if not ids:
        return ()
    from mojo.apps.account.models import User
    table = connection.ops.quote_name(User._meta.db_table)
    pk_column = connection.ops.quote_name(User._meta.pk.column)
    placeholders = ", ".join(["%s"] * len(ids))
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {pk_column} FROM {table} WHERE {pk_column} IN ({placeholders}) "
            f"ORDER BY {pk_column} FOR KEY SHARE", ids)
        return tuple(row[0] for row in cursor.fetchall())
