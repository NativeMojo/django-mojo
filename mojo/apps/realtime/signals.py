"""
Realtime signals.

``realtime_connection_changed`` is sent by the websocket handler once per
authenticated socket when it connects and again when it disconnects (#6562):

    realtime_connection_changed.send(
        sender=<identity model class>, user=<identity instance>,
        connected=True | False, connection_id="<uuid>")

It fires after the Redis presence set (``realtime:online:<type>:<id>``) has
been updated, so ``realtime.is_online()`` already reflects the change. It is
sent per socket, not per identity: a second tab sends its own ``connected``
event, and a receiver that wants online/offline edges decides them from the
presence set. ``sender`` is the identity's model class — connect with
``sender=User`` to hear only account users.

Receivers run on an executor thread (never the event loop), may use the ORM,
and must be cheap: they sit inside every socket's auth and teardown. An
exception in one is logged and never reaches the socket.
"""

from django.dispatch import Signal

realtime_connection_changed = Signal()
