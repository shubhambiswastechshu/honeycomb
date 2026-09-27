"""
The shape of a saved report, and the only place that decides what may be stored.

A report's ``layout`` and ``filters`` are JSON because a widget's ``fields`` and
``options`` are the frontend's business and will keep changing. Free-form JSON is
also where an unbounded blob or a surprising type gets in, so everything that is
stored passes through here first: a closed set of keys, whole-number coordinates
on a 12-column grid, and hard limits on size and nesting.

The checks refuse or accept; they never rewrite. An editor autosaves its own copy
and compares it with what came back, so a server that quietly trimmed a title or
filled in defaults would make every save look like a change the person did not
make. What is stored is exactly what was sent.

Nothing in this module touches the database or Django. The one check that needs
a tenant -- that a widget's connection belongs to the caller's organization --
is the serializer's job; ``connection_ids`` hands it what to look up.
"""

import json
import math
import re
from datetime import date

#: Bumped when the widget format changes in a way an old row cannot be read as.
#: Stored on the row so a later release can migrate old reports instead of
#: guessing which format they were saved in.
LAYOUT_SCHEMA_VERSION = 1

GRID_COLUMNS = 12
MAX_WIDGETS = 30
#: Tall enough for any real dashboard; short enough that a stray y cannot make
#: the canvas a million rows long.
MAX_ROWS = 500

WIDGET_TYPES = (
    'kpi', 'bar', 'column', 'line', 'area', 'stacked_bar', 'donut', 'table', 'calendar',
)

#: A string value in a widget's args that is exactly one of these is replaced by
#: a date from the report's date filter when the report runs (see runplan.py).
DATE_TOKENS = ('$date.start', '$date.end', '$date.prev_start', '$date.prev_end')

#: Same ids as RangeId in the Google Ads report's ads-model.ts.
DATE_RANGES = (
    'LAST_7_DAYS', 'LAST_14_DAYS', 'LAST_30_DAYS', 'LAST_90_DAYS',
    'THIS_MONTH', 'LAST_MONTH', 'CUSTOM',
)
MAX_CUSTOM_DAYS = 731

MAX_LAYOUT_BYTES = 128 * 1024
MAX_ARGS_BYTES = 4096
MAX_TITLE = 120
MAX_JSON_DEPTH = 6
MAX_JSON_STRING = 2000
MAX_JSON_ITEMS = 200
MAX_JSON_KEY = 64
#: A connection id is the one layout number that reaches a database query
#: (Connection.objects.filter(id__in=...)) rather than only arithmetic. Bounded
#: to what a bigint column can hold so an absurd value is a 400 here rather than
#: an OverflowError out of the DB driver -- SQLite raises on a Python int wider
#: than 64 bits; PostgreSQL raises on one wider than its bigint id column.
MAX_CONNECTION_ID = 2 ** 63 - 1

_WIDGET_KEYS = ('id', 'type', 'x', 'y', 'w', 'h', 'title', 'source', 'fields', 'options')
_REQUIRED_KEYS = ('id', 'type', 'x', 'y', 'w', 'h', 'source')
_SOURCE_KEYS = ('connection_id', 'tool', 'args')
_FILTER_KEYS = ('date', 'compare')

