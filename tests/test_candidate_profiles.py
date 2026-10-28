import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class CandidateProfileTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.binary = self.root / 'candidate.exe'
        self.binary.write_bytes(b'fixture-only')

    def profile(self, provider='claude'):
        value = {'provider': provider, 'model': 'fixture-model', 'cli_executable': str(self.binary),
                 'strict_tool_isolation': provider != 'codex', 'reasoning': {'mode': 'cli_default'}}
        if provider == 'codex':
            value['native_tools_authorization'] = {'source': 'synthetic-test-only', 'scope': 'trusted same-account execution; not an OS sandbox'}
        return value

    def test_selected_profile_never_inherits_base_launch_fields(self):
        from fusion_pipeline.candidate_profiles import resolve_candidate_profile
        for provider in ('claude', 'codex', 'opencode'):
            with self.subTest(provider=provider):
                config = {'provider': 'claude', 'model': 'old', 'cli_executable': 'wrong.exe',
                          'credential_settings': 'must-not-be-read', 'expected_actual_model': 'old-actual',
                          'candidate_profiles': {'selected': self.profile(provider)}}
                before = copy.deepcopy(config)
                with patch('subprocess.run', side_effect=AssertionError('static parser launched process')):
                    selected = resolve_candidate_profile(config, profile_id='selected')
                self.assertEqual(selected['provider'], provider)
                self.assertEqual(selected['cli_executable'], str(self.binary))
                self.assertIsNone(selected.get('credential_settings'))
                self.assertIsNone(selected.get('expected_actual_model'))
                self.assertEqual(config, before)

    def test_invalid_explicit_profiles_fail(self):
        from fusion_pipeline.candidate_profiles import resolve_candidate_profile
        invalid = [('provider', 'other', 'candidate_provider_unsupported'), ('model', '', 'candidate_model_required'),
                   ('reasoning', None, 'reasoning_invalid'), ('strict_tool_isolation', 'true', 'candidate_isolation_invalid'),
                   ('cli_executable', str(self.root / 'absent.exe'), 'candidate_executable_unavailable')]
        for field, value, code in invalid:
            profile = self.profile()
            profile[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, code):
                resolve_candidate_profile({'candidate_profiles': {'selected': profile}}, profile_id='selected')
        with self.assertRaisesRegex(ValueError, 'candidate_profile_unknown'):
            resolve_candidate_profile({'candidate_profiles': {}}, profile_id='absent')
        with self.assertRaisesRegex(ValueError, 'candidate_isolation_unsupported'):
            profile = self.profile('codex')
            profile['strict_tool_isolation'] = True
            resolve_candidate_profile({'candidate_profiles': {'selected': profile}}, profile_id='selected')

    def test_explicit_invalid_path_has_zero_path_fallback(self):
        from fusion_pipeline.adapters import executable
        with patch('fusion_pipeline.adapters.shutil.which') as lookup:
            with self.assertRaises(FileNotFoundError):
                executable('claude', str(self.root / 'absent.exe'))
            lookup.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'unsupported provider'):
            executable('other', str(self.binary))

    def test_profile_requires_explicit_authorization_for_native_tools(self):
        from fusion_pipeline.candidate_profiles import resolve_candidate_profile
        profile = self.profile('codex')
        profile.pop('native_tools_authorization')
        with self.assertRaisesRegex(ValueError, 'native_tools_authorization_required'):
            resolve_candidate_profile({'candidate_profiles': {'selected': profile}}, profile_id='selected')

    def test_management_selection_conflict_and_provider_switch(self):
        from fusion_pipeline.candidate_profiles import apply_candidate_selection
        config = {'provider': 'claude', 'model': 'old', 'cli_executable': str(self.binary),
                  'candidate_profiles': {'selected': self.profile('codex')}}
        with self.assertRaisesRegex(ValueError, 'candidate_selection_conflict'):
            apply_candidate_selection(config, {'candidate_profile': 'selected', 'model': 'override'})
        with self.assertRaisesRegex(ValueError, 'candidate_profile_required_for_provider_switch'):
            apply_candidate_selection(config, {'provider': 'codex', 'model': 'fixture-model'})
        updated = apply_candidate_selection(config, {'candidate_profile': 'selected'})
        self.assertEqual(updated['provider'], 'codex')
        self.assertEqual(config['provider'], 'claude')

    def test_existing_profile_rejects_partial_model_override(self):
        from fusion_pipeline.candidate_profiles import apply_candidate_selection
        config = {'candidate_profile': 'selected', 'candidate_profiles': {'selected': self.profile()}}
        with self.assertRaisesRegex(ValueError, 'candidate_selection_conflict'):
            apply_candidate_selection(config, {'model': 'unbound-model'})

    def test_profile_file_schema_and_safe_source_references(self):
        from fusion_pipeline.candidate_profiles import load_candidate_profiles, resolve_candidate_profile
        path = self.root / 'profiles.json'
        path.write_text(json.dumps({'schema': 'candidate-profiles/1', 'candidate_profiles': {'selected': self.profile()}}))
        self.assertIn('selected', load_candidate_profiles(path)['candidate_profiles'])
        path.write_text(json.dumps({'schema': 'candidate-profiles/2', 'candidate_profiles': {}}))
        with self.assertRaisesRegex(ValueError, 'candidate_profiles_invalid'):
            load_candidate_profiles(path)
        profile = self.profile()
        profile['credential_settings'] = {'api_key': 'do-not-store'}
        with self.assertRaisesRegex(ValueError, 'candidate_profile_invalid'):
            resolve_candidate_profile({'candidate_profiles': {'selected': profile}}, profile_id='selected')

    def test_freeze_does_not_read_credential_files_or_claim_executable_observed(self):
        from fusion_pipeline.candidate_profiles import freeze_candidate_profile, resolve_candidate_profile
        profile = self.profile()
        credential = self.root / 'secret.json'
        credential.write_text('{"token":"fixture-not-to-copy"}')
        profile['credential_settings'] = str(credential)
        selected = resolve_candidate_profile({'candidate_profiles': {'selected': profile}}, profile_id='selected')
        frozen = freeze_candidate_profile(selected, self.root / 'frozen')
        self.assertNotIn('fixture-not-to-copy', json.dumps(frozen))
        self.assertIsNone(frozen['resolved_executable'])
        self.assertIsNone(frozen['cli_available'])

    def test_reasoning_override_changes_final_hash_preserving_base(self):
        from fusion_pipeline.candidate_profiles import resolve_candidate_profile
        config = {'candidate_profiles': {'selected': self.profile()}}
        base = resolve_candidate_profile(config, profile_id='selected')
        override = {'mode': 'explicit', 'control': 'effort', 'value': 'high'}
        final = resolve_candidate_profile(config, profile_id='selected', reasoning_override=override)
        self.assertNotEqual(base['candidate_profile_sha256'], final['candidate_profile_sha256'])
        self.assertEqual(base['candidate_profile_sha256'], final['base_candidate_profile_sha256'])
        self.assertEqual(config['candidate_profiles']['selected']['reasoning'], {'mode': 'cli_default'})

    def test_saved_reasoning_override_survives_parser_roundtrip_and_explicit_clear(self):
        from fusion_pipeline.candidate_profiles import apply_candidate_selection, resolve_candidate_profile
        config = {'candidate_profiles': {'selected': self.profile(), 'other': self.profile('opencode')}}
        high = {'mode': 'explicit', 'control': 'effort', 'value': 'high'}
        derived = apply_candidate_selection(config, {'candidate_profile': 'selected', 'reasoning': high})
        for reparsed in (resolve_candidate_profile(derived), apply_candidate_selection(derived, {})):
            self.assertEqual(reparsed['reasoning'], high)
            self.assertEqual(reparsed['candidate_profile_sha256'], derived['candidate_profile_sha256'])
            self.assertEqual(reparsed['base_candidate_profile_sha256'], derived['base_candidate_profile_sha256'])
        cleared = apply_candidate_selection(derived, {'reasoning': {'mode': 'cli_default'}})
        self.assertEqual(resolve_candidate_profile(cleared)['reasoning'], {'mode': 'cli_default'})
        switched = apply_candidate_selection(derived, {'candidate_profile': 'other'})
        self.assertEqual(resolve_candidate_profile(switched)['reasoning'], {'mode': 'cli_default'})
        self.assertNotIn('reasoning_override', switched)
        scattered = {**config, 'candidate_profile': 'selected', 'reasoning': high}
        self.assertEqual(resolve_candidate_profile(scattered)['reasoning'], {'mode': 'cli_default'})
        derived['candidate_profiles']['selected']['model'] = 'changed-fixture-model'
        with self.assertRaisesRegex(ValueError, 'candidate_profile_override_mismatch'):
            resolve_candidate_profile(derived)

    def test_legacy_default_and_exclusive_snapshot(self):
        from fusion_pipeline.candidate_profiles import freeze_candidate_profile, resolve_candidate_profile
        selected = resolve_candidate_profile({'provider': 'claude', 'model': 'legacy', 'cli_executable': str(self.binary)})
        self.assertEqual(selected['candidate_profile_id'], 'legacy_unprofiled')
        self.assertEqual(selected['reasoning_origin'], 'legacy_default')
        output = self.root / 'frozen'
        manifest = freeze_candidate_profile(selected, output)
        self.assertEqual(manifest['observed_model_evidence'], None)
        self.assertEqual(json.loads((output / 'candidate-profile.json').read_text())['candidate_profile_sha256'], selected['candidate_profile_sha256'])
        with self.assertRaises(FileExistsError):
            freeze_candidate_profile(selected, output)


if __name__ == '__main__':
    unittest.main()
