import unittest


class VisualCheckTests(unittest.TestCase):
    def test_gateway_reads_must_match_both_images_identity_and_order(self):
        from fusion_pipeline.visual_check import assess_reads
        expected = [dict(id='a', index=0, source_sha256='a' * 64, returned_sha256='a' * 64),
                    dict(id='b', index=1, source_sha256='b' * 64, returned_sha256='b' * 64)]
        reads = [dict(image=item) for item in expected]
        self.assertTrue(assess_reads(reads, expected))
        self.assertFalse(assess_reads([reads[0], reads[0]], expected))
        self.assertFalse(assess_reads(reads[::-1], expected))
        self.assertFalse(assess_reads([reads[0], dict(image=dict(expected[1], returned_sha256='c' * 64))], expected))

    def test_answer_requires_each_image_and_correct_order(self):
        from fusion_pipeline.visual_check import assess_answer
        expected = [dict(id='asset-a', circles=2, triangles=3), dict(id='asset-b', circles=1, triangles=4)]
        self.assertTrue(assess_answer('{"observations":' + __import__('json').dumps(expected) + '}', expected))
        self.assertFalse(assess_answer('{"observations":' + __import__('json').dumps(expected[:1]) + '}', expected))
        self.assertFalse(assess_answer('{"observations":' + __import__('json').dumps(expected[::-1]) + '}', expected))
        self.assertFalse(assess_answer('I could not view these images.', expected))

    def test_visual_evidence_is_not_attested_by_cli_probe(self):
        from fusion_pipeline.adapters import capabilities
        self.assertTrue(all(value['visual_input_verification'] == 'requires_explicit_visual_check'
                            for value in capabilities().values()))
