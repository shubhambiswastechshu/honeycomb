"""Tests for the date and dedupe arithmetic behind a report run (reports/runplan.py).

Two things must hold. The windows must be right to the day: every tool is sent
explicit dates, so an off-by-one is a wrong number that looks exactly like a
right one. And identical calls must collapse to one, or a dashboard spends its
provider quota several times over on the same answer.
"""
from datetime import date

from django.test import SimpleTestCase

from reports.runplan import Window, build_plan, resolve_window, shift_back, substitute, uses_dates

TODAY = date(2026, 9, 27)


def w(range_, today=TODAY, **extra):
    return resolve_window(dict(range=range_, **extra), today)


def widget(wid, tool='get_daily_performance', args=None, connection=7, compare=False):
    return {
        'id': wid, 'type': 'kpi', 'x': 0, 'y': 0, 'w': 3, 'h': 2,
        'source': {'connection_id': connection, 'tool': tool, 'args': args if args is not None else {}},
        'fields': {}, 'options': {'compare': compare},
    }


class WindowTests(SimpleTestCase):
    def test_last_n_days_end_yesterday_and_span_n_days(self):
        self.assertEqual(w('LAST_7_DAYS'), Window('2026-09-20', '2026-09-26', '2026-09-13', '2026-09-19'))
        self.assertEqual(w('LAST_30_DAYS'), Window('2026-08-28', '2026-09-26', '2026-07-29', '2026-08-27'))
        self.assertEqual(w('LAST_90_DAYS').start, '2026-06-29')
        self.assertEqual(w('LAST_14_DAYS').start, '2026-09-13')

    def test_no_filter_means_the_last_thirty_days(self):
        self.assertEqual(resolve_window(None, TODAY), w('LAST_30_DAYS'))
        self.assertEqual(resolve_window({}, TODAY), w('LAST_30_DAYS'))

    def test_this_month_runs_from_the_first_to_today(self):
        self.assertEqual(w('THIS_MONTH'), Window('2026-09-01', '2026-09-27', '2026-08-05', '2026-08-31'))

    def test_last_month_is_the_whole_previous_month(self):
        self.assertEqual(w('LAST_MONTH'), Window('2026-08-01', '2026-08-31', '2026-07-01', '2026-07-31'))

    def test_on_the_first_of_a_month(self):
        first = date(2026, 10, 1)
        self.assertEqual(w('THIS_MONTH', first), Window('2026-10-01', '2026-10-01', '2026-09-30', '2026-09-30'))
        self.assertEqual(w('LAST_MONTH', first), Window('2026-09-01', '2026-09-30', '2026-08-02', '2026-08-31'))
        self.assertEqual(w('LAST_7_DAYS', first).end, '2026-09-30')

    def test_across_a_year_boundary(self):
        january = date(2027, 1, 5)
        self.assertEqual(w('LAST_7_DAYS', january).start, '2026-12-29')
        self.assertEqual(w('LAST_7_DAYS', january).end, '2027-01-04')
        self.assertEqual(w('LAST_MONTH', january).start, '2026-12-01')
        self.assertEqual(w('LAST_MONTH', january).end, '2026-12-31')

    def test_a_leap_february(self):
        leap = date(2028, 3, 10)
        self.assertEqual(w('LAST_MONTH', leap), Window('2028-02-01', '2028-02-29', '2028-01-03', '2028-01-31'))
        common = date(2027, 3, 10)
        self.assertEqual(w('LAST_MONTH', common).end, '2027-02-28')

    def test_custom_is_used_exactly_as_given(self):
        window = w('CUSTOM', start='2026-09-01', end='2026-09-10')
        self.assertEqual(window, Window('2026-09-01', '2026-09-10', '2026-08-22', '2026-08-31'))

    def test_a_custom_range_is_not_clamped_to_today(self):
        window = w('CUSTOM', start='2026-09-20', end='2026-10-05')
        self.assertEqual(window.end, '2026-10-05')

    def test_the_previous_period_always_has_the_same_length(self):
        for name in ('LAST_7_DAYS', 'LAST_14_DAYS', 'LAST_30_DAYS', 'LAST_90_DAYS', 'THIS_MONTH', 'LAST_MONTH'):
            window = w(name)
            length = (date.fromisoformat(window.end) - date.fromisoformat(window.start)).days
            prev = (date.fromisoformat(window.prev_end) - date.fromisoformat(window.prev_start)).days
            self.assertEqual(length, prev, name)
            self.assertEqual((date.fromisoformat(window.start) - date.fromisoformat(window.prev_end)).days, 1, name)

    def test_shifting_back_makes_the_previous_period_the_current_one(self):
        earlier = shift_back(w('LAST_30_DAYS'))
        self.assertEqual(earlier, Window('2026-07-29', '2026-08-27', '2026-06-29', '2026-07-28'))


