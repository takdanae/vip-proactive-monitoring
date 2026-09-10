import unittest
from unittest.mock import AsyncMock, patch

import main
from common.models import ModuleResult


class InputTests(unittest.TestCase):
    def test_invalid_input_stops_cli(self):
        with patch.object(main, 'cleanup_logs'), patch.object(main, 'setup_logging'), patch.object(main, 'read_fibre_list', side_effect=ValueError('invalid counts')), patch.object(main, 'run') as run, patch.object(main, 'save_summary') as save:
            with self.assertRaises(SystemExit) as error:
                main.main([])
            self.assertEqual(error.exception.code, 1)
            run.assert_not_called()
            save.assert_not_called()


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_flag_combinations(self):
        for mesh, playbox in [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (0, 3), (2, 3)]:
            fibre = dict(name='VIP customer', fibre_id='00123', mesh=mesh, playbox=playbox)
            mocks = {name: AsyncMock(return_value=ModuleResult(name, '00123', 'normal')) for name in ['airnet', 'onesense', 'npaw']}
            with patch.object(main.airnet, 'check', mocks['airnet']), patch.object(main.onesense, 'check', mocks['onesense']), patch.object(main.npaw, 'check', mocks['npaw']):
                summary = await main.run([fibre])
            entry = summary['fibres'][0]
            self.assertEqual(entry['name'], 'VIP customer')
            self.assertNotIn('first_name', entry)
            self.assertNotIn('last_name', entry)
            self.assertEqual((entry['mesh'], entry['playbox']), (mesh, playbox))
            self.assertEqual(entry['overall_status'], 'normal')
            mocks['airnet'].assert_awaited_once_with('00123')
            for name, flag in [('onesense', 'mesh'), ('npaw', 'playbox')]:
                if fibre[flag]:
                    mocks[name].assert_awaited_once_with('00123')
                else:
                    mocks[name].assert_not_called()
                    self.assertEqual(entry['modules'][name]['status'], 'N/A')
                    self.assertEqual(entry['modules'][name]['details']['message'], f'Skipped: {flag}=0')

    async def test_enabled_exception_is_error(self):
        with patch.object(main.airnet, 'check', AsyncMock(return_value=ModuleResult('airnet', '001', 'normal'))), patch.object(main.npaw, 'check', AsyncMock(side_effect=RuntimeError('failed'))):
            summary = await main.run([dict(name='VIP customer', fibre_id='001', mesh=0, playbox=1)])
        self.assertEqual(summary['fibres'][0]['overall_status'], 'error')
