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

class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_transport_and_npaw(self):
        original = httpx.AsyncClient
        for proxy in ('http://user:secret@proxy.test:8080', ''):
            options, requests = [], []
            def handler(req):
                requests.append(req)
                self.assertEqual(req.url.host, 'api.test')
                self.assertEqual(req.url.params['internetId'], '001')
                return httpx.Response(200, json={'status': 'Normal', 'errors': []})
            def factory(**kwargs):
                options.append(kwargs)
                return original(transport=httpx.MockTransport(handler))
            env = {'PROXY_URL': proxy, 'API_PAC_URL': 'http://pac.test/config', 'NPAW_API_URL': 'https://api.test/'}
            with patch.dict(os.environ, env), patch('common.api_http.load_dotenv'), patch('common.api_http.httpx.AsyncClient', side_effect=factory):
                result = await check('001')
            self.assertNotEqual(result.status, 'error')
            self.assertEqual(len(requests), 1)
            self.assertEqual(options[0]['proxy'], proxy or None)
            self.assertEqual(options[0]['trust_env'], not bool(proxy))

    async def test_invalid_proxy(self):
        with patch.dict(os.environ, {'PROXY_URL': 'ftp://user:secret@proxy.test', 'NPAW_API_URL': 'https://api.test/'}), patch('common.api_http.load_dotenv'):
            result = await check('001')
        self.assertEqual(result.status, 'error')
        self.assertIn('PROXY_URL', result.details['error'])
        self.assertNotIn('secret', str(result.details))

    async def test_api_failure_diagnostics(self):
        original = httpx.AsyncClient
        for failure in [httpx.ConnectError('DNS lookup failed'), httpx.ProxyError('407 Proxy Authentication Required'), httpx.ReadTimeout('read timed out')]:
            def handler(req):
                raise failure
            def factory(**kwargs):
                return original(transport=httpx.MockTransport(handler))
            with patch.dict(os.environ, {'PROXY_URL': '', 'API_PAC_URL': '', 'NPAW_API_URL': 'https://api.test/'}), patch('common.api_http.httpx.AsyncClient', side_effect=factory):
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
            with patch.dict(os.environ, {'PROXY_URL': '', 'API_PAC_URL': '', 'NPAW_API_URL': 'https://api.test/'}), patch('common.api_http.httpx.AsyncClient', side_effect=factory):
                result = await check('001')
            self.assertEqual(result.details['diagnostics']['stage'], stage)
            self.assertEqual(result.details['diagnostics']['http_status'], status)


if __name__ == '__main__':
    unittest.main()
