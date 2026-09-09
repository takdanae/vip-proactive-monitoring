import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
from common.models import ModuleResult


class InputTests(unittest.TestCase):
    def test_validation(self):
        valid = {'name': 'VIP customer', 'fibre_id': '00123', 'mesh': 0, 'playbox': 1}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fibres.json'
            with self.assertRaisesRegex(ValueError, 'Cannot load'):
                main.read_fibre_list(path)
            path.write_text(json.dumps([valid]))
            self.assertEqual(main.read_fibre_list(path), [valid])
            cases = [([], 'non-empty'), ({}, 'non-empty'), ([1], 'object'),
                     ([{}, valid], 'missing'), ([valid, valid], 'duplicate')]
            for field in ['mesh', 'playbox']:
                for value in [None, True, False, 1.0, '1', 2, -1]:
                    cases.append(([dict(valid, **{field: value})], field))
            for value in [None, 123, '', ' 00123']:
                cases.append(([dict(valid, fibre_id=value)], 'fibre_id'))
            for value in [None, 123, '', ' VIP customer']:
                cases.append(([dict(valid, name=value)], 'name'))
            for records, message in cases:
                with self.subTest(records=records):
                    path.write_text(json.dumps(records))
                    with self.assertRaisesRegex(ValueError, message):
                        main.read_fibre_list(path)
            path.write_text('{')
            with self.assertRaisesRegex(ValueError, 'Cannot load'):
                main.read_fibre_list(path)

    def test_invalid_input_stops_cli(self):
        with patch.object(main, 'setup_logging'), patch.object(main, 'read_fibre_list', side_effect=ValueError('invalid flags')), patch.object(main, 'run') as run:
            with self.assertRaises(SystemExit):
                main.main()
            run.assert_not_called()


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_flag_combinations(self):
        for mesh, playbox in [(0, 0), (0, 1), (1, 0), (1, 1)]:
            fibre = dict(name='VIP customer', fibre_id='00123', mesh=mesh, playbox=playbox)
            mocks = {name: AsyncMock(return_value=ModuleResult(name, '00123', 'normal')) for name in ['airnet', 'onesense', 'npaw']}
            with patch.object(main.airnet, 'check', mocks['airnet']), patch.object(main.onesense, 'check', mocks['onesense']), patch.object(main.npaw, 'check', mocks['npaw']):
                summary = await main.run([fibre])
            entry = summary['fibres'][0]
            self.assertEqual(entry['name'], 'VIP customer')
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
