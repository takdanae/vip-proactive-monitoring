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
                     'Errors: ' + escape(json.dumps(errors, ensure_ascii=False))]:
            self.assertIn(text, html)
        self.assertLess(html.index('Smart7'), html.index('NPAW'))
        self.assertLess(html.index('NPAW'), html.index('OneSense'))
        self.assertLess(html.index('Fibre ID: 001'), html.index('Fibre ID: 002'))
        self.assertEqual(html.count('<table>'), 2)
        self.assertNotIn('SECRET', html)
        self.assertEqual(value, original)

    def test_npaw_error_content(self):
        for source in ('npaw',):
            for errors in ([], ['Playback <failed>'], [{'code': 42, 'message': '\u0e44\u0e17\u0e22<&', 'context': {'devices': [1, 2]}}]):
                with self.subTest(source=source, errors=errors):
                    value = summary()
                    value['fibres'][0]['modules'] = {
                        source: {'status': 'abnormal', 'details': {'errors': errors}},
                    }
                    self.assertIn('Errors: ' + escape(json.dumps(errors, ensure_ascii=False)),
                                  teams.build_html_message(value))
            value['fibres'][0]['modules'][source]['details'] = {}
            self.assertIn('Errors: []', teams.build_html_message(value))

    def test_filter_and_empty_errors(self):
        value = summary()
        value['fibres'][0]['modules']['airnet']['status'] = 'normal'
        value['fibres'][0]['modules']['npaw']['details'] = {}
        healthy = copy.deepcopy(value['fibres'][0])
        healthy.update(name='Healthy customer', fibre_id='healthy', overall_status='normal')
        healthy['modules']['npaw']['status'] = 'normal'
        value['fibres'].append(healthy)
        html = teams.build_html_message(value)
        self.assertIn('Errors: []', html)
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
