import csv
import json
from pathlib import Path
import tempfile
import unittest

from src.contract import aggregate_case, MODULE_NAMES, PROTOCOL_ID
from src.reporting import report


class ReportingTests(unittest.TestCase):
    def test_rejected_module_score_is_blank_in_comparison_but_kept_as_diagnostic(self):
        hashes = {'gt':'a'*64, 'candidate':'b'*64, 'annotations':'c'*64}
        modules = {name:{'schema_version':'2.0','protocol_id':PROTOCOL_ID,'case':'Q01',
            'input_hashes':dict(hashes),'status':'ok','score':1.,
            'quality':{'measurement_valid':True,'reference_validated':True},
            'raw':{},'timing':{'compute_s':0.1}} for name in MODULE_NAMES}
        modules['shape']['score'] = 0.75
        modules['shape']['quality']['measurement_valid'] = False
        final = aggregate_case('Q01', modules, hashes)

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            results = output/'Q01'/'results'
            results.mkdir(parents=True)
            for name, row in modules.items():
                (results/f'{name}.json').write_text(json.dumps(row), encoding='utf-8')
            manifest = {'cases':[{'case':'Q01','historical_results':None}],
                'workers':1,'timeout_per_case_s':60}
            summary = {'cases':[final], 'wall_s':1., 'code_unchanged':True,
                'original_inputs_unchanged':True}

            report(output, manifest, summary)

            with (output/'comparison.csv').open(encoding='utf-8-sig', newline='') as stream:
                shape = next(row for row in csv.DictReader(stream) if row['module']=='shape')
            self.assertEqual(shape['new_score'], '')
            self.assertEqual(shape['new_diagnostic_score'], '0.75')
            self.assertEqual(shape['delta'], '')


if __name__ == '__main__': unittest.main()
