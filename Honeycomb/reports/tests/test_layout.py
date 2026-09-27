"""Tests for what a report is allowed to store (reports/layout.py).

The layout is free-form JSON that the frontend owns, so this module is the only
thing between a client and an unbounded blob in a column. What has to hold is
that every rule refuses -- rather than trims or guesses -- and that a sound
layout comes out exactly as it went in, so an autosave is never rewritten
behind the editor's back.
"""
import copy
import json

from django.test import SimpleTestCase

from reports import layout as rules
from reports.layout import LayoutError, check_filters, check_layout, connection_ids, default_filters


def widget(**overrides):
    base = {
        'id': 'w1', 'type': 'kpi', 'x': 0, 'y': 0, 'w': 3, 'h': 2,
        'source': {'connection_id': 7, 'tool': 'get_daily_performance', 'args': {}},
    }
    base.update(overrides)
    return base


def nested(levels):
    """An object holding `levels` containers, the outermost being level one."""
    value = {}
    for _ in range(levels - 1):
        value = {'k': value}
    return value


class CheckLayoutTests(SimpleTestCase):
    def test_a_sound_layout_comes_back_exactly_as_it_went_in(self):
        full = widget(title='  Spend ', fields={'metric': 'cost'}, options={'compare': True})
        minimal = widget(id='w2', y=2, source={'connection_id': 7, 'tool': 'x'})
        raw = [full, minimal]
        before = copy.deepcopy(raw)
        self.assertEqual(check_layout(raw), before)
        # Nothing is written into it either: no defaults for the parts left out.
        self.assertEqual(raw, before)
        self.assertNotIn('fields', minimal)
        self.assertNotIn('options', minimal)
        self.assertNotIn('args', minimal['source'])
        self.assertNotIn('title', minimal)

    def test_a_title_may_be_blank_and_is_kept_as_written(self):
        for title in ('', '   ', '  Clicks  '):
            self.assertEqual(check_layout([widget(title=title)])[0]['title'], title)

    def test_an_empty_layout_is_fine(self):
        self.assertEqual(check_layout([]), [])

    def test_it_must_be_a_list(self):
        for bad in (None, {}, 'x', 3, {'w1': widget()}):
            with self.assertRaises(LayoutError):
                check_layout(bad)

    def test_thirty_widgets_fit_and_thirty_one_do_not(self):
        thirty = [widget(id='w{0}'.format(i), y=i) for i in range(30)]
        self.assertEqual(len(check_layout(thirty)), 30)
        with self.assertRaises(LayoutError):
            check_layout(thirty + [widget(id='w30', y=30)])

    def test_ids_must_be_unique(self):
        with self.assertRaises(LayoutError):
            check_layout([widget(), widget(y=2)])

    def test_ids_are_short_and_plain(self):
        for good in ('a', 'W-1_x', 'a' * 40):
            check_layout([widget(id=good)])
        for bad in ('', 'a' * 41, 'has space', 'ü', 'a/b', 7, None, 'a\n'):
            with self.assertRaises(LayoutError, msg=repr(bad)):
                check_layout([widget(id=bad)])

    def test_only_known_types_are_accepted(self):
        for kind in rules.WIDGET_TYPES:
            check_layout([widget(type=kind)])
        for bad in ('pie', 'KPI', '', None, 3):
            with self.assertRaises(LayoutError, msg=repr(bad)):
                check_layout([widget(type=bad)])

    def test_every_required_key_is_required(self):
        for key in ('id', 'type', 'x', 'y', 'w', 'h', 'source'):
            raw = widget()
            del raw[key]
            with self.assertRaises(LayoutError, msg=key):
                check_layout([raw])

    def test_an_unknown_key_is_refused_not_stored(self):
        with self.assertRaises(LayoutError):
            check_layout([widget(colour='red')])

    def test_a_widget_must_be_an_object(self):
        with self.assertRaises(LayoutError):
            check_layout(['w1'])

    def test_coordinates_are_whole_numbers_only(self):
        for key in ('x', 'y', 'w', 'h'):
            for bad in (1.5, 2.0, '3', True, None):
                with self.assertRaises(LayoutError, msg='{0}={1!r}'.format(key, bad)):
                    check_layout([widget(**{key: bad})])

    def test_coordinates_have_floors(self):
        for overrides in ({'x': -1}, {'y': -1}, {'w': 0}, {'h': 0}):
            with self.assertRaises(LayoutError, msg=str(overrides)):
                check_layout([widget(**overrides)])

    def test_a_widget_stays_inside_twelve_columns(self):
        check_layout([widget(x=9, w=3)])
        with self.assertRaises(LayoutError):
            check_layout([widget(x=9, w=4)])

    def test_a_widget_stays_inside_five_hundred_rows(self):
        check_layout([widget(y=498, h=2)])
        with self.assertRaises(LayoutError):
            check_layout([widget(y=498, h=3)])

    def test_overlap_is_allowed_because_a_drag_passes_through_it(self):
        self.assertEqual(len(check_layout([widget(id='a'), widget(id='b')])), 2)

    def test_a_title_is_bounded_text(self):
        check_layout([widget(title='t' * 120)])
        for bad in ('t' * 121, 5, ['x']):
            with self.assertRaises(LayoutError):
                check_layout([widget(title=bad)])

    def test_source_shape(self):
        bad_sources = [
            None, [], 'x', {},
            {'tool': 'x'},
            {'connection_id': 7},
            {'connection_id': 0, 'tool': 'x'},
            {'connection_id': -3, 'tool': 'x'},
            {'connection_id': True, 'tool': 'x'},
            {'connection_id': '7', 'tool': 'x'},
            {'connection_id': 7.0, 'tool': 'x'},
            {'connection_id': 7, 'tool': ''},
            {'connection_id': 7, 'tool': 'has space'},
            {'connection_id': 7, 'tool': 'a-b'},
            {'connection_id': 7, 'tool': 'x' * 65},
            {'connection_id': 7, 'tool': 5},
            {'connection_id': 7, 'tool': 'x', 'args': []},
            {'connection_id': 7, 'tool': 'x', 'args': None},
            {'connection_id': 7, 'tool': 'x', 'extra': 1},
        ]
        for source in bad_sources:
            with self.assertRaises(LayoutError, msg=repr(source)):
                check_layout([widget(source=source)])
        check_layout([widget(source={'connection_id': 7, 'tool': 'x' * 64})])

    def test_connection_id_has_an_upper_bound(self):
        # A connection id is the one number that reaches a DB query rather than
        # only arithmetic; an unbounded one crashes the ORM lookup with an
        # OverflowError instead of failing validation cleanly (see execution.py
        # and serializers.py, both filtering Connection by id__in=).
        check_layout([widget(source={'connection_id': rules.MAX_CONNECTION_ID, 'tool': 'x'})])
        with self.assertRaises(LayoutError):
            check_layout([widget(source={'connection_id': rules.MAX_CONNECTION_ID + 1, 'tool': 'x'})])
        with self.assertRaises(LayoutError):
            check_layout([widget(source={'connection_id': 10 ** 30, 'tool': 'x'})])

    def test_control_characters_are_refused_in_title_and_args(self):
        for bad in ('bad\x00title', 'bad\x01title', 'bad\x1ftitle'):
            with self.assertRaises(LayoutError, msg=repr(bad)):
                check_layout([widget(title=bad)])
        for bad in ('a\x00b', 'a\x0bb'):
            with self.assertRaises(LayoutError, msg=repr(bad)):
                check_layout([widget(source={'connection_id': 7, 'tool': 'x', 'args': {'q': bad}})])
            with self.assertRaises(LayoutError, msg=repr(bad)):
                check_layout([widget(fields={'q': bad})])
        # Tab, newline and carriage return are ordinary whitespace, not refused.
        check_layout([widget(title='line one\nline two\ttabbed\r')])

    def test_args_size_is_capped(self):
        def args_of_size(target):
            # Several strings, each under the 2000-character string cap, so it
            # is the args cap being tested and not that one.
            args = {'a': 'x' * 2000, 'b': 'x' * 2000, 'c': ''}
            args['c'] = 'x' * (target - len(json.dumps(args, separators=(',', ':'))))
            self.assertEqual(len(json.dumps(args, separators=(',', ':'))), target)
            return args

        def source(args):
            return {'connection_id': 7, 'tool': 'x', 'args': args}

        check_layout([widget(source=source(args_of_size(rules.MAX_ARGS_BYTES)))])
        with self.assertRaises(LayoutError):
            check_layout([widget(source=source(args_of_size(rules.MAX_ARGS_BYTES + 1)))])

    def test_opaque_objects_must_be_objects(self):
        for key in ('fields', 'options'):
            for bad in ([], 'x', None, 3):
                with self.assertRaises(LayoutError, msg='{0}={1!r}'.format(key, bad)):
                    check_layout([widget(**{key: bad})])

    def test_nesting_is_bounded_in_fields_options_and_args(self):
        check_layout([widget(fields=nested(6), options=nested(6))])
        with self.assertRaises(LayoutError):
            check_layout([widget(fields=nested(7))])
        with self.assertRaises(LayoutError):
            check_layout([widget(options=nested(7))])
        deep_args = nested(7)
        with self.assertRaises(LayoutError):
            check_layout([widget(source={'connection_id': 7, 'tool': 'x', 'args': deep_args})])

    def test_lists_count_as_a_level(self):
        deep = {'a': [[[[[1]]]]]}  # dict + 5 lists = 6 levels
        check_layout([widget(fields=deep)])
        with self.assertRaises(LayoutError):
            check_layout([widget(fields={'a': [[[[[[1]]]]]]})])

    def test_strings_lists_and_keys_are_bounded(self):
        check_layout([widget(fields={'a': 's' * 2000, 'b': list(range(200)), 'k' * 64: 1})])
        for bad in ({'a': 's' * 2001}, {'b': list(range(201))}, {'k' * 65: 1}):
            with self.assertRaises(LayoutError, msg=repr(list(bad)[0])):
                check_layout([widget(fields=bad)])

    def test_only_json_values_are_accepted(self):
        for bad in ({'a': {1, 2}}, {'a': object()}, {'a': float('nan')}, {'a': float('inf')}, {1: 'x'}):
            with self.assertRaises(LayoutError, msg=repr(bad)):
                check_layout([widget(fields=bad)])

    def test_date_tokens_are_checked_on_values_at_any_depth(self):
        for token in rules.DATE_TOKENS:
            args = {'a': [{'b': token}], 'c': token}
            check_layout([widget(source={'connection_id': 7, 'tool': 'x', 'args': args})])
        for bad in ('$date.bogus', '$date.', '$date.START', '$date.start '):
            for args in ({'a': bad}, {'a': [bad]}, {'a': {'b': [{'c': bad}]}}):
                with self.assertRaises(LayoutError, msg=repr(args)):
                    check_layout([widget(source={'connection_id': 7, 'tool': 'x', 'args': args})])

    def test_text_that_only_contains_a_token_is_left_alone(self):
        args = {'q': 'cost since $date.start', '$date.weird': 1}
        check_layout([widget(source={'connection_id': 7, 'tool': 'x', 'args': args})])

    def test_the_whole_layout_is_capped_in_size(self):
        # Each widget is under every per-widget limit; together they are not.
        chunk = {'a': 'x' * 2000, 'b': 'x' * 2000, 'c': 'x' * 2000, 'd': 'x' * 2000}
        many = [widget(id='w{0}'.format(i), y=i, fields=chunk) for i in range(20)]
        with self.assertRaises(LayoutError):
            check_layout(many)

    def test_connection_ids_lists_every_source_once(self):
        layout = check_layout([
            widget(id='a', source={'connection_id': 7, 'tool': 'x'}),
            widget(id='b', source={'connection_id': 7, 'tool': 'y'}),
            widget(id='c', source={'connection_id': 9, 'tool': 'x'}),
        ])
        self.assertEqual(connection_ids(layout), {7, 9})
        self.assertEqual(connection_ids([]), set())

    def test_messages_name_the_widget_so_they_can_be_shown_as_they_are(self):
        with self.assertRaises(LayoutError) as caught:
            check_layout([widget(id='ok'), widget(id='b', x=11, w=4)])
        self.assertIn('Widget 2', str(caught.exception))


