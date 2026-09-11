import copy
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import main
from common.models import ModuleResult
from common.teams_notification import build_html_message
from modules.onesense import checker


def alert(number=1):
    return dict(alert_id=number, service_name='001', target='example.com',
                agent_id='agent-a', isp=None, type='PING_TIMEOUT', severity='CRITICAL',
                status='CLOSED', start_time='2026-09-10T10:00:00+07:00',
                end_time='2026-09-10T10:02:00+07:00', duration_seconds=120,
                overlap_seconds=120, detail={'summary': 'Timeout <detected>',
                'max_packets_lost': 100}, traceroute={'raw_route': 'PRIVATE_TRACE'})


def page(status='abnormal', **kwargs):
    result = dict(service_name='001', range='15m', status=status,
                  as_of='2026-09-10T10:15:00+07:00',
                  **{'from': '2026-09-10T10:00:00+07:00', 'to': '2026-09-10T10:15:00+07:00'},
                  alert_count=1 if status == 'abnormal' else 0,
                  alerts=[alert()] if status == 'abnormal' else [],
                  limit=50, offset=0, has_more=False, next_offset=None)
    result.update(kwargs)
    return result


class OneSenseTests(unittest.IsolatedAsyncioTestCase):
    async def check_pages(self, pages):
        responses = [p if isinstance(p, Exception) else httpx.Response(200, json=p) for p in pages]
        with patch.dict(os.environ, ONESENSE_API_URL='https://example.com/api?service_name=old&range=24h', ONESENSE_API_KEY='SECRET'), patch.object(checker, 'load_dotenv', create=True), patch.object(checker, 'request', AsyncMock(side_effect=responses), create=True) as request:
            result = await checker.check('001')
        return result, request

    async def test_pagination_auth_and_deduplication(self):
        result, request = await self.check_pages([
            page(alert_count=2, has_more=True, next_offset=1),
            page(alert_count=2, offset=1, alerts=[alert(), alert(2)])])
        self.assertEqual(result.status, 'abnormal')
        self.assertEqual(len(result.details['alerts']), 2)
        first = request.call_args_list[0]
        self.assertEqual(first.args[0], 'GET')
        self.assertEqual(first.kwargs['headers'], {'x-api-key': 'SECRET'})
        self.assertEqual(dict(first.args[1].params), {'service_name': '001', 'range': '15m', 'limit': '50', 'offset': '0'})
        self.assertEqual(request.call_args_list[1].args[1].params['as_of'], page()['as_of'])
        self.assertIn('PRIVATE_TRACE', str(result.details))

    async def test_three_pages_keep_same_as_of(self):
        result, request = await self.check_pages([
            page(alert_count=3, has_more=True, next_offset=1),
            page(alert_count=3, offset=1, has_more=True, next_offset=2, alerts=[alert(2)]),
            page(alert_count=3, offset=2, alerts=[alert(3)])])
        self.assertEqual(result.status, 'abnormal')
        self.assertEqual(len(result.details['alerts']), 3)
        for call in request.call_args_list[1:]:
            self.assertEqual(call.args[1].params['as_of'], '2026-09-10T10:15:00+07:00')

    async def test_statuses_and_partial_failure(self):
        for status in ('normal', 'unknown', 'abnormal'):
            result, _ = await self.check_pages([page(status)])
            self.assertEqual(result.status, status)
        result, _ = await self.check_pages([page(has_more=True, next_offset=1), httpx.ReadTimeout('SECRET')])
        self.assertEqual(result.status, 'error')
        self.assertTrue(result.details['incomplete'])
        self.assertEqual(len(result.details['alerts']), 1)
        self.assertNotIn('SECRET', str(result.details))

    async def test_invalid_pages(self):
        for payload in ([], page(status='bad'), page(alerts='bad'), page(has_more=True, next_offset=0), page(alerts=[{'alert_id': True}]),
                        page(service_name='other'), page(has_more=True, next_offset=10001),
                        page(as_of='2026-09-10T10:15:00'), page(alert_count=2),
                        page('normal', alerts=[alert()])):
            result, _ = await self.check_pages([payload])
            self.assertEqual(result.status, 'error')

    async def test_changed_window_retains_previous_alerts(self):
        result, _ = await self.check_pages([
            page(alert_count=2, has_more=True, next_offset=1),
            page(offset=1, as_of='2026-09-10T10:16:00+07:00', alerts=[alert(2)])])
        self.assertEqual(result.status, 'error')
        self.assertEqual([a['alert_id'] for a in result.details['alerts']], [1])
        self.assertTrue(result.details['incomplete'])

    async def test_invalid_continuation_retains_valid_current_alerts(self):
        for next_offset in (0, 10001, None):
            result, _ = await self.check_pages([page(has_more=True, next_offset=next_offset)])
            self.assertEqual(result.status, 'error')
            self.assertTrue(result.details['incomplete'])
            self.assertEqual([a['alert_id'] for a in result.details['alerts']], [1])

    async def test_high_latency_and_null_metrics(self):
        incident = alert()
        incident.update(type='HIGH_LATENCY', status='OPEN', end_time=None,
                        detail={'summary': 'High latency', 'last_avg_ping_ms': 150,
                                'threshold_ms': 100, 'worst_avg_ping_ms': 250,
                                'max_packets_lost': None})
        result, _ = await self.check_pages([page(alerts=[incident])])
        self.assertEqual(result.status, 'abnormal')
        self.assertEqual(result.details['alerts'][0], incident)

    async def test_http_errors_are_sanitized(self):
        for code in (400, 401, 405, 422, 503):
            with patch.dict(os.environ, ONESENSE_API_URL='https://example.com', ONESENSE_API_KEY='SECRET'), patch.object(checker, 'load_dotenv'), patch.object(checker, 'request', AsyncMock(return_value=httpx.Response(code, text='SECRET'))):
                result = await checker.check('001')
            self.assertEqual(result.status, 'error')
            self.assertIn(str(code), result.details['error'])
            self.assertNotIn('SECRET', str(result.details))

    async def test_missing_config_and_http_failure(self):
        with patch.dict(os.environ, ONESENSE_API_URL='', ONESENSE_API_KEY=''), patch.object(checker, 'load_dotenv', create=True):
            self.assertEqual((await checker.check('001')).status, 'error')
        with patch.dict(os.environ, ONESENSE_API_URL='https://example.com', ONESENSE_API_KEY='SECRET'), patch.object(checker, 'load_dotenv', create=True), patch.object(checker, 'request', AsyncMock(return_value=httpx.Response(404, json={'error': 'service_not_found'})), create=True):
            result = await checker.check('001')
        self.assertEqual(result.status, 'error')
        self.assertIn('service_not_found', str(result.details))


