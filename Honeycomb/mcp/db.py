"""Database access for the MCP data plane, off the one shared ORM thread.

Why this exists: FastAPI requests never pass through Django's handler, so they
get no ThreadSensitiveContext. Every ``afirst()``/``aupdate()`` and every
default ``sync_to_async`` call therefore lands on asgiref's process-wide
single-thread executor. All MCP database work in a worker queued behind one
thread -- measured locally at about 100 authenticated requests a second for
two workers, however many clients were waiting.

``run_db`` runs the work with ``thread_sensitive=False`` instead, on the event
loop's default thread pool, so independent requests query in parallel. Each
pool thread keeps its own Django connection, which raises the second problem
this file handles: nothing ever closes those connections, because Django only
recycles a connection on the ``request_started``/``request_finished`` signals
these requests never send. So before each unit of work:

  * a connection that has seen a database error is health-checked and dropped
    if it is dead, and a statement that fails on a connection the server
    dropped (a Postgres restart) is retried once on a fresh one -- before
    this, every MCP call failed until the container itself restarted;
  * a connection older than ``RECYCLE_SECONDS`` is closed and reopened, so
    server-side state and pooler limits cannot pile up forever.
"""
import time

from asgiref.sync import sync_to_async
from django.db import InterfaceError, OperationalError, connection

RECYCLE_SECONDS = 300

_OPENED = '_hc_opened_at'


def _prepare():
    if connection.connection is None:
        setattr(connection, _OPENED, None)
        return
    opened = getattr(connection, _OPENED, None)
    if opened is not None and time.monotonic() - opened > RECYCLE_SECONDS:
        connection.close()
        setattr(connection, _OPENED, None)
        return
    if connection.errors_occurred:
        if connection.is_usable():
            connection.errors_occurred = False
        else:
            connection.close()
            setattr(connection, _OPENED, None)


def _run(fn, args, kwargs):
    _prepare()
    try:
        try:
            return fn(*args, **kwargs)
        except (InterfaceError, OperationalError):
            # The usual cause is a connection the server dropped (a Postgres
            # restart or failover) that this thread was still holding. If so
            # the statement never ran, so reconnecting and running it once more
            # is safe -- and saves the caller a 500 for our stale socket.
            if connection.connection is None or connection.is_usable():
                raise
            connection.close()
            setattr(connection, _OPENED, None)
            return fn(*args, **kwargs)
    finally:
        if connection.connection is not None and getattr(connection, _OPENED, None) is None:
            setattr(connection, _OPENED, time.monotonic())


async def run_db(fn, *args, **kwargs):
    """Run the synchronous ``fn(*args, **kwargs)`` on the thread pool."""
    return await sync_to_async(_run, thread_sensitive=False)(fn, args, kwargs)
