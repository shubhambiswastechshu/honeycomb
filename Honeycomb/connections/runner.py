"""
Run a batch of read tools for a page, safely.

Two routes turn one browser request into several provider calls: the
connection's own ``report`` route and the saved-report run route in the reports
app. They have to be exactly as careful as each other -- read-only, switched-off
tools respected, errors redacted, one failing tool costing one section and not
the page -- so the gate and the shape of every outcome live here once, instead
of in two copies that drift apart.

Nothing in this module writes an McpActivity row. Those rows are the record of
tool calls made through the MCP plane, and they feed the Overview's counts and
the live panel; a person opening a report is not an AI client calling a tool,
and a dozen rows per page view would bury the real ones. Failures come back
inline instead.
"""

import asyncio
import logging
import time
from collections import namedtuple

from .serializers import ToolRunSerializer

logger = logging.getLogger(__name__)

#: One run that passed the gate and is ready to be awaited. ``index`` is its slot
#: in the results list ``prepare_runs`` returned.
Pending = namedtuple('Pending', 'index connection name handler args')


def prepare_runs(connection, connector, raw_runs):
    """Put every run through the same gate as ``ConnectionViewSet.run``.

    Returns ``(results, pending)``. ``results`` lines up with ``raw_runs`` and
    already holds the refusals; the slots of runs that may go ahead stay None
    until ``run_pending`` fills them. A misspelled, write or switched-off tool
    is refused here one run at a time, so it costs its own section of the page
    and not the whole page.
    """
    handlers = getattr(connector, 'handlers', None) or {}
    results = [None] * len(raw_runs)
    pending = []
    for index, raw in enumerate(raw_runs):
        serializer = ToolRunSerializer(
            data=raw, context={'connection': connection, 'connector': connector}
        )
        name = str(raw.get('tool', ''))[:64]
        if not serializer.is_valid():
            problems = serializer.errors.get('tool') or serializer.errors.get('args') or []
            message = str(problems[0]) if problems else 'This report could not be run.'
            results[index] = {'tool': name, 'ok': False, 'error': message, 'status': 400}
            continue
        name = serializer.validated_data['tool']
        handler = handlers.get(name)
        if handler is None:
            results[index] = {'tool': name, 'ok': False, 'status': 400,
                              'error': "Tool '{0}' has no handler.".format(name)}
            continue
        pending.append(Pending(index, connection, name, handler, serializer.validated_data['args']))
    return results, pending


async def execute(item, semaphore=None):
    """Await one pending run and describe how it went. Never raises.

    Errors are redacted with the same helpers the MCP plane uses: some providers
    carry the access token in a query parameter, and an upstream error string
    echoed verbatim would put a live credential on the page.
    """
    from connectors.shims.errors import ConnectorError, redact_exc, redact_text
    from mcp.endpoint import _tool_timeout

    async def attempt():
        # The clock starts once the call may begin. Time spent queued behind the
        # concurrency cap is ours, not the provider's, and must not be counted
        # against its timeout.
        begun = time.monotonic()

        def ms():
            return int((time.monotonic() - begun) * 1000)

        try:
            # The second argument is the ported handlers' `db` session, which
            # none of them dereference -- they read what they need off the
            # connection. None is correct, not a placeholder.
            data = await asyncio.wait_for(
                item.handler(item.connection, None, item.args), timeout=_tool_timeout())
            return {'tool': item.name, 'ok': True, 'duration_ms': ms(), 'data': data}
        except asyncio.TimeoutError:
            return {'tool': item.name, 'ok': False, 'status': 504, 'duration_ms': ms(),
                    'error': "'{0}' took longer than {1:.0f}s.".format(item.name, _tool_timeout())}
        except ConnectorError as exc:
            return {'tool': item.name, 'ok': False, 'status': 502, 'duration_ms': ms(),
                    'error': redact_text(str(exc))}
        except Exception as exc:  # noqa: BLE001 -- never a raw 500 with a token in it
            logger.exception('Portal report %s.%s failed', item.connection.connector, item.name)
            return {'tool': item.name, 'ok': False, 'status': 502, 'duration_ms': ms(),
                    'error': redact_exc(exc)}

    if semaphore is None:
        return await attempt()
    async with semaphore:
        return await attempt()


def run_pending(pending, limit=None):
    """Run every pending call concurrently, at most ``limit`` at a time.

    Returns one outcome per item, in order. The semaphore is built inside the
    coroutine on purpose: before Python 3.10 it binds to whichever event loop is
    current when it is constructed, and the loop ``async_to_sync`` runs this on
    does not exist until then.
    """
    from asgiref.sync import async_to_sync

    if not pending:
        return []

    async def run_all():
        semaphore = asyncio.Semaphore(limit) if limit else None
        return await asyncio.gather(*(execute(item, semaphore) for item in pending))

    return async_to_sync(run_all)()
