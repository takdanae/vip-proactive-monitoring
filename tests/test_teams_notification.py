import copy
import json
from html import escape
import os
import logging
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import main
from common import teams_notification as teams


def summary(status='abnormal'):
    return {'finished_at': '2026-09-10T02:24:04+00:00', 'fibres': [
        {'name': 'VIP <A>&', 'fibre_id': '001', 'overall_status': status,
         'modules': {
             'airnet': {'status': status, 'details': {'online_status': 'Online', 'row_count': 2, 'output_file': 'SECRET_PATH'}},
             'npaw': {'status': 'abnormal', 'details': {'errors': ['Playback <failed>']}},
             'onesense': {'status': 'N/A', 'details': {'message': 'Not implemented yet', 'diagnostics': 'SECRET_DIAGNOSTICS'}},
         }}]}


class FormattingTests(unittest.TestCase):
    def test_status_priority_and_empty(self):
        for statuses, expected in [([], 'unknown'), (['normal'], 'normal'),
                (['normal', 'unknown'], 'unknown'), (['unknown'], 'unknown'),
                (['unknown', 'abnormal'], 'abnormal'), (['abnormal', 'error'], 'error')]:
            with self.subTest(statuses=statuses):
                value = summary()
                value['fibres'] = [dict(value['fibres'][0], overall_status=s) for s in statuses]
                self.assertEqual(teams.run_status(value), expected)

    def test_layout_details_escaping_and_no_mutation(self):
        value = summary()
        errors = [{'code': 42, 'message': '\u0e44\u0e17\u0e22 <failed>&', 'devices': ['a', 'b']}]
        value['fibres'][0]['modules']['onesense'] = {'status': 'unknown', 'details': {'reason': 'insufficient_measurement_coverage'}}
        value['fibres'][0]['modules']['npaw']['details']['errors'] = errors
        value['fibres'].append(copy.deepcopy(dict(value['fibres'][0], name='Second', fibre_id='002')))
        original = copy.deepcopy(value)
        html = teams.build_html_message(value)
        for text in ['Name: VIP &lt;A&gt;&amp;<br>', 'Fibre ID: 001<br>',
                     'Status: <span style="color:red">abnormal</span><br>', '<tr><th>Service</th><th>Detail</th></tr>',
                     'Online', 'insufficient_measurement_coverage',
                     '<h4>NPAW error details</h4>', '<pre style="white-space:pre-wrap;word-break:break-word">']:
            self.assertIn(text, html)
        self.assertLess(html.index('Smart7'), html.index('NPAW'))
        self.assertLess(html.index('NPAW'), html.index('OneSense'))
        self.assertLess(html.index('Fibre ID: 001'), html.index('Fibre ID: 002'))
        self.assertEqual(html.count('<table>'), 2)
        self.assertNotIn('SECRET', html)
        self.assertEqual(value, original)

    def test_npaw_renders_vdo_and_app_errors_as_task_tables(self):
        value = summary()
        value['fibres'][0]['modules'] = {
            'npaw': {'status': 'abnormal', 'details': {'errors': [
                {'task': 'VDO Error', 'error': [{
                    'errorCode': 'ERROR_CODE_IO_NETWORK_CONNECTION_FAILED (2001)',
                    'description': 'HttpDataSourceException <failed>', 'title': '\u0e44\u0e17\u0e22',
                    'device': 'Android', 'occurredAt': '2026-09-10 15:57:43',
                }]},
                {'task': 'App Error', 'error': [{
                    'errorName': '80100005', 'description': 'can not get new jwtToken!',
                    'metadata': 'jwttoken ajax error', 'count': 1,
                }]},
            ]}},
        }
        html = teams.build_html_message(value)
        for text in ['<h4>Task: VDO Error</h4>', 'Error Code / Name',
                     'Description / Message', 'Occurred At',
                     'HttpDataSourceException &lt;failed&gt;', '\u0e44\u0e17\u0e22',
                     '<h4>Task: App Error</h4>', 'Error Name / Code', 'Metadata',
                     '80100005', 'jwttoken ajax error']:
            self.assertIn(text, html)
        self.assertNotIn('Errors:', html)
        vdo_table = html.split('<h4>Task: VDO Error</h4>', 1)[1].split('</table>', 1)[0]
        self.assertLess(vdo_table.index('<th>Occurred At</th>'),
                        vdo_table.index('<th>Error Code / Name</th>'))
        self.assertLess(vdo_table.index('<td>2026-09-10 15:57:43</td>'),
                        vdo_table.index('<td>ERROR_CODE_IO_NETWORK_CONNECTION_FAILED (2001)</td>'))

    def test_npaw_unknown_shape_uses_escaped_pretty_json(self):
        value = summary()
        value['fibres'][0]['modules'] = {
            'npaw': {'status': 'abnormal', 'details': {'errors': [
                {'task': 'Unexpected', 'payload': {'message': '<failed>&'},
                 'diagnostics': {'url': 'SECRET_URL', 'token': 'SECRET_TOKEN'}},
            ]}},
        }
        html = teams.build_html_message(value)
        self.assertIn('<pre style="white-space:pre-wrap;word-break:break-word">', html)
        self.assertIn('&quot;task&quot;: &quot;Unexpected&quot;', html)
        self.assertIn('&lt;failed&gt;&amp;', html)
        self.assertNotIn('SECRET', html)

    def test_onesense_incident_uses_field_table_and_pretty_detail(self):
        value = summary()
        value['fibres'][0]['modules'] = {
            'onesense': {'status': 'abnormal', 'details': {'alerts': [{
                'alert_id': 12088, 'type': 'HIGH_LATENCY', 'severity': 'MINOR',
                'target': '176.109.89.10', 'isp': 'AIS FIBRE', 'status': 'OPEN',
                'start_time': '2026-09-10T16:06:08+07:00', 'end_time': None,
                'duration_seconds': 170, 'detail': {
                    'summary': '\u0e44\u0e17\u0e22 <detected>&', 'nested': {'threshold_ms': 200},
                },
            }]}},
        }
        html = teams.build_html_message(value)
        for text in ['<tr><th>Field</th><th>Value</th></tr>',
                     'Type', 'HIGH_LATENCY', 'Severity', 'MINOR', 'Target',
                     'ISP', 'Incident state', 'Start', '<td>End</td><td>-</td>',
                     'Duration', '2 minutes', 'Detail',
                     '&quot;summary&quot;: &quot;\u0e44\u0e17\u0e22 &lt;detected&gt;&amp;&quot;',
                     '&quot;nested&quot;: {']:
            self.assertIn(text, html)

    def test_filter_and_empty_errors(self):
        value = summary()
        value['fibres'][0]['modules']['airnet']['status'] = 'normal'
        value['fibres'][0]['modules']['npaw']['details'] = {}
        healthy = copy.deepcopy(value['fibres'][0])
        healthy.update(name='Healthy customer', fibre_id='healthy', overall_status='normal')
        healthy['modules']['npaw']['status'] = 'normal'
        value['fibres'].append(healthy)
        html = teams.build_html_message(value)
        self.assertIn('<h4>NPAW error details</h4>', html)
        self.assertIn('<pre style="white-space:pre-wrap;word-break:break-word">[]</pre>', html)
        for text in ['Smart7', 'OneSense', 'Healthy customer', 'healthy', 'Status: Normal']:
            self.assertNotIn(text, html)
        value['fibres'][0]['modules']['npaw']['status'] = 'normal'
        self.assertEqual(teams.build_html_message(value), '')
        self.assertEqual(teams.build_html_message({'fibres': []}), '')
        for module in value['fibres'][0]['modules'].values():
            module['status'] = 'N/A'
        self.assertEqual(teams.build_html_message(value), '')

    def test_error_only_is_sanitized(self):
        value = summary('error')
        value['fibres'][0]['modules']['npaw']['status'] = 'normal'
        value['fibres'][0]['modules']['airnet']['details'] = {'error': 'SECRET_URL', 'errors': ['SECRET']}
        html = teams.build_html_message(value)
        self.assertIn('Status: <span style="color:red">error</span>', html)
        self.assertIn('Retrieval failed', html)
        self.assertNotIn('Errors:', html)
        self.assertNotIn('SECRET', html)

    def test_large_unicode_message_omits_complete_fibres(self):
        value = summary()
        block = teams.build_html_message(value)
        value['fibres'] *= 3
        limit = len((block + '<i>2 fibres omitted; full results in summary.json</i><br>').encode('utf-8'))
        with patch.object(teams, 'MAX_HTML_BYTES', limit):
            html = teams.build_html_message(value)
        self.assertLessEqual(len(html.encode('utf-8')), limit)
        self.assertEqual(html.count('<table>'), 1)
        self.assertEqual(html.count('</table>'), 1)
        self.assertIn('2 fibres omitted', html)
        value['fibres'] *= 200
        value['fibres'][0]['name'] = '\u0e44\u0e17\u0e22<&' * 3000
        html = teams.build_html_message(value)
        self.assertLessEqual(len(html.encode('utf-8')), 24 * 1024)
        self.assertIn('600 fibres omitted', html)
        self.assertNotIn('<table>', html)

    def test_message_at_limit_is_not_truncated(self):
        value = summary()
        value['fibres'][0]['modules']['npaw']['details']['errors'] = ['x' * 9000]
        expected = teams.build_html_message(value)
        with patch.object(teams, 'MAX_HTML_BYTES', len(expected.encode('utf-8'))):
            self.assertEqual(teams.build_html_message(value), expected)
        self.assertIn('x' * 9000, expected)
        self.assertNotIn('omitted', expected)


