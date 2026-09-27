"""
From a layout and a date window to the fewest provider calls that answer it.

A dashboard of twelve widgets often reads three or four distinct things: the
same daily series feeds a KPI, a trend line and a table. Every provider call
costs quota and seconds, so identical calls are made once and every widget that
wanted them is pointed at the one answer.

Pure functions only -- no database, no Django, no clock. "Today" is handed in,
which is what lets the arithmetic be tested to the day.
"""

import json
from collections import namedtuple
from datetime import date, timedelta

from .layout import DATE_TOKENS

#: Inclusive ISO dates. ``prev_*`` is the period of the same length that ends the
#: day before ``start``.
Window = namedtuple('Window', 'start end prev_start prev_end')

#: One provider call: ``key`` is what the response and the widgets refer to it by.
RunSpec = namedtuple('RunSpec', 'key connection_id tool args')

_ONE_DAY = timedelta(days=1)


def _window(start, end):
    days = (end - start).days + 1
    prev_end = start - _ONE_DAY
    prev_start = prev_end - timedelta(days=days - 1)
    return Window(start.isoformat(), end.isoformat(), prev_start.isoformat(), prev_end.isoformat())


def resolve_window(date_filter, today):
    """The concrete dates behind a cleaned date filter.

    Sent to every tool as explicit dates rather than a preset, so the current
    period and the one before it come from the same arithmetic and can never be
    a day apart. Like Google's own presets, "last N days" ends YESTERDAY: today's
    numbers are partial and would drag every ratio down. The rules mirror
    ``windowFor`` in the Google Ads report's ads-model.ts.

    With no filter the window is the last 30 days. ``today`` is a UTC date, so
    around midnight a person far from UTC can be a day off; a CUSTOM range is
    the exact way to say which days are wanted.
    """
    spec = date_filter or {}
    chosen = spec.get('range', 'LAST_30_DAYS')
    yesterday = today - _ONE_DAY
    if chosen == 'CUSTOM':
        return _window(date.fromisoformat(spec['start']), date.fromisoformat(spec['end']))
    if chosen == 'THIS_MONTH':
        return _window(today.replace(day=1), today)
    if chosen == 'LAST_MONTH':
        last_of_previous = today.replace(day=1) - _ONE_DAY
        return _window(last_of_previous.replace(day=1), last_of_previous)
    days = {'LAST_7_DAYS': 7, 'LAST_14_DAYS': 14, 'LAST_90_DAYS': 90}.get(chosen, 30)
    return _window(yesterday - timedelta(days=days - 1), yesterday)


def shift_back(window):
    """The window of the previous period, with its own previous period beside it."""
    return _window(date.fromisoformat(window.prev_start), date.fromisoformat(window.prev_end))


def _tokens(window):
    return dict(zip(DATE_TOKENS, (window.start, window.end, window.prev_start, window.prev_end)))


def substitute(value, window):
    """Copy of ``value`` with every date token replaced by its date.

    Only a string that IS a token is replaced, never text that merely contains
    one, so a search phrase can say anything it likes.
    """
    return _substitute(value, _tokens(window))


def _substitute(value, tokens):
    if isinstance(value, str):
        return tokens.get(value, value)
    if isinstance(value, dict):
        return {key: _substitute(item, tokens) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, tokens) for item in value]
    return value


def uses_dates(value):
    """True when a token appears anywhere inside ``value``."""
    if isinstance(value, str):
        return value in DATE_TOKENS
    if isinstance(value, dict):
        return any(uses_dates(item) for item in value.values())
    if isinstance(value, list):
        return any(uses_dates(item) for item in value)
    return False


class Plan(object):
    """The calls to make, and which widget reads which.

    ``runs`` is a list of RunSpec in first-use order. ``widgets`` maps a widget
    id to ``{'run': key, 'prev_run': key or None}``.
    """

    def __init__(self):
        self.runs = []
        self.widgets = {}
        self._keys = {}

    def add(self, connection_id, tool, args):
        """The key of the call for these inputs, adding it if it is new.

        Two calls are the same call when the connection, the tool and the
        arguments are equal. Argument order is irrelevant, which is why the
        arguments are compared as canonical JSON: it is also what keeps 1 and
        true apart, which Python's own == would not.
        """
        fingerprint = (connection_id, tool, json.dumps(args, sort_keys=True, separators=(',', ':')))
        key = self._keys.get(fingerprint)
        if key is None:
            key = 'r{0}'.format(len(self.runs) + 1)
            self._keys[fingerprint] = key
            self.runs.append(RunSpec(key, connection_id, tool, args))
        return key


def build_plan(widgets, window, compare):
    """Plan the calls for a cleaned layout.

    A widget also gets a comparison call only when the report is comparing
    (``compare``), the widget asked for it (``options.compare``) and its
    arguments carry a date -- a call with no date would return the same answer
    twice and just spend the quota. The comparison call reads the previous period.
    """
    plan = Plan()
    previous = shift_back(window)
    for widget in widgets:
        source = widget['source']
        args = source.get('args') or {}
        run = plan.add(source['connection_id'], source['tool'], substitute(args, window))
        prev_run = None
        wants_comparison = (widget.get('options') or {}).get('compare') is True
        if compare and wants_comparison and uses_dates(args):
            prev_run = plan.add(source['connection_id'], source['tool'], substitute(args, previous))
        plan.widgets[widget['id']] = {'run': run, 'prev_run': prev_run}
    return plan
