"""Native admission checks are byte/identity checks, never CAD health proof."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fusion_pipeline.native_contract import make_native_descriptor, validate_native_descriptor
from scheduler import Scheduler


class NativeContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identity = dict(run_id='run-1', case_id='Q01', attempt_id='attempt-1',
                             protocol_id='fusion-step-adapted-v4.0', cad_backend='solidworks',
                             cad_config_sha256='a' * 64)
        self.path = self.root / 'submission.SLDPRT'
        self.path.write_bytes(bytes.fromhex('d0cf11e0a1b11ae1') + b'synthetic fixture')

    def descriptor(self):
        return make_native_descriptor(self.path, identity=self.identity,
                                      configuration='Default', dependency_sha256='b' * 64)

    def test_configuration_and_all_identity_fields_are_bound(self):
        item = self.descriptor()
        self.assertTrue(validate_native_descriptor(item, identity=self.identity, path=self.path))
        for key in self.identity:
            with self.subTest(key=key):
                changed = {**item, key: 'different'}
                with self.assertRaises(ValueError):
                    validate_native_descriptor(changed, identity=self.identity, path=self.path)
        for configuration in (None, '', '  ', 3):
            with self.assertRaises(ValueError):
                validate_native_descriptor({**item, 'configuration': configuration})

    def test_rejects_unknown_fields_and_changed_native_bytes(self):
        item = self.descriptor()
        with self.assertRaises(ValueError):
            validate_native_descriptor({**item, 'other': True})
        self.path.write_bytes(b'pretend this is a SolidWorks file')
        with self.assertRaises(ValueError):
            validate_native_descriptor(item, path=self.path)

    def test_solidworks_opaque_byte_identity_is_not_native_recognition(self):
        # SW 34.4.1 saves two Parts with different leading bytes. Even bytes
        # without a native header may have a descriptor; Gateway save evidence
        # and independent COM reopen must establish actual Part recognition.
        for content in (bytes.fromhex('5433e12200000004'),
                        bytes.fromhex('228f7a5700000004'), b'not a native Part'):
            self.path.write_bytes(content)
            item = self.descriptor()
            self.assertEqual(item['sha256'], hashlib.sha256(content).hexdigest())
            self.assertNotIn('healthy', item)
            self.assertNotIn('format_verified', item)

    def test_fusion_configuration_is_explicitly_not_applicable(self):
        import zipfile
        path = self.root / 'submission.f3d'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('fixture', 'synthetic')
        identity = {**self.identity, 'cad_backend': 'fusion'}
        item = make_native_descriptor(path, identity=identity, dependency_sha256='b' * 64)
        self.assertIsNone(item['configuration'])
        with self.assertRaises(ValueError):
            validate_native_descriptor({**item, 'configuration': 'Default'})

    def test_hardlink_alias_and_wrong_filename_rejected(self):
        import os
        alias = self.root / 'alias.SLDPRT'
        os.link(self.path, alias)
        with self.assertRaises(ValueError):
            self.descriptor()
        alias.unlink()
        self.path.rename(alias)
        with self.assertRaises(ValueError):
            make_native_descriptor(alias, identity=self.identity, configuration='Default', dependency_sha256='b'*64)


class NativeSchedulerTests(NativeContractTests):
    def stage(self):
        self.scheduler = Scheduler(self.root / 'runs')
        self.scheduler.create_run('run-1', cad_backend='solidworks', cad_config_sha256='a'*64)
        task, drawing = self.root / 'TASK.md', self.root / 'drawing.dxf'
        task.write_text('public task', encoding='utf-8')
        drawing.write_bytes(b'synthetic dxf')
        self.scheduler.register_case('Q01', protocol_id=self.identity['protocol_id'], task=task, drawing=drawing)
        candidate = self.scheduler.run_dir / 'candidate/Q01'
        candidate.mkdir(parents=True)
        native = candidate / self.path.name
        native.write_bytes(self.path.read_bytes())
        deps = candidate / 'dependency_manifest.json'
        deps.write_text(json.dumps(dict(schema_version=1, case_id='Q01', attempt_id='attempt-1',
                                       inspection_method='unverified contract declaration', reviewer='test', dependencies=[])), encoding='utf-8')
        item = make_native_descriptor(native, identity=self.identity, configuration='Default',
                                      dependency_sha256=hashlib.sha256(deps.read_bytes()).hexdigest())
        (candidate / 'native_document.json').write_text(json.dumps(item), encoding='utf-8')
        for state in ('case_queued', 'modeling', 'artifact_staged'):
            self.scheduler.transition('Q01', state)
        return candidate, item

    def test_new_manifest_uses_native_document_and_reloads(self):
        candidate, item = self.stage()
        frozen = self.scheduler.freeze_candidate('Q01')
        data = json.loads(frozen.read_text(encoding='utf-8'))
        self.assertEqual(data['schema_version'], 3)
        self.assertNotIn('f3d', data['artifacts'])
        self.assertEqual(data['artifacts']['native_document'], item)
        reloaded = Scheduler.load(self.root / 'runs', 'run-1')
        self.assertTrue(reloaded.verify_artifact_manifest(frozen))
        (candidate / 'native_document.json').write_text(json.dumps({**item, 'configuration': 'Other'}), encoding='utf-8')
        self.assertFalse(reloaded.verify_artifact_manifest(frozen))

    def test_dependency_change_and_backend_drift_cannot_freeze(self):
        candidate, item = self.stage()
        (candidate / 'dependency_manifest.json').write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.scheduler.freeze_candidate('Q01')
        checkpoint = self.scheduler.run_dir / 'checkpoint.json'
        data = json.loads(checkpoint.read_text(encoding='utf-8'))
        data['cases']['Q01']['contract']['cad_backend'] = 'fusion'
        checkpoint.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            Scheduler.load(self.root / 'runs', 'run-1')

    def test_legacy_run_does_not_upgrade(self):
        old = Scheduler(self.root / 'old')
        old.create_run('old-1')
        before = (old.run_dir / 'checkpoint.json').read_bytes()
        Scheduler.load(self.root / 'old', 'old-1')
        self.assertEqual(before, (old.run_dir / 'checkpoint.json').read_bytes())
        self.assertEqual(json.loads(before)['schema_version'], 2)


if __name__ == '__main__':
    unittest.main()
