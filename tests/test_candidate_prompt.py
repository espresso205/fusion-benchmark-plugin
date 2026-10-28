import unittest
from fusion_pipeline import candidate_prompt


class CandidatePromptTests(unittest.TestCase):
    def test_no_optional_skill_prompt_preserves_required_solidworks_protocol(self):
        public = {'skills': [], 'mcp_servers': ['cad_native']}
        contract = {'cad_backend': 'solidworks', 'workspace': 'public', 'max_tool_calls': 200}
        prompt = candidate_prompt.build_candidate_prompt({'case_id': 'Q01'}, contract, public)
        for word in ['validate_parameter_plan', 'control_operations', 'parameter_plan_token',
                     'source_ref', 'unit', 'save_document', 'configuration', 'unknown']:
            self.assertIn(word, prompt)
        for forbidden in ['cad_skill', 'fusion_skill', 'read_skill(', 'Optional strategy', 'registry']:
            self.assertNotIn(forbidden, prompt)
        self.assertEqual(len(candidate_prompt.common_protocol_sha256()), 64)

    def test_enabled_skills_requested_in_order_without_private_contract_leaks(self):
        public = {'skills': [{'id': 'second'}, {'id': 'first'}], 'mcp_servers': ['cad_native']}
        prompt = candidate_prompt.build_candidate_prompt({}, {'cad_backend': 'fusion',
            'capability_manifest': 'PRIVATE-MANIFEST', 'connection_file': 'PRIVATE-CONNECTION'}, public)
        self.assertLess(prompt.index('second'), prompt.index('first'))
        self.assertIn('read_skill', prompt)
        self.assertNotIn('PRIVATE-', prompt)

    def test_no_cad_prompt_has_no_native_tool_or_submission_instructions(self):
        text = candidate_prompt.build_gateway_instructions({'cad_backend': 'solidworks'},
            {'skills': [], 'mcp_servers': [], 'study_kind': 'negative_control_no_cad'})
        self.assertNotIn('cad_call', text)
        self.assertNotIn('validate_parameter_plan', text)
        self.assertNotIn('save_document', text)
        self.assertIn('negative_control_no_cad', text)
