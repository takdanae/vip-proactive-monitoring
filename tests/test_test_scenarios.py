import unittest
from io import BytesIO, TextIOWrapper
from unittest.mock import AsyncMock, patch

import main
from common import teams_notification as teams
from modules.airnet.checker import _count_recent_offline_rows, TZ_BKK
from datetime import datetime


class TestScenarioSummaryTests(unittest.TestCase):
    def test_each_scenario_builds_a_test_marked_summary(self):
        expected_service = {
            'smart7-offline': 'airnet',
            'smart7-recent-offlines': 'airnet',
            'smart7-error': 'airnet',
            'npaw-errors': 'npaw',
            'npaw-long-metadata': 'npaw',
            'npaw-oversize-metadata': 'npaw',
            'npaw-error': 'npaw',
            'onesense-alerts': 'onesense',
            'onesense-error': 'onesense',
            'all-errors': None,
            'all-abnormal': None,
        }

        for scenario, service in expected_service.items():
            with self.subTest(scenario=scenario):
                summary = main.build_test_summary(scenario)
                self.assertEqual(summary['testScenario'], scenario)
                self.assertEqual(summary['fibre_count'], 1)
                modules = summary['fibres'][0]['modules']
                if service is None:
                    expected_status = 'error' if scenario == 'all-errors' else 'abnormal'
                    self.assertEqual({module['status'] for module in modules.values()}, {expected_status})
                else:
                    self.assertIn(modules[service]['status'], ('abnormal', 'error'))
                    self.assertTrue(all(
                        module['status'] == 'normal'
                        for name, module in modules.items() if name != service
                    ))

    def test_test_summary_renders_test_banner_and_service_details(self):
        expected = {
            'smart7-offline': 'Router Offline',
            'smart7-recent-offlines': '3 recent offline records within 30 minutes',
            'smart7-error': 'Retrieval failed',
            'npaw-errors': 'Task: VDO Error',
            'npaw-long-metadata': 'LONG_METADATA_',
            'npaw-error': 'Retrieval failed',
            'onesense-alerts': 'HIGH_LATENCY',
            'onesense-error': 'OneSense API request timed out',
            'all-errors': 'Retrieval failed',
            'all-abnormal': 'Smart7',
        }

        for scenario, detail in expected.items():
            with self.subTest(scenario=scenario):
                html = teams.build_html_message(main.build_test_summary(scenario))
                self.assertIn(f'TEST: {scenario}', html)
                self.assertIn(detail, html)

    def test_all_abnormal_renders_all_three_services(self):
        html = teams.build_html_message(main.build_test_summary('all-abnormal'))
        for service in ('Smart7', 'NPAW', 'OneSense'):
            self.assertIn(service, html)
        self.assertIn('Status: <span style="color:red">abnormal</span>', html)

    def test_oversize_metadata_stays_within_the_teams_html_limit(self):
        html = teams.build_html_message(main.build_test_summary('npaw-oversize-metadata'))
        self.assertLessEqual(len(html.encode('utf-8')), teams.MAX_HTML_BYTES)
        self.assertIn('1 fibres omitted', html)


class Smart7RecentOfflineTests(unittest.TestCase):
    def test_counts_only_recent_parseable_offline_rows(self):
        now = datetime(2026, 9, 11, 10, 30, tzinfo=TZ_BKK)
        rows = ([{'Offline Time': '11/09/2026 10:01'}] * 3
                + [{'Offline Time': '11/09/2026 09:59'}, {'Offline Time': 'invalid'}, {}])
        self.assertEqual(_count_recent_offline_rows(rows, now), 3)


class TestScenarioCliTests(unittest.TestCase):
    def test_console_output_is_utf8_for_unicode_summary(self):
        raw = BytesIO()
        stream = TextIOWrapper(raw, encoding='cp874', errors='strict')
        with patch.object(main.sys, 'stdout', stream):
            main.configure_console_output()
            print('═', file=stream)
            stream.flush()

        self.assertIn('═'.encode('utf-8'), raw.getvalue())

    def test_test_scenario_does_not_touch_live_services_or_send_by_default(self):
        with patch.object(main, 'setup_logging'), \
             patch.object(main, 'read_fibre_list') as read_fibres, \
             patch.object(main, 'cleanup_logs') as cleanup, \
             patch.object(main, 'persist_run_log') as persist, \
             patch.object(main, 'send_notification', new_callable=AsyncMock) as send, \
             patch.object(main, 'save_test_summary') as save, \
             patch.object(main, 'print_summary'), \
             patch('builtins.print'):
            main.main(['--test-scenario', 'npaw-errors'])

        read_fibres.assert_not_called()
        cleanup.assert_not_called()
        persist.assert_not_called()
        send.assert_not_awaited()
        save.assert_called_once()

    def test_send_teams_is_opt_in_for_test_scenarios(self):
        with patch.object(main, 'setup_logging'), \
             patch.object(main, 'save_test_summary'), \
             patch.object(main, 'print_summary'), \
             patch.object(main, 'send_notification', new_callable=AsyncMock) as send, \
             patch('builtins.print'):
            main.main(['--test-scenario', 'onesense-alerts', '--send-teams'])

        send.assert_awaited_once()
        self.assertIn('TEST: onesense-alerts', send.await_args.args[0])

    def test_invalid_flag_combinations_are_rejected(self):
        with patch.object(main, 'setup_logging'):
            with self.assertRaises(SystemExit):
                main.main(['--send-teams'])
            with self.assertRaises(SystemExit):
                main.main(['--test-scenario', 'smart7-error', '--cron'])
