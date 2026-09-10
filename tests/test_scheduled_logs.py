import json
import sqlite3
import unittest
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
import httpx
from common.models import ModuleResult
from modules.airnet.checker import _determine_status, TZ_BKK


class ScheduledLogsTests(unittest.TestCase):
    def test_cli_modes_and_interrupt(self):
        with patch.object(main, 'setup_logging'), patch.object(main, 'run_once', return_value=0) as once, patch.object(main, 'run_scheduled', side_effect=KeyboardInterrupt) as cron:
            main.main([])
            once.assert_called_once()
            cron.assert_not_called()
            main.main(['--cron'])
            cron.assert_called_once()
            self.assertEqual(once.call_count, 1)
        with patch.object(main, 'setup_logging'), patch.object(main, 'run_once', return_value=1):
            with self.assertRaises(SystemExit) as error:
                main.main([])
            self.assertEqual(error.exception.code, 1)

    def test_d1_write_parameters_and_safe_failures(self):
        from common.d1_fibres import query_d1
        env = {'CLOUDFLARE_ACCOUNT_ID': 'account', 'CLOUDFLARE_D1_DATABASE_ID': 'db', 'CLOUDFLARE_API_TOKEN': 'secret'}
        request = httpx.Request('POST', 'https://api.cloudflare.com')
        responses = [httpx.Response(200, json={'success': True, 'result': [{'success': True, 'results': []}]}, request=request),
                     httpx.Response(403, request=request),
                     httpx.Response(200, json={'success': True, 'result': [{'success': False}]}, request=request),
                     httpx.Response(200, text='bad', request=request),
                     httpx.ReadTimeout('secret')]
        for index, response in enumerate(responses):
            mock = AsyncMock(**({'side_effect': response} if isinstance(response, Exception) else {'return_value': response}))
            with patch.dict('os.environ', env, clear=True), patch('common.d1_fibres.load_dotenv'), patch('common.api_http.request', mock):
                if index == 0:
                    self.assertEqual(query_d1('INSERT INTO example VALUES (?)', ["ไทย ' quoted"]), [])
                    self.assertEqual(mock.call_args.kwargs['json'], {'sql': 'INSERT INTO example VALUES (?)', 'params': ["ไทย ' quoted"]})
                else:
                    with self.assertRaises(ValueError) as error:
                        query_d1('DELETE FROM example WHERE created_at < ?', ['cutoff'])
                    self.assertNotIn('secret', str(error.exception))

    def test_next_boundary(self):
        self.assertTrue(hasattr(main, 'next_quarter_hour'), 'scheduler is missing')
        for raw, expected in [('10:01:30', '10:15:00'), ('10:15:00', '10:30:00'), ('23:59:59', '00:00:00')]:
            now = datetime.fromisoformat('2026-09-10T' + raw).replace(tzinfo=TZ_BKK)
            result = main.next_quarter_hour(now)
            self.assertEqual(result.strftime('%H:%M:%S'), expected)
            self.assertGreater(result, now)

    def test_scheduler_skips_elapsed_boundaries_and_recovers(self):
        self.assertTrue(hasattr(main, 'run_scheduled'), 'scheduler is missing')
        times = iter([datetime(2026, 9, 10, 10, 1, tzinfo=TZ_BKK),
                      datetime(2026, 9, 10, 10, 15, tzinfo=TZ_BKK),
                      datetime(2026, 9, 10, 10, 37, tzinfo=TZ_BKK),
                      datetime(2026, 9, 10, 10, 45, tzinfo=TZ_BKK)])
        waits = []
        with patch.object(main, 'run_once', side_effect=[RuntimeError('failed'), KeyboardInterrupt]) as run:
            with self.assertLogs(main.logger, level='ERROR'), self.assertRaises(KeyboardInterrupt):
                main.run_scheduled(now=lambda: next(times), sleep=waits.append)
        self.assertEqual(waits, [840, 480])
        self.assertEqual(run.call_count, 2)

    def test_run_order_and_independent_failures(self):
        self.assertTrue(hasattr(main, 'run_once'), 'run logging is missing')
        for failure in (None, 'cleanup', 'persist', 'send', 'read'):
            events = []
            def action(name, value=None):
                def call(*args):
                    events.append(name)
                    if failure == name:
                        raise ValueError('operation failed')
                    return value
                return call
            async def send(*args):
                events.append('send')
                if failure == 'send':
                    raise main.NotificationError('delivery failed')
            with ExitStack() as stack:
                for name, replacement in {
                    'cleanup_logs': action('cleanup'), 'read_fibre_list': action('read', []),
                    'run': AsyncMock(return_value={'fibres': [], 'finished_at': '2026-09-10T10:00:00+07:00'}),
                    'build_html_message': lambda s: 'html', 'save_summary': action('save'),
                    'print_summary': action('print'), 'persist_run_log': action('persist'),
                    'send_notification': send,
                }.items():
                    stack.enter_context(patch.object(main, name, replacement))
                stack.enter_context(patch('builtins.print'))
                stack.enter_context(patch.object(main.logger, 'error'))
                self.assertEqual(main.run_once(), int(failure is not None))
            self.assertEqual(events, ['cleanup', 'read'] if failure == 'read' else
                             ['cleanup', 'read', 'save', 'print', 'persist', 'send'])

    def test_migration_serialization_and_retention(self):
        migration = Path('migrations/003_log_table.sql')
        self.assertTrue(migration.exists(), 'log migration is missing')
        from common.d1_logs import persist_run_log, cleanup_logs
        fibres = [{'modules': {source: ModuleResult(source, fid, status, {'text': "ไทย ' quoted"}).to_dict()
                               for source in ('airnet', 'npaw', 'onesense')}}
                  for fid, status in [('001', 'error'), ('002', 'N/A')]]
        with closing(sqlite3.connect(':memory:')) as db:
            db.executescript(migration.read_text())
            db.executescript(migration.read_text())
            def query(sql, params=None):
                return db.execute(sql, params or []).fetchall()
            with patch('common.d1_logs.query_d1', side_effect=query):
                persist_run_log({'finished_at': '2026-09-10T10:00:00+07:00', 'fibres': fibres})
                row = db.execute('SELECT * FROM log_table').fetchone()
                self.assertEqual(row[:2], (1, '2026-09-10T03:00:00.000000Z'))
                for index, source in enumerate(('airnet', 'npaw', 'onesense'), 2):
                    self.assertEqual(json.loads(row[index]), [f['modules'][source] for f in fibres])
                for stamp in ('2026-08-11T02:59:59.999999Z', '2026-08-11T03:00:00.000000Z'):
                    db.execute("INSERT INTO log_table(created_at,log_airnet,log_npaw,log_onesense) VALUES (?, '[]', '[]', '[]')", (stamp,))
                cleanup_logs(datetime(2026, 9, 10, 3, tzinfo=timezone.utc))
                self.assertEqual(db.execute('SELECT id FROM log_table ORDER BY id').fetchall(), [(1,), (3,)])


class AirnetRulesTests(unittest.TestCase):
    def test_status_rules(self):
        now = datetime(2026, 9, 10, 10, 30, 59, tzinfo=TZ_BKK)
        def rows(raw, count=5):
            return [{'Offline Time': raw}] * count
        self.assertEqual(_determine_status('oFfLiNe', [], now), 'abnormal')
        for raw, count, expected in [('10/09/2026 10:00', 5, 'abnormal'),
                                     ('10/09/2026 10:30', 5, 'abnormal'),
                                     ('10/09/2026 10:15', 4, 'normal'),
                                     ('10/09/2026 09:59', 5, 'normal'),
                                     ('10/09/2026 10:31', 5, 'normal'),
                                     ('bad', 5, 'normal'), ('', 5, 'normal')]:
            self.assertEqual(_determine_status('Online', rows(raw, count), now), expected)
        self.assertEqual(_determine_status('Unknown', [{}] * 5, now), 'normal')
