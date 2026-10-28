import copy
import unittest


class ReasoningTests(unittest.TestCase):
    def candidate(self, provider='codex', control='effort', value='high'):
        return {'provider': provider, 'model': 'fixture-model', 'cli_version': 'fixture-1',
                'reasoning': {'mode': 'explicit', 'control': control, 'value': value}}

    def support(self, provider='codex', control='effort'):
        return {'provider': provider, 'model': 'fixture-model', 'cli_version': 'fixture-1', 'status': 'verified',
                'controls': {control: ['low', 'high']}, 'evidence_sha256': '0' * 64,
                'variant_options': {'high': {'reasoningEffort': 'high'}}}

    def test_three_native_mappings_and_default(self):
        from fusion_pipeline.reasoning import resolve_reasoning
        for provider, control, expected in [('codex', 'effort', ['-c', 'model_reasoning_effort="high"']),
                                            ('claude', 'effort', ['--effort', 'high']),
                                            ('opencode', 'variant', ['--variant', 'high'])]:
            with self.subTest(provider=provider):
                resolved = resolve_reasoning(self.candidate(provider, control), self.support(provider, control))
                self.assertEqual(resolved['native_parameters'], expected)
                self.assertIsNone(resolved['observed_reasoning'])
                self.assertEqual(resolved['reasoning_verification_status'], 'parameter_emitted_only')
        default = resolve_reasoning({'provider': 'codex', 'model': 'fixture-model', 'reasoning': {'mode': 'cli_default'}}, {})
        self.assertEqual(default['native_parameters'], [])
        self.assertIsNone(default['resolved'])

    def test_unsupported_and_unverified_or_stale_support_rejected(self):
        from fusion_pipeline.reasoning import resolve_reasoning
        for field, value, code in [('controls', {'effort': ['low']}, 'reasoning_unsupported'),
                                   ('status', 'not_verified', 'reasoning_support_unverified'),
                                   ('model', 'other', 'reasoning_support_unverified'),
                                   ('cli_version', 'old', 'reasoning_support_unverified'),
                                   ('evidence_sha256', 'bad', 'reasoning_support_unverified')]:
            evidence = self.support()
            evidence[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, code):
                resolve_reasoning(self.candidate(), evidence)

    def test_invalid_shapes_and_provider_control_mismatch(self):
        from fusion_pipeline.reasoning import normalize_reasoning, resolve_reasoning
        for value in (None, [], {}, {'mode': 'explicit'}, {'mode': 'cli_default', 'value': 'high'},
                      {'mode': 'explicit', 'control': 'effort', 'value': ''},
                      {'mode': 'explicit', 'control': 'effort', 'value': True}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'reasoning_invalid'):
                normalize_reasoning(value)
        with self.assertRaisesRegex(ValueError, 'reasoning_unsupported'):
            resolve_reasoning(self.candidate('codex', 'variant'), self.support('codex', 'variant'))

    def test_variant_freezes_options_but_rejects_permissions(self):
        from fusion_pipeline.reasoning import resolve_reasoning
        support = self.support('opencode', 'variant')
        support['variant_options']['high']['temperature'] = 0.5
        before = copy.deepcopy(support)
        resolved = resolve_reasoning(self.candidate('opencode', 'variant'), support)
        self.assertEqual(resolved['variant_resolved_options'], support['variant_options']['high'])
        self.assertEqual(len(resolved['variant_resolved_options_sha256']), 64)
        self.assertFalse(resolved['reasoning_only_comparison'])
        self.assertEqual(support, before)
        support['variant_options']['high']['mcp'] = {'secret': True}
        with self.assertRaisesRegex(ValueError, 'reasoning_variant_options_invalid'):
            resolve_reasoning(self.candidate('opencode', 'variant'), support)

    def test_observed_evidence_is_separate_and_mismatch_fails(self):
        from fusion_pipeline.reasoning import observe_reasoning, resolve_reasoning
        resolved = resolve_reasoning(self.candidate(), self.support())
        observed = observe_reasoning(resolved, {'control': 'effort', 'value': 'low'}, source='fixture_event')
        self.assertEqual(observed['reasoning_verification_status'], 'mismatch')
        self.assertEqual(observed['observed_reasoning']['value'], 'low')
        self.assertIsNone(resolved['observed_reasoning'])


if __name__ == '__main__':
    unittest.main()
