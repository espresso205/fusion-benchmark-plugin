import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

class UpdateInstallerTests(unittest.TestCase):
    def load(self):
        path=Path(__file__).resolve().parents[1]/'scripts/assembly_update/apply_update.py'
        spec=importlib.util.spec_from_file_location('assembly_update',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

    def fixture(self,root):
        package=root/'package';(package/'payload').mkdir(parents=True)
        target=root/'target';target.mkdir();(target/'old.py').write_bytes(b'old')
        (package/'payload/old.py').write_bytes(b'new');(package/'payload/added.py').write_bytes(b'added')
        digest=lambda b:hashlib.sha256(b).hexdigest()
        manifest={'files':{'old.py':{'before_sha256':digest(b'old'),'sha256':digest(b'new')},
                           'added.py':{'before_sha256':None,'sha256':digest(b'added')}}}
        (package/'update-manifest.json').write_text(json.dumps(manifest));return package,target

    def test_apply_and_rollback_restore_original_file_set(self):
        module=self.load()
        with tempfile.TemporaryDirectory() as temp:
            package,target=self.fixture(Path(temp))
            backup=module.install(package,target)
            self.assertEqual((target/'old.py').read_bytes(),b'new')
            self.assertEqual((target/'added.py').read_bytes(),b'added')
            module.rollback(target,backup)
            self.assertEqual((target/'old.py').read_bytes(),b'old')
            self.assertFalse((target/'added.py').exists())

    def test_modified_installation_rejected_before_any_write(self):
        module=self.load()
        with tempfile.TemporaryDirectory() as temp:
            package,target=self.fixture(Path(temp));(target/'old.py').write_bytes(b'user change')
            with self.assertRaises(ValueError):module.install(package,target)
            self.assertEqual((target/'old.py').read_bytes(),b'user change')
            self.assertFalse((target/'added.py').exists())

    def test_path_escape_is_rejected(self):
        module=self.load()
        with tempfile.TemporaryDirectory() as temp:
            package,target=self.fixture(Path(temp));m=json.loads((package/'update-manifest.json').read_text())
            m['files']['../escape']=m['files'].pop('added.py');(package/'update-manifest.json').write_text(json.dumps(m))
            with self.assertRaises(ValueError):module.install(package,target)
            self.assertEqual((target/'old.py').read_bytes(),b'old')

    def test_journal_failure_after_replacement_restores_original(self):
        module=self.load();self.assertTrue(hasattr(module,'save_journal'),'durable journal API required')
        with tempfile.TemporaryDirectory() as temp:
            package,target=self.fixture(Path(temp));save=module.save_journal;failed=False
            def fail_once(path,data):
                nonlocal failed
                if not failed and (target/'old.py').read_bytes()==b'new':
                    failed=True;raise OSError('journal disk failure')
                return save(path,data)
            with patch.object(module,'save_journal',side_effect=fail_once):
                with self.assertRaisesRegex(OSError,'journal disk failure'):module.install(package,target)
            self.assertEqual((target/'old.py').read_bytes(),b'old')
            self.assertFalse((target/'added.py').exists())
            journal=json.loads(next(Path(temp).glob('*-before-*/restore.json')).read_text())
            self.assertEqual(journal['status'],'restored')

    def test_process_interruption_after_replace_recovers_from_write_ahead(self):
        module=self.load()
        with tempfile.TemporaryDirectory() as temp:
            package,target=self.fixture(Path(temp));replace=module.replace_file
            def crash(source,dest):
                replace(source,dest)
                if Path(dest).is_relative_to(target):raise SystemExit('power loss')
            with patch.object(module,'replace_file',side_effect=crash):
                with self.assertRaises(SystemExit):module.install(package,target)
            backup=next(Path(temp).glob('*-before-*'));module.rollback(target,backup)
            self.assertEqual((target/'old.py').read_bytes(),b'old')

    def test_interrupted_rollback_can_resume_and_repeat(self):
        module=self.load()
        with tempfile.TemporaryDirectory() as temp:
            package,target=self.fixture(Path(temp));backup=module.install(package,target);replace=module.replace_file
            def crash(source,dest):replace(source,dest);raise SystemExit('rollback interruption')
            with patch.object(module,'replace_file',side_effect=crash):
                with self.assertRaises(SystemExit):module.rollback(target,backup)
            module.rollback(target,backup);module.rollback(target,backup)
            self.assertEqual((target/'old.py').read_bytes(),b'old');self.assertFalse((target/'added.py').exists())

    def test_file_replacement_error_restores_before_reporting_failure(self):
        module=self.load()
        with tempfile.TemporaryDirectory() as temp:
            package,target=self.fixture(Path(temp));replace=module.replace_file
            def fail(source,dest):
                if Path(source).is_relative_to(package) and Path(dest).name=='added.py':raise OSError('replace failed')
                return replace(source,dest)
            with patch.object(module,'replace_file',side_effect=fail):
                with self.assertRaisesRegex(OSError,'replace failed'):module.install(package,target)
            self.assertEqual((target/'old.py').read_bytes(),b'old');self.assertFalse((target/'added.py').exists())

    def test_failed_atomic_journal_publish_keeps_previous_valid_json(self):
        module=self.load();self.assertTrue(hasattr(module,'save_journal'))
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'restore.json';module.save_journal(path,{'status':'installing'})
            with patch.object(module,'durable_replace',side_effect=OSError('journal rename failed')):
                with self.assertRaises(OSError):module.save_journal(path,{'status':'installed'})
            self.assertEqual(json.loads(path.read_text()),{'status':'installing'})

if __name__=='__main__':unittest.main()