class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_httpx_logs_do_not_expose_signed_url(self):
        url = 'https://example.com/flow?sig=SECRET'
        async def transport_request(method, endpoint, **kwargs):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(202))) as client:
                return await client.request(method, endpoint, **kwargs)
        with patch.dict(os.environ, {'POWER_AUTOMATE_URL': url}), patch.object(teams, 'load_dotenv'), patch.object(teams, 'request', side_effect=transport_request), self.assertLogs(level=logging.INFO) as logs:
            await teams.send_notification('hello')
        self.assertNotIn('SECRET', '\n'.join(logs.output))

    async def test_disabled(self):
        with patch.dict(os.environ, {'POWER_AUTOMATE_URL': ''}), patch.object(teams, 'load_dotenv'), patch.object(teams, 'request', new_callable=AsyncMock) as request:
            self.assertFalse(await teams.send_notification('<b>test</b>'))
            request.assert_not_called()

    async def test_payload_and_failures_without_retry(self):
        url = 'https://example.com/flow?sig=SECRET'
        for code in [200, 202, 204, 302, 400, 500]:
            response = httpx.Response(code, request=httpx.Request('POST', url))
            with patch.dict(os.environ, {'POWER_AUTOMATE_URL': url}), patch.object(teams, 'load_dotenv'), patch.object(teams, 'request', AsyncMock(return_value=response)) as request:
                if code < 300:
                    self.assertTrue(await teams.send_notification('hello'))
                else:
                    with self.assertRaises(teams.NotificationError) as error:
                        await teams.send_notification('hello')
                    self.assertNotIn('SECRET', str(error.exception))
                request.assert_awaited_once_with('POST', url, json={'htmlMessage': 'hello'})
        with patch.dict(os.environ, {'POWER_AUTOMATE_URL': url}), patch.object(teams, 'load_dotenv'), patch.object(teams, 'request', AsyncMock(side_effect=httpx.ReadTimeout(url))) as request:
            with self.assertRaises(teams.NotificationError) as error:
                await teams.send_notification('hello')
            self.assertNotIn('SECRET', str(error.exception))
            self.assertEqual(request.await_count, 1)