class CheckFiltersTests(SimpleTestCase):
    def test_every_preset_is_accepted(self):
        for name in rules.DATE_RANGES:
            if name == 'CUSTOM':
                continue
            self.assertEqual(check_filters({'date': {'range': name}}), {'date': {'range': name}})

    def test_custom_needs_real_ordered_dates(self):
        ok = {'date': {'range': 'CUSTOM', 'start': '2026-09-01', 'end': '2026-09-30'}}
        self.assertEqual(check_filters(ok), ok)
        one_day = {'date': {'range': 'CUSTOM', 'start': '2026-09-01', 'end': '2026-09-01'}}
        self.assertEqual(check_filters(one_day), one_day)
        bad = [
            {'range': 'CUSTOM'},
            {'range': 'CUSTOM', 'start': '2026-09-01'},
            {'range': 'CUSTOM', 'end': '2026-09-01'},
            {'range': 'CUSTOM', 'start': '2026-09-30', 'end': '2026-09-01'},
            {'range': 'CUSTOM', 'start': '2026-9-1', 'end': '2026-09-30'},
            {'range': 'CUSTOM', 'start': '2026/09/01', 'end': '2026-09-30'},
            {'range': 'CUSTOM', 'start': '20260901', 'end': '2026-09-30'},
            {'range': 'CUSTOM', 'start': '2026-02-30', 'end': '2026-09-30'},
            {'range': 'CUSTOM', 'start': 20260901, 'end': '2026-09-30'},
            {'range': 'CUSTOM', 'start': None, 'end': '2026-09-30'},
        ]
        for spec in bad:
            with self.assertRaises(LayoutError, msg=repr(spec)):
                check_filters({'date': spec})

    def test_a_custom_range_covers_at_most_731_days_counting_both_ends(self):
        # 2027-01-01 .. 2028-12-31 is 366 + 365 = 731 days inclusive.
        check_filters({'date': {'range': 'CUSTOM', 'start': '2027-01-01', 'end': '2028-12-31'}})
        with self.assertRaises(LayoutError):
            check_filters({'date': {'range': 'CUSTOM', 'start': '2027-01-01', 'end': '2029-01-01'}})

    def test_start_and_end_only_go_with_custom(self):
        with self.assertRaises(LayoutError):
            check_filters({'date': {'range': 'LAST_7_DAYS', 'start': '2026-09-01'}})
        with self.assertRaises(LayoutError):
            check_filters({'date': {'range': 'LAST_7_DAYS', 'end': '2026-09-01'}})

    def test_unknown_ranges_keys_and_shapes_are_refused(self):
        for bad in (
            {'date': {'range': 'YESTERDAY'}}, {'date': {'range': 'last_7_days'}},
            {'date': {}}, {'date': []}, {'date': 'LAST_7_DAYS'}, {'date': None},
            {'date': {'range': 'LAST_7_DAYS', 'tz': 'UTC'}},
            {'account': '1'}, {'compare': 'yes'}, {'compare': 1}, {'compare': None},
            [], 'x', None, 3,
        ):
            with self.assertRaises(LayoutError, msg=repr(bad)):
                check_filters(bad)

    def test_compare_is_a_boolean(self):
        self.assertEqual(check_filters({'compare': True}), {'compare': True})
        self.assertEqual(check_filters({'compare': False}), {'compare': False})

    def test_empty_filters_are_valid_and_stay_empty(self):
        self.assertEqual(check_filters({}), {})

    def test_the_default_is_a_fresh_object_every_time(self):
        first = default_filters()
        first['date']['range'] = 'LAST_7_DAYS'
        self.assertEqual(default_filters(), {'date': {'range': 'LAST_30_DAYS'}, 'compare': False})
