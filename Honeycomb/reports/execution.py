"""
Run a plan: find the connections, put every distinct call through the gate, and
run them all at once.

The connection lookup below is the tenant boundary for a run. A widget stores a
connection id in JSON, so nothing about the row itself says whose connection it
is; only a lookup filtered by the caller's organization decides that. An id from
another organization, or one that no longer exists, is simply not found and
becomes a failed widget -- never a call made with someone else's credentials.
"""

from connections.models import Connection
from connections.runner import prepare_runs, run_pending
from connectors import registry

#: Provider calls in flight at once across the whole request. Thirty widgets on
#: one Google Ads account would otherwise open thirty requests to the same API in
#: the same instant and trip its rate limit -- a report that fails because it
#: was too eager.
MAX_IN_FLIGHT = 8


def _refused(spec, status, message):
    return {'tool': spec.tool, 'ok': False, 'status': status, 'error': message}


def execute_plan(tenant, plan):
    """Run every call in ``plan`` and return ``{run key: result}``.

    Each result is what ``connections.runner`` describes, plus the
    ``connection_id`` it ran on. A call that cannot be made -- unknown
    connection, connector gone, tool refused by the gate, provider error,
    timeout -- is a failed result under its own key and never stops the others.
    """
    wanted = {spec.connection_id for spec in plan.runs}
    connections = {
        connection.id: connection
        for connection in Connection.objects.filter(tenant=tenant, id__in=wanted)
    }

    by_connection = {}
    for spec in plan.runs:
        by_connection.setdefault(spec.connection_id, []).append(spec)

    results = {}
    waiting = []  # (run key, connections.runner.Pending)
    for connection_id, specs in by_connection.items():
        connection = connections.get(connection_id)
        if connection is None:
            for spec in specs:
                results[spec.key] = _refused(spec, 404, 'This connection no longer exists.')
            continue
        connector = registry.get(connection.connector)
        if connector is None:
            for spec in specs:
                results[spec.key] = _refused(spec, 400, 'This connector is no longer available.')
            continue
        gated, pending = prepare_runs(
            connection, connector, [{'tool': spec.tool, 'args': spec.args} for spec in specs])
        for spec, refusal in zip(specs, gated):
            if refusal is not None:
                results[spec.key] = refusal
        waiting.extend((specs[item.index].key, item) for item in pending)

    outcomes = run_pending([item for _, item in waiting], limit=MAX_IN_FLIGHT)
    for (key, _), outcome in zip(waiting, outcomes):
        results[key] = outcome

    return {
        spec.key: dict(results[spec.key], connection_id=spec.connection_id)
        for spec in plan.runs
    }