class IntegrationTests(unittest.TestCase):
    def test_save_and_print_before_failed_send(self):
        events = []
        async def send(html):
            events.append('send')
            self.assertIn('Name: VIP', html)
            raise teams.NotificationError('Delivery failed')
        value = summary()
        with patch.object(main, 'cleanup_logs'), patch.object(main, 'persist_run_log'), patch.object(main, 'setup_logging'), patch.object(main, 'read_fibre_list', return_value=[]), patch.object(main, 'run', AsyncMock(return_value=value)), patch.object(main, 'save_summary', side_effect=lambda s: events.append('save')), patch.object(main, 'print_summary', side_effect=lambda s: events.append('print')), patch.object(main, 'send_notification', side_effect=send), patch('builtins.print'), self.assertLogs(main.logger, level='ERROR'):
            with self.assertRaises(SystemExit) as error:
                main.main([])
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(events, ['save', 'print', 'send'])
        self.assertIn('htmlMessage', value)

    def test_no_alert_still_saves_and_logs_without_sending(self):
        value = summary('normal')
        value['fibres'][0]['modules']['npaw']['status'] = 'normal'
        original = copy.deepcopy(value)
        with patch.object(main, 'cleanup_logs'), patch.object(main, 'read_fibre_list', return_value=[]), patch.object(main, 'run', AsyncMock(return_value=value)), patch.object(main, 'save_summary') as save, patch.object(main, 'print_summary') as display, patch.object(main, 'persist_run_log') as persist, patch.object(main, 'send_notification', AsyncMock()) as send, patch('builtins.print'):
            self.assertEqual(main.run_once(), 0)
        self.assertEqual(value['htmlMessage'], '')
        self.assertEqual(value['fibres'], original['fibres'])
        save.assert_called_once_with(value)
        display.assert_called_once_with(value)
        persist.assert_called_once_with(value)
        send.assert_not_called()