class MessageTests(unittest.TestCase):
    def summary(self, status='abnormal'):
        return {'finished_at': '2026-09-10T03:57:13+00:00', 'fibres': [
            {'name': '<VIP>', 'fibre_id': '001', 'overall_status': status,
             'modules': {'onesense': {'status': status, 'details': dict(alerts=[alert()] if status == 'abnormal' else [], reason='insufficient_measurement_coverage')},
                         'airnet': {'status': 'abnormal', 'details': {'online_status': 'Offline', 'row_count': 42}}}}]}

    def test_header_colors_and_readable_details(self):
        value = self.summary()
        original = copy.deepcopy(value)
        html = build_html_message(value)
        for text in ('<b>VIP proactive monitoring</b>', '2026-09-10 10:57:13 (UTC+7)', '<span style="color:red">abnormal</span>',
                     '<tr><th>Field</th><th>Value</th></tr>',
                     'PING_TIMEOUT', 'CLOSED', '2 minutes', 'Timeout &lt;detected&gt;',
                     '&quot;max_packets_lost&quot;: 100'):
            self.assertIn(text, html)
        self.assertNotIn('42 rows', html)
        self.assertNotIn('PRIVATE_TRACE', html)
        self.assertEqual(original, value)

    def test_duration_in_days_hours_minutes(self):
        value = self.summary()
        incident = value['fibres'][0]['modules']['onesense']['details']['alerts'][0]
        for seconds, expected in [(90060, '1 day 1 hour 1 minute'),
                                  (172920, '2 days 2 minutes'),
                                  (3600, '1 hour'), (170, '2 minutes'),
                                  (59, 'Less than 1 minute'), (0, '0 minutes'),
                                  (None, '-')]:
            with self.subTest(seconds=seconds):
                incident['duration_seconds'] = seconds
                self.assertIn(f'<td>Duration</td><td>{expected}</td>', build_html_message(value))
                self.assertEqual(incident['duration_seconds'], seconds)

    def test_smart7_offline_label(self):
        value = self.summary()
        html = build_html_message(value)
        self.assertIn('<p>Router Offline</p>', html)
        self.assertEqual(value['fibres'][0]['modules']['airnet']['details']['online_status'], 'Offline')

    def test_compact_detail_and_bangkok_dates(self):
        value = self.summary()
        incident = value['fibres'][0]['modules']['onesense']['details']['alerts'][0]
        incident.update(start_time='2026-09-10T20:00:00Z', end_time=None)
        incident['detail'].update(packets_lost_unit='percent', effective_problem_seconds=15,
                                 last_metric_at='2026-09-11T03:01:00+07:00')
        original = copy.deepcopy(value)
        html = build_html_message(value)
        self.assertNotIn('<h4>Incident', html)
        self.assertNotIn('<pre', html)
        self.assertIn('<td>End</td><td>-</td>', html)
        self.assertIn('2026-09-11 03:00:00 (UTC+7)', html)
        self.assertIn('2026-09-11 03:01:00 (UTC+7)', html)
        self.assertNotIn('packets_lost_unit', html)
        self.assertNotIn('effective_problem_seconds', html)
        self.assertIn('&quot;max_packets_lost&quot;: 100', html)
        self.assertIn('<br>', html)
        self.assertEqual(original, value)
        incident['end_time'] = '2026-09-11T04:00:00+08:00'
        self.assertIn('<td>End</td><td>2026-09-11 03:00:00 (UTC+7)</td>', build_html_message(value))

    def test_unknown_and_aggregation(self):
        value = self.summary('unknown')
        value['fibres'][0]['modules'].pop('airnet')
        self.assertIn('insufficient_measurement_coverage', build_html_message(value))
        self.assertIn('color:#b36b00', build_html_message(value))
        self.assertEqual(main.aggregate_status([ModuleResult('airnet', '001', 'normal'), ModuleResult('onesense', '001', 'unknown')]), 'unknown')

    def test_partial_error_displays_retained_incident(self):
        value = self.summary('error')
        value['fibres'][0]['modules']['onesense']['details'].update(
            alerts=[alert()], incomplete=True, error='OneSense API returned HTTP 404 (service_not_found)')
        html = build_html_message(value)
        self.assertIn('service_not_found', html)
        self.assertIn('Incomplete results', html)
        self.assertIn('Timeout &lt;detected&gt;', html)
        value['fibres'][0]['modules']['onesense']['details']['error'] = 'SECRET'
        self.assertNotIn('SECRET', build_html_message(value))

    def test_unknown_only_run_delivers_notification(self):
        value = self.summary('unknown')
        value['fibres'][0]['modules'].pop('airnet')
        delivered = []
        async def send(html):
            delivered.append(html)
            return True
        with patch.object(main, 'cleanup_logs'), patch.object(main, 'read_fibre_list', return_value=[]), patch.object(main, 'run', AsyncMock(return_value=value)), patch.object(main, 'save_summary'), patch.object(main, 'print_summary'), patch.object(main, 'persist_run_log'), patch.object(main, 'send_notification', side_effect=send), patch('builtins.print'):
            self.assertEqual(main.run_once(), 0)
        self.assertEqual(len(delivered), 1)
        self.assertIn('insufficient_measurement_coverage', delivered[0])

    def test_alerts_omitted_before_fibre(self):
        value = self.summary()
        value['fibres'][0]['modules']['onesense']['details']['alerts'] = [dict(alert(i), detail={'summary': 'ก' * 1000}) for i in range(20)]
        html = build_html_message(value)
        self.assertLessEqual(len(html.encode()), 24 * 1024)
        self.assertIn('alerts omitted', html)
        self.assertIn('Fibre ID: 001', html)
        self.assertEqual(html.count('<table>'), html.count('</table>'))
