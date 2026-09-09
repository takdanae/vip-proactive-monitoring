import os
import unittest
from unittest.mock import patch

import httpx

from common import api_http
from modules.npaw.checker import check


class PacTests(unittest.TestCase):
    def test_diagnostic_redaction_and_causes(self):
        try:
            try:
                raise OSError('DNS lookup failed')
            except OSError as exc:
                raise httpx.ConnectError('Failed https://user:secret@api.test/path?token=secret') from exc
        except httpx.ConnectError as exc:
            causes = api_http.exception_details(exc)
        self.assertEqual(causes[1]['message'], 'DNS lookup failed')
        self.assertNotIn('secret', str(causes))
        self.assertIn('https://api.test/path', causes[0]['message'])

    def test_routes(self):
        for rule, expected in [('DIRECT', None), ('PROXY proxy.test:2520; DIRECT', 'http://proxy.test:2520')]:
            script = 'function FindProxyForURL(url, host) { return "' + rule + '"; }'
            self.assertEqual(api_http._evaluate_pac(script, 'https://api.test/'), expected)

    def test_host_selection(self):
        script = 'function FindProxyForURL(url, host) { return host === "api.test" ? "DIRECT" : "PROXY other:80"; }'
        self.assertIsNone(api_http._evaluate_pac(script, 'https://api.test/path?internetId=001'))

    def test_invalid_rule_and_script(self):
        for script in ['invalid javascript!', 'function FindProxyForURL(url, host) { return "SOCKS host:80"; }']:
            with self.assertRaises(api_http.ProxyConfigurationError):
                api_http._evaluate_pac(script, 'https://api.test/')
        self.assertIsNone(api_http._evaluate_pac('function FindProxyForURL(u,h){return "DIRECT";}', 'https://api.test/'))


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_pac_transport_and_npaw(self):
        original = httpx.AsyncClient
        groups = [{'task': name, 'error': [{'errorName': 'sample'}]} for name in ['App Error', 'APP CRASH', 'VDO Error']]
        for rule, proxy in [('DIRECT', None), ('PROXY proxy.test:2520', 'http://proxy.test:2520')]:
            options = []
            def handler(req):
                if req.url.host == 'pac.test':
                    return httpx.Response(200, text='function FindProxyForURL(u,h){return "' + rule + '";}')
                self.assertEqual(req.url.params['internetId'], '001')
                self.assertNotIn('authorization', req.headers)
                return httpx.Response(200, json={'status': 'Abnormal', 'errors': groups})
            def factory(**kwargs):
                options.append(kwargs)
                return original(transport=httpx.MockTransport(handler))
            with patch.dict(os.environ, {'API_PAC_URL': 'http://pac.test/config', 'NPAW_API_URL': 'https://api.test/'}), patch('common.api_http.httpx.AsyncClient', side_effect=factory):
                result = await check('001')
            self.assertEqual(result.status, 'abnormal')
            self.assertEqual(result.details['errors'], groups)
            self.assertFalse(options[0]['trust_env'])
            self.assertFalse(options[1]['trust_env'])
            self.assertEqual(options[1]['proxy'], proxy)

    async def test_pac_download_failure(self):
        original = httpx.AsyncClient
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(lambda req: httpx.Response(503)))
        with patch.dict(os.environ, {'API_PAC_URL': 'http://pac.test/config', 'NPAW_API_URL': 'https://api.test/'}), patch('common.api_http.httpx.AsyncClient', side_effect=factory):
            result = await check('001')
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.details['error'], 'Could not download API_PAC_URL')
        self.assertEqual(result.details['diagnostics']['stage'], 'pac_download')
        self.assertEqual(result.details['diagnostics']['pac_http_status'], 503)
        self.assertEqual(result.details['diagnostics']['causes'][1]['type'], 'HTTPStatusError')

    async def test_api_failure_diagnostics(self):
        original = httpx.AsyncClient
        for failure in [httpx.ConnectError('DNS lookup failed'), httpx.ProxyError('407 Proxy Authentication Required'), httpx.ReadTimeout('read timed out')]:
            def handler(req):
                raise failure
            def factory(**kwargs):
                return original(transport=httpx.MockTransport(handler))
            with patch.dict(os.environ, {'API_PAC_URL': '', 'NPAW_API_URL': 'https://api.test/'}), patch('common.api_http.httpx.AsyncClient', side_effect=factory):
                result = await check('001')
            diag = result.details['diagnostics']
            self.assertEqual(result.status, 'error')
            self.assertEqual(diag['stage'], 'api_request')
            self.assertEqual(diag['causes'][0]['type'], type(failure).__name__)
            self.assertEqual(diag['causes'][0]['message'], str(failure))

    async def test_response_failure_stages(self):
        original = httpx.AsyncClient
        for status, body, stage in [(403, 'Forbidden', 'http_response'), (200, '<html/>', 'response_json'), (200, '{"status":"unexpected"}', 'response_validation')]:
            def factory(**kwargs):
                return original(transport=httpx.MockTransport(lambda req: httpx.Response(status, text=body)))
            with patch.dict(os.environ, {'API_PAC_URL': '', 'NPAW_API_URL': 'https://api.test/'}), patch('common.api_http.httpx.AsyncClient', side_effect=factory):
                result = await check('001')
            self.assertEqual(result.details['diagnostics']['stage'], stage)
            self.assertEqual(result.details['diagnostics']['http_status'], status)


if __name__ == '__main__':
    unittest.main()