_ID = re.compile(r'[A-Za-z0-9_-]{1,40}')
_TOOL = re.compile(r'[A-Za-z0-9_]{1,64}')
_ISO_DAY = re.compile(r'\d{4}-\d{2}-\d{2}')
#: C0 control characters other than tab/newline/CR. PostgreSQL's text and jsonb
#: storage cannot hold a NUL byte at all (psycopg raises adapting it, or the
#: server rejects it), and the rest have no legitimate place in a title or a
#: tool argument, so every free-text value is refused rather than silently
#: passed through to a database that will refuse it later.
_CONTROL_CHARS = re.compile(u'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def has_control_chars(value):
    """True when ``value`` holds a C0 control character other than tab/newline/CR.

    Shared with the serializer for the report's own text fields (``name``,
    ``description``, ``client``), which are plain model fields rather than
    layout JSON and so never pass through ``_check_json``.
    """
    return bool(_CONTROL_CHARS.search(value))


class LayoutError(ValueError):
    """A layout or filter value the server refuses to store.

    The message is written for the person editing the report, so it names the
    widget and the rule and can be shown as it is.
    """


def default_filters():
    """What a new report starts with. Module-level: Django serialises a field
    default into the migration by import path, so a lambda cannot be used."""
    return {'date': {'range': 'LAST_30_DAYS'}, 'compare': False}


def _dump(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False)


def _size(value):
    return len(_dump(value).encode('utf-8'))


def _check_json(value, where, depth=1):
    """Refuse anything that is not plain, bounded JSON.

    ``depth`` counts containers, and the object handed in is level one, so a
    value that is only scalars is level one however long it is.
    """
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LayoutError('{0} holds a number that is not finite.'.format(where))
        return
    if isinstance(value, str):
        if len(value) > MAX_JSON_STRING:
            raise LayoutError('{0} holds a text longer than {1} characters.'.format(
                where, MAX_JSON_STRING))
        if has_control_chars(value):
            raise LayoutError('{0} holds a control character, which cannot be stored.'.format(where))
        return
    if isinstance(value, (list, dict)):
        if depth > MAX_JSON_DEPTH:
            raise LayoutError('{0} is nested more than {1} levels deep.'.format(
                where, MAX_JSON_DEPTH))
        if isinstance(value, list):
            if len(value) > MAX_JSON_ITEMS:
                raise LayoutError('{0} holds a list of more than {1} items.'.format(
                    where, MAX_JSON_ITEMS))
            for item in value:
                _check_json(item, where, depth + 1)
            return
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > MAX_JSON_KEY:
                raise LayoutError('{0} has a name that is not text of at most {1} characters.'.format(
                    where, MAX_JSON_KEY))
            _check_json(item, where, depth + 1)
        return
    raise LayoutError('{0} holds a value that is not JSON.'.format(where))


def _check_tokens(value, where):
    """Reject a ``$date.`` string that is not one of the four real tokens.

    Only values are checked: keys are never substituted. A typo here would
    otherwise reach the provider as a literal string and come back as a
    confusing error from someone else's API.
    """
    if isinstance(value, str):
        if value.startswith('$date.') and value not in DATE_TOKENS:
            raise LayoutError('{0} uses "{1}", which is not a date placeholder (use {2}).'.format(
                where, value, ', '.join(DATE_TOKENS)))
    elif isinstance(value, list):
        for item in value:
            _check_tokens(item, where)
    elif isinstance(value, dict):
        for item in value.values():
            _check_tokens(item, where)


def _whole(value, where, minimum, maximum=None):
    # ``type is int`` and not isinstance: True is an int in Python, and a JSON
    # true in a coordinate is a bug on the sender's side, not a 1.
    if type(value) is not int:
        raise LayoutError('{0} must be a whole number.'.format(where))
    if value < minimum:
        raise LayoutError('{0} must be at least {1}.'.format(where, minimum))
    if maximum is not None and value > maximum:
        raise LayoutError('{0} must be at most {1}.'.format(where, maximum))
    return value


def _object(value, where):
    if not isinstance(value, dict):
        raise LayoutError('{0} must be an object.'.format(where))
    return value


def _closed(value, allowed, where):
    """Every key must be one we know: an unknown one is a typo, or a client that
    is newer than this server, and either way it must not be stored silently."""
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise LayoutError('{0} has an unknown setting "{1}".'.format(where, unknown[0]))


def _check_source(raw, where):
    source = _object(raw, where + ' source')
    _closed(source, _SOURCE_KEYS, where + ' source')
    for key in ('connection_id', 'tool'):
        if key not in source:
            raise LayoutError('{0} source is missing "{1}".'.format(where, key))
    _whole(source['connection_id'], where + ' connection', 1, MAX_CONNECTION_ID)
    tool = source['tool']
    if not isinstance(tool, str) or not _TOOL.fullmatch(tool):
        raise LayoutError('{0} names a tool that is not valid (letters, digits and _, up to 64).'.format(
            where))
    args = _object(source.get('args', {}), where + ' args')
    _check_json(args, where + ' args')
    if _size(args) > MAX_ARGS_BYTES:
        raise LayoutError('{0} args are larger than {1} bytes.'.format(where, MAX_ARGS_BYTES))
    _check_tokens(args, where)


def _check_widget(raw, index):
    where = 'Widget {0}'.format(index + 1)
    widget = _object(raw, where)
    _closed(widget, _WIDGET_KEYS, where)
    for key in _REQUIRED_KEYS:
        if key not in widget:
            raise LayoutError('{0} is missing "{1}".'.format(where, key))

    ident = widget['id']
    if not isinstance(ident, str) or not _ID.fullmatch(ident):
        raise LayoutError('{0} has an id that is not valid (letters, digits, - and _, up to 40).'.format(
            where))
    if widget['type'] not in WIDGET_TYPES:
        raise LayoutError('{0} has a type that is not supported.'.format(where))

    x = _whole(widget['x'], where + ' x', 0)
    y = _whole(widget['y'], where + ' y', 0)
    w = _whole(widget['w'], where + ' width', 1)
    h = _whole(widget['h'], where + ' height', 1)
    if x + w > GRID_COLUMNS:
        raise LayoutError('{0} reaches column {1}, but the grid has {2} columns.'.format(
            where, x + w, GRID_COLUMNS))
    if y + h > MAX_ROWS:
        raise LayoutError('{0} reaches row {1}, but a report has at most {2} rows.'.format(
            where, y + h, MAX_ROWS))

    if 'title' in widget:
        title = widget['title']
        if not isinstance(title, str):
            raise LayoutError('{0} title must be text.'.format(where))
        if len(title) > MAX_TITLE:
            raise LayoutError('{0} title is longer than {1} characters.'.format(where, MAX_TITLE))
        if has_control_chars(title):
            raise LayoutError('{0} title holds a control character, which cannot be stored.'.format(where))
    _check_source(widget['source'], where)
    for key in ('fields', 'options'):
        value = _object(widget.get(key, {}), '{0} {1}'.format(where, key))
        _check_json(value, '{0} {1}'.format(where, key))


def check_layout(raw):
    """Return ``raw`` if it may be stored, or raise LayoutError.

    Overlapping widgets are deliberately allowed: a drag in progress passes
    through overlaps, autosave must not fail on the way, and the canvas
    resolves them on screen.
    """
    if not isinstance(raw, list):
        raise LayoutError('The layout must be a list of widgets.')
    if len(raw) > MAX_WIDGETS:
        raise LayoutError('A report can hold at most {0} widgets.'.format(MAX_WIDGETS))
    seen = set()
    for index, widget in enumerate(raw):
        _check_widget(widget, index)
        if widget['id'] in seen:
            raise LayoutError('Widget {0} reuses the id "{1}".'.format(index + 1, widget['id']))
        seen.add(widget['id'])
    if _size(raw) > MAX_LAYOUT_BYTES:
        raise LayoutError('The layout is larger than {0} KB.'.format(MAX_LAYOUT_BYTES // 1024))
    return raw


def connection_ids(layout):
    """Every connection id a checked layout reads from."""
    return {widget['source']['connection_id'] for widget in layout}


def _iso_day(value, where):
    if not isinstance(value, str) or not _ISO_DAY.fullmatch(value):
        raise LayoutError('{0} must be a date written YYYY-MM-DD.'.format(where))
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise LayoutError('{0} is not a real date.'.format(where))


def _check_date(raw):
    spec = _object(raw, 'The date filter')
    _closed(spec, ('range', 'start', 'end'), 'The date filter')
    chosen = spec.get('range')
    if not isinstance(chosen, str) or chosen not in DATE_RANGES:
        raise LayoutError('The date filter needs a range: {0}.'.format(', '.join(DATE_RANGES)))
    if chosen != 'CUSTOM':
        if 'start' in spec or 'end' in spec:
            raise LayoutError('A start and end only go with the CUSTOM range.')
        return
    start = _iso_day(spec.get('start'), 'The start date')
    end = _iso_day(spec.get('end'), 'The end date')
    if start > end:
        raise LayoutError('The start date is after the end date.')
    if (end - start).days + 1 > MAX_CUSTOM_DAYS:
        raise LayoutError('A custom range can cover at most {0} days.'.format(MAX_CUSTOM_DAYS))


def check_filters(raw):
    """Return the report-wide filters if they may be stored, or raise LayoutError."""
    filters = _object(raw, 'The filters')
    _closed(filters, _FILTER_KEYS, 'The filters')
    if 'date' in filters:
        _check_date(filters['date'])
    if 'compare' in filters and not isinstance(filters['compare'], bool):
        raise LayoutError('The compare setting must be true or false.')
    return filters
