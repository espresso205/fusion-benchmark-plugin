"""Real filesystem/ledger checks for the public-input handoff; no CAD/model calls."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from fusion_pipeline.runner import BASE, Trial, snapshot_evaluator
from scheduler.core import Scheduler


class RunnerInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'questions/Q01'
        self.source.mkdir(parents=True)
        (self.source / 'TASK.md').write_text('Build the dimensioned public drawings.', encoding='utf-8')
        from PIL import Image
        for index in (1, 2):
            Image.new('RGB', (64, 64), ('white', 'black')[index - 1]).save(self.source / f'view{index}.png')
        def asset(identifier, name, role, mime):
            content = (self.source / name).read_bytes()
            return dict(id=identifier, role=role, path=name, mime=mime, bytes=len(content),
                        sha256=hashlib.sha256(content).hexdigest(), display_name=name)
        self.manifest = dict(schema='fusion-public-inputs/1', case_id='Q01',
            task=asset('task', 'TASK.md', 'task', 'text/markdown'),
            attachments=[asset(f'v{i}', f'view{i}.png', 'view', 'image/png') for i in (2, 1)])
        (self.source / 'inputs.json').write_text(json.dumps(self.manifest), encoding='utf-8')
        self.trial = Trial.__new__(Trial)
        self.trial.config = dict(questions_root=str(self.source.parent), python=sys.executable)
        self.trial.unified_gateway = True
        self.trial.evaluator = BASE
        self.trial.ledger = Scheduler(self.root / 'ledger').create_run('test-inputs')
        self.row = dict(case_id='Q01')

    def test_manifest_is_frozen_copied_in_order_and_only_listed_files_are_exposed(self):
        (self.source / 'not-public.txt').write_text('not in manifest')
        self.trial.prepare_case_inputs(self.row)
        self.assertEqual(self.row['input_manifest'], self.manifest)
        workspace = Path(self.row['workspace'])
        self.assertFalse((workspace / 'drawing.dxf').exists())
        self.assertFalse((workspace / 'not-public.txt').exists())
        self.assertEqual((workspace / 'view2.png').read_bytes(), (self.source / 'view2.png').read_bytes())
        self.trial.verify_public_inputs(self.row)
        (self.source / 'view2.png').write_bytes(b'changed original after freezing')
        self.trial.verify_public_inputs(self.row)

    def test_both_frozen_and_working_copies_changed_are_rejected(self):
        self.trial.prepare_case_inputs(self.row)
        for directory in (self.trial.ledger / 'public/Q01', Path(self.row['workspace'])):
            (directory / 'TASK.md').write_text('same tampering in both files')
        with self.assertRaises(ValueError):
            self.trial.verify_public_inputs(self.row)

    def test_old_backend_rejects_manifest_before_registration(self):
        self.trial.unified_gateway = False
        with self.assertRaisesRegex(ValueError, 'unified_gateway'):
            self.trial.prepare_case_inputs(self.row)
        self.assertFalse((self.trial.ledger / 'public/Q01').exists())

    def test_frozen_scheduler_contains_input_contract_module(self):
        snapshot = snapshot_evaluator(BASE, self.root / 'snapshot')
        self.assertTrue((snapshot / 'fusion_pipeline/input_assets.py').is_file())
        self.assertTrue((snapshot / 'fusion_pipeline/__init__.py').is_file())

    def test_auto_report_uses_frozen_input_order_and_bound_hashes(self):
        from fusion_pipeline.reporting import frozen_report_inputs
        self.trial.prepare_case_inputs(self.row)
        path = frozen_report_inputs(dict(cases=[self.row]))
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        self.assertEqual(data['schema_version'], 'fusion-report-inputs/1')
        assets = data['cases'][0]['assets']
        self.assertEqual([a['label'] for a in assets], ['view2.png', 'view1.png'])
        for item, original in zip(assets, self.manifest['attachments']):
            self.assertEqual(item['sha256'], original['sha256'])
            self.assertEqual(hashlib.sha256((Path(path).parent / item['path']).read_bytes()).hexdigest(), item['sha256'])


if __name__ == '__main__':
    unittest.main()