class SubstituteTests(SimpleTestCase):
    window = Window('2026-08-28', '2026-09-26', '2026-07-29', '2026-08-27')

    def test_all_four_tokens_are_replaced_at_any_depth(self):
        args = {
            'customer_id': '123',
            'start_date': '$date.start',
            'end_date': '$date.end',
            'compare': [{'from': '$date.prev_start', 'to': '$date.prev_end'}],
            'limit': 50,
        }
        self.assertEqual(substitute(args, self.window), {
            'customer_id': '123',
            'start_date': '2026-08-28',
            'end_date': '2026-09-26',
            'compare': [{'from': '2026-07-29', 'to': '2026-08-27'}],
            'limit': 50,
        })

    def test_only_a_string_that_is_a_token_is_replaced(self):
        args = {'q': 'since $date.start', 'r': '$date.startx', 's': ' $date.start'}
        self.assertEqual(substitute(args, self.window), args)

    def test_the_input_is_not_mutated(self):
        args = {'a': ['$date.start']}
        substitute(args, self.window)
        self.assertEqual(args, {'a': ['$date.start']})

    def test_non_strings_pass_through(self):
        args = {'a': 1, 'b': True, 'c': None, 'd': 1.5}
        self.assertEqual(substitute(args, self.window), args)

    def test_uses_dates_sees_a_token_anywhere_and_only_a_real_one(self):
        self.assertTrue(uses_dates({'a': [{'b': '$date.end'}]}))
        self.assertFalse(uses_dates({'a': 'text $date.end', 'b': 3, 'c': ['x']}))
        self.assertFalse(uses_dates({}))


