"""Real Python subprocess regressions for localized Windows pipe/file output."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fusion_pipeline import runner


def legacy_environment(io_encoding=None):
    environment = dict(os.environ)
    environment.pop('PYTHONIOENCODING', None)
    environment.pop('PYTHONUTF8', None)
    environment['PYTHONUTF8'] = '0'
    if io_encoding is not None:
        environment['PYTHONIOENCODING'] = io_encoding
    environment['BENCHMARK_ENCODING_TEST'] = 'inherited-runtime-setting'
    return environment


class RunnerEncodingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='runner-encoding-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / '中文 路径'
        self.root.mkdir()
        self.trial = runner.Trial.__new__(runner.Trial)
        self.trial.config = {'python': sys.executable}
        self.trial.evaluator = runner.BASE

    def test_scheduler_returns_real_chinese_path_without_inherited_utf8(self):
        # Removing the child's explicit encoding contract must break the path round trip.
        for index, encoding in enumerate((None, 'gbk:strict')):
            with self.subTest(inherited_io_encoding=encoding), patch.dict(os.environ, legacy_environment(encoding), clear=True):
                root = self.root / str(index)
                try:
                    result = self.trial.scheduler('create-run', root, 'test-run',
                        '--cad-backend', 'solidworks', '--cad-config-sha256', 'a' * 64)
                except Exception as error:
                    self.fail('Scheduler could not return its Unicode output: ' + repr(error))
                self.assertEqual(Path(result), root / 'test-run')
                checkpoint = json.loads((Path(result) / 'checkpoint.json').read_text(encoding='utf-8'))
                self.assertEqual(checkpoint['schema_version'], 3)
                self.assertEqual(os.environ.get('PYTHONIOENCODING'), encoding)

    def test_scheduler_preserves_unicode_error_details_with_inherited_gbk(self):
        root = self.root / '重复 创建'
        (root / 'test-run').mkdir(parents=True)
        with patch.dict(os.environ, legacy_environment('gbk:strict'), clear=True):
            with self.assertRaises(RuntimeError) as caught:
                self.trial.scheduler('create-run', root, 'test-run')
        self.assertIn('重复 创建', str(caught.exception))

    def test_evaluation_log_is_utf8_and_inherits_other_runtime_settings(self):
        # A real child writes stdout/stderr; scoring publication is outside this encoding regression.
        from tests.test_fusion_runner_safety import RunnerSafetyTests
        for index, encoding in enumerate((None, 'gbk:strict')):
            with self.subTest(inherited_io_encoding=encoding):
                fixture = RunnerSafetyTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                trial = fixture.trial
                fixture.case()
                trial.private = self.root / ('评测 输出 ' + str(index))
                trial.private.mkdir()
                trial.config['python'] = sys.executable
                script = ("import os,sys\n"
                          "print('中文评测日志')\n"
                          "print('中文错误详情',file=sys.stderr)\n"
                          "print(os.environ['BENCHMARK_ENCODING_TEST'])\n")
                (trial.evaluator / 'evaluate.py').write_text(script, encoding='utf-8')
                manifest = trial.evaluator / 'PACKAGE_MANIFEST.json'
                files = json.loads(manifest.read_text(encoding='utf-8'))['files']
                payload = (trial.evaluator / 'evaluate.py').read_bytes()
                files['evaluate.py'] = {'bytes': len(payload), 'sha256': runner.sha(trial.evaluator / 'evaluate.py')}
                runner.atomic(manifest, {'files': files})
                trial.state['evaluator_manifest_sha256'] = runner.sha(manifest)
                summary = {'batch_mean': None, 'cases': [{'case': 'Q01', 'final_score': None, 'status': 'encoding_probe'}]}
                with (patch.dict(os.environ, legacy_environment(encoding), clear=True),
                      patch.object(runner, 'validate_evaluation', return_value=summary)):
                    trial.evaluate()
                    self.assertEqual(os.environ.get('PYTHONIOENCODING'), encoding)
                self.assertEqual(trial.state['evaluation']['returncode'], 0)
                try:
                    log = (trial.private / 'evaluation.log').read_text(encoding='utf-8')
                except UnicodeDecodeError as error:
                    self.fail('Evaluation child did not write UTF-8: ' + repr(error))
                self.assertIn('中文评测日志', log)
                self.assertIn('中文错误详情', log)
                self.assertIn('inherited-runtime-setting', log)


if __name__ == '__main__':
    unittest.main()
