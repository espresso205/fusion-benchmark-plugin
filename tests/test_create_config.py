"""Recipient configuration must not silently weaken the adapter boundary."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/create_config.py'


class CreateConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='config-generator-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def generate(self, provider, *extra):
        target = self.root / (provider + '.json')
        result = subprocess.run([
            sys.executable, '-B', str(SCRIPT), '--output', str(target),
            '--provider', provider, '--model', 'example/model',
            '--cli-executable', sys.executable,
            '--questions-root', str(self.root / 'public'),
            '--private-cases', str(self.root / 'private/cases.json'),
            '--runs-root', str(self.root / 'runs'),
            '--private-runs-root', str(self.root / 'private-runs'),
            '--fusion-url', 'http://127.0.0.1:8000/mcp', *extra,
        ], capture_output=True, text=True, encoding='utf-8', timeout=10)
        return result, target

    def test_capable_providers_keep_strict_isolation_by_default(self):
        for provider in ('opencode', 'claude'):
            with self.subTest(provider=provider):
                result, target = self.generate(provider)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(target.read_text(encoding='utf-8'))['strict_tool_isolation'])

    def test_codex_requires_explicit_native_tool_permission_before_writing(self):
        result, target = self.generate('codex')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())
        self.assertIn('--allow-native-tools', result.stderr)
        self.assertIn('same-account', result.stderr)

    def test_explicit_permission_generates_codex_config_with_boundary_disabled(self):
        result, target = self.generate('codex', '--allow-native-tools')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(config['provider'], 'codex')
        self.assertFalse(config['strict_tool_isolation'])

    def test_scoring_mode_default_is_explicit_and_v3_cannot_align(self):
        result,target=self.generate('opencode')
        self.assertEqual(result.returncode,0,result.stderr)
        config=json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual((config['protocol'],config['coordinate_mode']),('fusion-step-adapted-v4.0','geometry'))
        result,target=self.generate('claude','--protocol','fusion-step-adapted-v3.0','--coordinate-mode','geometry')
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