class PlanTests(SimpleTestCase):
    window = w('LAST_30_DAYS')

    def test_identical_calls_are_made_once(self):
        plan = build_plan([widget('a'), widget('b'), widget('c')], self.window, compare=False)
        self.assertEqual(len(plan.runs), 1)
        self.assertEqual({v['run'] for v in plan.widgets.values()}, {'r1'})
        self.assertEqual(plan.widgets['b'], {'run': 'r1', 'prev_run': None})

    def test_different_arguments_are_different_calls(self):
        plan = build_plan([widget('a', args={'n': 1}), widget('b', args={'n': 2})], self.window, False)
        self.assertEqual([r.key for r in plan.runs], ['r1', 'r2'])

    def test_argument_order_does_not_matter(self):
        a = widget('a', args={'x': 1, 'y': {'p': 1, 'q': 2}})
        b = widget('b', args={'y': {'q': 2, 'p': 1}, 'x': 1})
        self.assertEqual(len(build_plan([a, b], self.window, False).runs), 1)

    def test_one_and_true_are_not_the_same_argument(self):
        a = widget('a', args={'flag': 1})
        b = widget('b', args={'flag': True})
        self.assertEqual(len(build_plan([a, b], self.window, False).runs), 2)

    def test_the_same_call_on_two_connections_is_two_calls(self):
        plan = build_plan([widget('a', connection=7), widget('b', connection=8)], self.window, False)
        self.assertEqual([(r.connection_id, r.tool) for r in plan.runs], [(7, 'get_daily_performance'), (8, 'get_daily_performance')])

    def test_a_token_and_the_literal_date_it_resolves_to_are_the_same_call(self):
        token = widget('a', args={'start_date': '$date.start'})
        literal = widget('b', args={'start_date': self.window.start})
        plan = build_plan([token, literal], self.window, False)
        self.assertEqual(len(plan.runs), 1)
        self.assertEqual(plan.runs[0].args, {'start_date': '2026-08-28'})

    def test_keys_follow_first_use_in_layout_order(self):
        plan = build_plan([widget('a', tool='t1'), widget('b', tool='t2'), widget('c', tool='t1')], self.window, False)
        self.assertEqual([(r.key, r.tool) for r in plan.runs], [('r1', 't1'), ('r2', 't2')])
        self.assertEqual(plan.widgets['c']['run'], 'r1')

    def test_comparison_needs_all_three_conditions(self):
        dated = {'s': '$date.start', 'e': '$date.end'}
        undated = {'customer': '1'}
        for report_compare in (False, True):
            for asks in (False, True):
                for args, has_dates in ((dated, True), (undated, False)):
                    plan = build_plan([widget('a', args=args, compare=asks)], self.window, report_compare)
                    expected = report_compare and asks and has_dates
                    got = plan.widgets['a']['prev_run'] is not None
                    self.assertEqual(got, expected, (report_compare, asks, has_dates))

    def test_the_comparison_call_reads_the_previous_period(self):
        plan = build_plan([widget('a', args={'s': '$date.start', 'e': '$date.end'}, compare=True)], self.window, True)
        self.assertEqual(plan.widgets['a'], {'run': 'r1', 'prev_run': 'r2'})
        self.assertEqual(plan.runs[0].args, {'s': '2026-08-28', 'e': '2026-09-26'})
        self.assertEqual(plan.runs[1].args, {'s': '2026-07-29', 'e': '2026-08-27'})

    def test_the_main_run_is_numbered_before_its_comparison(self):
        args = {'s': '$date.start'}
        plan = build_plan([widget('a', args=args, compare=True), widget('b', tool='other', args=args)], self.window, True)
        self.assertEqual([r.key for r in plan.runs], ['r1', 'r2', 'r3'])
        self.assertEqual(plan.runs[2].tool, 'other')

    def test_widgets_that_share_a_comparison_window_share_the_call(self):
        args = {'s': '$date.start'}
        plan = build_plan([widget('a', args=args, compare=True), widget('b', args=args, compare=True)], self.window, True)
        self.assertEqual(len(plan.runs), 2)
        self.assertEqual(plan.widgets['a'], plan.widgets['b'])

    def test_a_previous_period_call_can_be_the_same_as_a_current_one(self):
        # b asks, in plain tokens, for exactly the period a compares against.
        a = widget('a', args={'s': '$date.start'}, compare=True)
        b = widget('b', args={'s': '$date.prev_start'})
        plan = build_plan([a, b], self.window, True)
        self.assertEqual(len(plan.runs), 2)
        self.assertEqual(plan.widgets['b']['run'], plan.widgets['a']['prev_run'])

    def test_thirty_widgets_with_comparisons_make_at_most_sixty_calls(self):
        widgets = [widget('w{0}'.format(i), args={'n': i, 's': '$date.start'}, compare=True) for i in range(30)]
        self.assertEqual(len(build_plan(widgets, self.window, True).runs), 60)

    def test_a_widget_with_no_args_key_still_plans(self):
        raw = widget('a')
        del raw['source']['args']
        self.assertEqual(len(build_plan([raw], self.window, False).runs), 1)

    def test_an_empty_layout_plans_nothing(self):
        plan = build_plan([], self.window, True)
        self.assertEqual((plan.runs, plan.widgets), ([], {}))
