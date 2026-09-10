from contextlib import closing
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from common import api_http
import main

from common.d1_fibres import read_fibre_list, validate_fibres


VALID = dict(name='john doe', fibre_id='00123', mesh=2, playbox=0)
ENV = dict(CLOUDFLARE_ACCOUNT_ID='account', CLOUDFLARE_D1_DATABASE_ID='database', CLOUDFLARE_API_TOKEN='secret')


class D1Tests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(validate_fibres([VALID]), [VALID])
        cases = [[], {}, [None], [{}], [VALID, VALID]]
        for field in ('name', 'fibre_id'):
            cases.extend([[dict(VALID, **{field: value})] for value in (None, 1, '', ' x')])
        for field in ('mesh', 'playbox'):
            cases.extend([[dict(VALID, **{field: value})] for value in (None, True, False, 1.0, '1', -1)])
        for records in cases:
            with self.subTest(records=records), self.assertRaises(ValueError):
                validate_fibres(records)

    def load(self, handler):
        async def request(method, url, **kwargs):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await client.request(method, url, **kwargs)
        with patch.dict(os.environ, ENV, clear=True), patch('common.d1_fibres.load_dotenv'), patch('common.api_http.request', side_effect=request) as shared, patch('httpx.Client', side_effect=AssertionError('D1 bypassed shared helper')):
            result = read_fibre_list()
            shared.assert_awaited_once()
            self.assertEqual(shared.call_args.args[0], 'POST')
            self.assertEqual(shared.call_args.kwargs['timeout'], 30.0)
            return result

    def test_query(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.headers['authorization'], 'Bearer secret')
            self.assertEqual(str(request.url), 'https://api.cloudflare.com/client/v4/accounts/account/d1/database/database/query')
            self.assertEqual(json.loads(request.content)['sql'], 'SELECT name, fibre_id, mesh, playbox FROM fibre_list ORDER BY fibre_id')
            return httpx.Response(200, json={'success': True, 'result': [{'success': True, 'results': [VALID]}]})
        self.assertEqual(self.load(handler), [VALID])
        self.assertEqual(len(requests), 1)

    def test_failures(self):
        responses = [httpx.Response(401), httpx.Response(500), httpx.Response(200, text='invalid'),
                     httpx.Response(200, json={'success': False}),
                     httpx.Response(200, json={'success': True, 'result': [{'success': False}]}),
                     httpx.Response(200, json={'success': True, 'result': []}),
                     httpx.Response(200, json={'success': True, 'result': [{'success': True, 'results': []}]})]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.load(lambda request: response)
        def timeout(request):
            raise httpx.ReadTimeout('secret', request=request)
        with self.assertRaises(ValueError) as error:
            self.load(timeout)
        self.assertNotIn('secret', str(error.exception))

    def test_missing_config(self):
        with patch.dict(os.environ, {}, clear=True), patch('common.d1_fibres.load_dotenv'), patch('common.api_http.request') as client:
            with self.assertRaisesRegex(ValueError, 'CLOUDFLARE_ACCOUNT_ID'):
                read_fibre_list()
            client.assert_not_called()

    def test_routing_and_startup_failures(self):
        original = httpx.AsyncClient
        for mode in ('proxy', 'environment', 'network'):
            with self.subTest(mode=mode):
                options, requests = [], []
                def handler(req):
                    requests.append(req)
                    self.assertEqual(req.url.host, 'api.cloudflare.com')
                    self.assertEqual(req.extensions['timeout']['read'], 30.0)
                    if mode == 'network':
                        raise httpx.ReadTimeout('secret credential query=value')
                    return httpx.Response(200, json={'success': True, 'result': [{'success': True, 'results': [VALID]}]})
                def factory(**kwargs):
                    options.append(kwargs)
                    return original(transport=httpx.MockTransport(handler))
                env = dict(ENV, PROXY_URL='' if mode == 'environment' else 'http://proxy.test:8080', API_PAC_URL='http://pac.test/config', HTTP_PROXY='http://env.test:80', HTTPS_PROXY='http://env.test:80')
                with patch.dict(os.environ, env, clear=True), patch('common.d1_fibres.load_dotenv'), patch('common.api_http.load_dotenv'), patch('common.api_http.httpx.AsyncClient', side_effect=factory), patch('httpx.Client', side_effect=AssertionError('D1 bypassed shared helper')):
                    if mode == 'network':
                        with patch.object(main, 'cleanup_logs'), patch.object(main, 'persist_run_log'), patch.object(main, 'setup_logging'), patch.object(main, 'run') as run, patch.object(main, 'save_summary') as save, self.assertLogs(main.logger, level='ERROR') as logs:
                            with self.assertRaises(SystemExit) as error:
                                main.main([])
                        self.assertEqual(error.exception.code, 1)
                        run.assert_not_called()
                        save.assert_not_called()
                        self.assertNotIn('secret', str(logs.output))
                        self.assertNotIn('query=value', str(logs.output))
                        self.assertEqual(len(requests), 1)
                    else:
                        self.assertEqual(read_fibre_list(), [VALID])
                        self.assertEqual(len(requests), 1)
                        self.assertEqual(options[-1]['trust_env'], mode == 'environment')
                        self.assertEqual(options[-1]['proxy'], 'http://proxy.test:8080' if mode == 'proxy' else None)


class MigrationTests(unittest.TestCase):
    def test_seed_and_constraints(self):
        root = Path(__file__).resolve().parents[1]
        with closing(sqlite3.connect(':memory:')) as db:
            db.executescript((root / 'migrations/001_fibre_list.sql').read_text())
            seed = (root / 'migrations/002_seed_fibre_list.sql').read_text()
            db.executescript(seed)
            expected = sorted([
                ('8804133194', 'john', 1, 1), ('8801478464', 'mike', 1, 1),
                ('8806756368', 'oven', 1, 1), ('8806916302', 'sucy', 1, 1),
            ])
            self.assertEqual(db.execute('SELECT fibre_id, name, mesh, playbox FROM fibre_list ORDER BY fibre_id').fetchall(), expected)
            db.execute("UPDATE fibre_list SET name='edited', mesh=3")
            db.executescript(seed)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibre_list WHERE name='edited' AND mesh=3").fetchone()[0], 4)
            for value in (-1, 1.5, 'invalid', None):
                with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                    db.execute('UPDATE fibre_list SET mesh=?', (value,))
