import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]


class InputContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('gt.step', 'candidate.step', 'annotations.json'):
            (self.root/name).write_text('{}', encoding='utf-8')
        self.row = {'case': 'Q01', **{k: str(self.root/v) for k,v in {
            'gt': 'gt.step', 'candidate': 'candidate.step', 'annotations': 'annotations.json'}.items()},
            'expected_hashes': {k:'44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a' for k in ('gt','candidate','annotations')}}

    def validate(self, rows):
        spec = importlib.util.spec_from_file_location('scoring_cli', BASE/'evaluate.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.validate_cases({'cases': rows})

    def test_valid_input_is_bound_to_file_contents(self):
        rows = self.validate([self.row])
        self.assertEqual(rows[0]['case'], 'Q01')

    def test_stale_hash_rejected_before_worker(self):
        (self.root/'candidate.step').write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.validate([self.row])

    def test_duplicate_case_rejected_instead_of_overwritten(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.validate([self.row, self.row])

    def test_case_id_cannot_escape_output_directory(self):
        self.row['case'] = '../escape'
        with self.assertRaisesRegex(ValueError, 'case'):
            self.validate([self.row])

    def test_missing_expected_hashes_cannot_be_silently_refrozen(self):
        del self.row['expected_hashes']
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.validate([self.row])

    def test_overflowing_json_number_is_rejected(self):
        from src.io_utils import strict_json
        p=self.root/'overflow.json';p.write_text('{"tolerance":1e400}')
        with self.assertRaises(ValueError):strict_json(p)


if __name__ == '__main__':
    unittest.main()
