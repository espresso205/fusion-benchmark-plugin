"""Offline reader contract tests; fixtures contain JSON only, never geometry."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.contract import aggregate_case, MODULE_NAMES

try:
    from analysis.result_reader import read_source
except ImportError:
    read_source = None

PROTOCOL = 'fusion-step-adapted-v3.0'
WEIGHTS = dict(shape=.3, dimension=.25, distance=.15, mass=.1, face_match=.1, section=.1)
HASHES = dict(candidate='a' * 64, gt='b' * 64, annotations='c' * 64)


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, allow_nan=False), encoding='utf-8')


def modules(case='Q01'):
    return {name: dict(schema_version='3.0', protocol_id=PROTOCOL, case=case,
                       angle=name, input_hashes=dict(HASHES), status='ok', score=1.,
                       quality=dict(measurement_valid=True, candidate_valid=True),
                       raw=dict(candidate=dict(healthy=True), gt=dict(healthy=True))
                       if name == 'validity' else {}, timing=dict(compute_s=.01))
            for name in MODULE_NAMES}


def v3_fixture(root, rows=None, postprocess=None):
    rows = modules() if rows is None else copy.deepcopy(rows)
    manifest = dict(schema_version='3.0', protocol_id=PROTOCOL,
                    cases=[dict(case='Q01', expected_hashes=dict(HASHES))],
                    weights=dict(protocol_id=PROTOCOL, weights=WEIGHTS),
                    code_hashes={'evaluate.py': 'd' * 64}, ranking_eligible=False)
    write_json(root / 'manifest.json', manifest)
    digest = hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest()
    for name, row in rows.items():
        row['run_manifest_sha256'] = digest
        write_json(root / 'Q01' / 'results' / (name + '.json'), row)
    assessment = aggregate_case('Q01', rows, HASHES, protocol_id=PROTOCOL)
    assessment.update(run_manifest_sha256=digest,
                      execution=dict(status='completed', returncode=0, timed_out=False))
    if postprocess:
        postprocess(assessment)
    write_json(root / 'Q01' / 'aggregate.json', assessment)
    summary = dict(schema_version='3.0', protocol_id=PROTOCOL,
                   run_manifest_sha256=digest, cases=[assessment],
                   code_unchanged=True, original_inputs_unchanged=True,
                   participating_case_count=int(assessment['participates_in_scoring']),
                   total_case_count=1, batch_mean=assessment['final_score'], ranking_eligible=False)
    write_json(root / 'summary.json', summary)
    return summary


def legacy_fixture(root, *, validity='ok', failed_metric=None):
    values = dict(shape=.123456789012, dimension=1., distance=1., mass=1., face_match=1., section=1.)
    config = dict(weights=WEIGHTS, gate=dict(angle='validity'), angle_order=list(MODULE_NAMES))
    write_json(root / 'weights.json', config)
    write_json(root / 'cases.json', dict(cases=[dict(case='part')]))
    for name in MODULE_NAMES:
        status = validity if name == 'validity' else ('evaluator_error' if name == failed_metric else 'ok')
        value = (1. if validity == 'ok' else 0.) if name == 'validity' else values[name]
        if status == 'evaluator_error':
            value = None
        write_json(root / 'results' / (name + '.json'),
                   dict(angle=name, cases=[dict(case='part', status=status, score=value, raw={})]))
    status = 'invalid_shape' if validity == 'invalid_shape' else (
        'evaluator_error(validity)' if validity == 'evaluator_error' else
        'evaluator_error(' + failed_metric + ')' if failed_metric else 'ok')
    final = .7370370367036 if status == 'ok' else 0. if status == 'invalid_shape' else None
    row = dict(case='part', status=status, final_score=final)
    if status == 'ok':
        row.update({name: round(value, 6) for name, value in values.items()})
    elif status == 'invalid_shape':
        row.update(values)
    write_json(root / 'results' / 'final_scores.json', dict(weights=WEIGHTS, gate=config['gate'], cases=[row]))


class ResultReaderTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(read_source), 'read_source must implement the offline result reader')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def change(self, relative, update):
        path = self.root / relative
        doc = json.loads(path.read_text(encoding='utf-8'))
        update(doc)
        write_json(path, doc)

    def change_assessment(self, update):
        self.change('summary.json', lambda doc: update(doc['cases'][0]))
        self.change('Q01/aggregate.json', update)

    def test_complete_v3_preserves_measured_values_and_json_hash_inventory(self):
        v3_fixture(self.root)
        data = read_source(self.root / 'summary.json', source_id='run-a', system='system-a')
        row = data['records'][0]
        self.assertEqual((data['source_id'], data['system'], data['source_protocol_id']), ('run-a', 'system-a', PROTOCOL))
        self.assertEqual(row['baseline_score'], 1.)
        self.assertEqual(row['measured_scores'], dict.fromkeys(WEIGHTS, 1.))
        self.assertEqual(row['input_hashes'], HASHES)
        self.assertTrue(row['participates'])
        self.assertEqual(row['exclusion_reasons'], [])
        self.assertEqual(len(data['source_files']), 10)
        for item in data['source_files']:
            path = Path(item['path'])
            self.assertTrue(path.is_absolute())
            self.assertEqual(item['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_missing_metric_keeps_null_separate_from_policy_zero(self):
        rows = modules()
        del rows['section']
        v3_fixture(self.root, rows)
        row = read_source(self.root)['records'][0]
        self.assertIsNone(row['measured_scores']['section'])
        self.assertEqual(row['awarded_scores']['section'], 0.)
        self.assertAlmostEqual(row['baseline_score'], .9)
        self.assertEqual(row['score_kind'], 'policy_lower_bound')
        self.assertAlmostEqual(row['original_weight_coverage'], .9)
        self.assertEqual(row['metric_statuses']['section'], 'missing')

    def test_unavailable_error_keeps_execution_and_approximate_metric_metadata(self):
        rows = modules()
        rows['section'].update(status='evaluator_error', score=None)
        rows['section']['quality']['measurement_valid'] = False
        rows['mass']['quality']['scored_approximate'] = True
        rows['mass']['backend'] = 'adaptive_gk'
        v3_fixture(self.root, rows)
        row = read_source(self.root)['records'][0]
        self.assertEqual(row['metric_statuses']['section'], 'evaluator_error')
        self.assertEqual(row['execution']['status'], 'completed')
        self.assertEqual(row['metric_details']['mass']['score_source'], 'approximate_measurement')
        self.assertEqual(row['metric_details']['mass']['backend'], 'adaptive_gk')

    def test_invalid_candidate_remains_excluded_with_source_reasons(self):
        rows = modules()
        rows['validity'].update(status='candidate_invalid', score=None)
        rows['validity']['quality']['candidate_invalid_confirmed'] = True
        v3_fixture(self.root, rows)
        row = read_source(self.root)['records'][0]
        self.assertFalse(row['participates'])
        self.assertFalse(row['validation_passed'])
        self.assertIsNone(row['baseline_score'])
        self.assertTrue(all(value is None for value in row['awarded_scores'].values()))
        self.assertIn('candidate_invalid', ' '.join(row['exclusion_reasons']))

    def test_post_run_revoked_case_keeps_measurements_but_no_awards(self):
        from src.contract import refresh_assessment
        def revoke(row):
            row.update(status='evaluator_error', final_score=None)
            row['reasons'].append(dict(detail='code or original input changed during run'))
            refresh_assessment(row)
        v3_fixture(self.root, postprocess=revoke)
        row = read_source(self.root)['records'][0]
        self.assertFalse(row['participates'])
        self.assertEqual(row['measured_scores']['shape'], 1.)
        self.assertIn('code or original input changed', ' '.join(row['exclusion_reasons']))

    def test_rejects_v2_explicitly(self):
        write_json(self.root / 'summary.json', dict(protocol_id='fusion-step-adapted-v2.0', schema_version='2.0'))
        with self.assertRaisesRegex(ValueError, 'v2.*unsupported|unsupported.*v2'):
            read_source(self.root)

    def test_rejects_manifest_hash_tampering(self):
        v3_fixture(self.root)
        self.change('manifest.json', lambda doc: doc.update(started_at='changed'))
        with self.assertRaisesRegex(ValueError, 'manifest.*hash|manifest.*sha256'):
            read_source(self.root)

    def test_rejects_disagreement_between_summary_and_final_aggregate(self):
        v3_fixture(self.root)
        self.change('Q01/aggregate.json', lambda doc: doc.update(final_score=.7))
        with self.assertRaisesRegex(ValueError, 'aggregate|summary'):
            read_source(self.root)

    def test_rejects_module_identity_hash_status_and_score_disagreement(self):
        for update in (lambda r: r.update(case='other'),
                       lambda r: r.update(angle='mass'),
                       lambda r: r.update(run_manifest_sha256='e' * 64),
                       lambda r: r['input_hashes'].update(gt='e' * 64),
                       lambda r: r.update(score=.5),
                       lambda r: r.update(status='evaluator_error')):
            with self.subTest(update=update):
                v3_fixture(self.root)
                self.change('Q01/results/shape.json', update)
                with self.assertRaises(ValueError):
                    read_source(self.root)

    def test_rejects_aggregate_arithmetic_coverage_and_null_corruption(self):
        for update in (lambda r: r.update(final_score=.4),
                       lambda r: r.update(measurement_weight_coverage=.8),
                       lambda r: r['metrics']['section'].update(awarded_score=None),
                       lambda r: r.update(validation_passed=False),
                       lambda r: r['metrics']['shape']['evidence']['input_hashes'].update(gt='e' * 64)):
            with self.subTest(update=update):
                v3_fixture(self.root)
                self.change_assessment(update)
                with self.assertRaises(ValueError):
                    read_source(self.root)

    def test_rejects_missing_published_evidence(self):
        v3_fixture(self.root)
        (self.root / 'Q01/results/shape.json').unlink()
        with self.assertRaisesRegex(ValueError, 'missing|evidence'):
            read_source(self.root)

    def test_rejects_duplicate_cases_and_path_traversal(self):
        for case in ('Q01', '../outside'):
            v3_fixture(self.root)
            if case == 'Q01':
                self.change('summary.json', lambda d: d['cases'].append(d['cases'][0]))
            else:
                self.change('summary.json', lambda d: d['cases'][0].update(case=case))
            with self.assertRaises(ValueError):
                read_source(self.root)

    def test_rejects_duplicate_keys_and_nonfinite_json(self):
        for payload in ('{"protocol_id":"a","protocol_id":"b"}',
                        '{"protocol_id":NaN}', '{"protocol_id":1e999}'):
            (self.root / 'summary.json').write_text(payload, encoding='utf-8')
            with self.assertRaises(ValueError):
                read_source(self.root)

    def test_legacy_rebuilds_full_precision_and_accepts_each_supported_entry_path(self):
        legacy_fixture(self.root)
        for path in (self.root, self.root / 'results', self.root / 'results/final_scores.json'):
            data = read_source(path)
            row = data['records'][0]
            self.assertEqual(data['source_protocol_id'], 'legacy-20261017')
            self.assertAlmostEqual(row['baseline_score'], .7370370367036, places=14)
            self.assertEqual(row['measured_scores']['shape'], .123456789012)
            self.assertEqual(row['input_hashes'], {})
            self.assertTrue(data['warnings'])
            self.assertEqual(len(data['source_files']), 10)

    def test_legacy_invalid_zero_is_preserved_but_excluded_from_analysis(self):
        legacy_fixture(self.root, validity='invalid_shape')
        row = read_source(self.root)['records'][0]
        self.assertIsNone(row['baseline_score'])
        self.assertEqual(row['source_assessment']['final_score'], 0.)
        self.assertFalse(row['participates'])
        self.assertFalse(row['validation_passed'])
        self.assertIn('invalid_shape', row['exclusion_reasons'])

    def test_legacy_failed_metric_never_becomes_measured_or_policy_zero(self):
        legacy_fixture(self.root, failed_metric='section')
        row = read_source(self.root)['records'][0]
        self.assertFalse(row['participates'])
        self.assertTrue(row['validation_passed'])
        self.assertIsNone(row['measured_scores']['section'])
        self.assertIsNone(row['awarded_scores']['section'])
        self.assertIsNone(row['baseline_score'])
        self.assertEqual(row['status'], 'evaluator_error(section)')

    def test_legacy_validation_error_stays_unknown(self):
        legacy_fixture(self.root, validity='evaluator_error')
        row = read_source(self.root)['records'][0]
        self.assertIsNone(row['validation_passed'])

    def test_legacy_first_failure_follows_original_weight_order(self):
        # The historical aggregate.py emits evaluator_error(section) when
        # section precedes shape in cfg['weights'] and both modules fail.
        ordered_weights = {'section': .1, 'shape': .3, 'dimension': .25,
                           'distance': .15, 'mass': .1, 'face_match': .1}
        for keep_config in (True, False):
            with self.subTest(keep_config=keep_config):
                legacy_fixture(self.root, failed_metric='section')
                self.change('results/shape.json',
                            lambda d: d['cases'][0].update(status='evaluator_error', score=None))
                self.change('weights.json', lambda d: d.update(weights=ordered_weights))
                self.change('results/final_scores.json', lambda d: d.update(weights=ordered_weights))
                if not keep_config:
                    (self.root / 'weights.json').unlink()
                row = read_source(self.root)['records'][0]
                self.assertEqual(row['status'], 'evaluator_error(section)')
                self.assertFalse(row['participates'])
                self.assertTrue(row['validation_passed'])
                self.assertIsNone(row['baseline_score'])
                self.assertIsNone(row['measured_scores']['section'])
                self.assertIsNone(row['measured_scores']['shape'])
                self.assertTrue(all(v is None for v in row['awarded_scores'].values()))

    def test_legacy_invalid_case_accepts_unrounded_historical_diagnostics(self):
        legacy_fixture(self.root, validity='invalid_shape')
        self.change('results/final_scores.json', lambda d: d['cases'][0].update(shape=.123456789012))
        row = read_source(self.root)['records'][0]
        self.assertIsNone(row['baseline_score'])
        self.assertEqual(row['measured_scores']['shape'], .123456789012)

    def test_rejects_inconsistent_interval_profile_or_metric_provenance(self):
        for update in (
            lambda r: r.update(score_interval=[0., 1.]),
            lambda r: r['scores'][PROTOCOL].update(value=.2),
            lambda r: r['scores'][PROTOCOL].update(score_kind='policy_lower_bound'),
            lambda r: r['metrics']['mass'].update(backend='invented_backend'),
            lambda r: r['metrics']['mass'].update(score_source='approximate_measurement'),
            lambda r: r['metrics']['mass'].update(diagnostic_value=.7),
        ):
            with self.subTest(update=update):
                v3_fixture(self.root)
                self.change_assessment(update)
                with self.assertRaises(ValueError):
                    read_source(self.root)

    def test_diagnostic_score_does_not_become_an_unavailable_measurement(self):
        rows = modules()
        rows['mass'].update(status='evaluator_error', score=.6)
        rows['mass']['quality']['measurement_valid'] = False
        v3_fixture(self.root, rows)
        row = read_source(self.root)['records'][0]
        self.assertIsNone(row['measured_scores']['mass'])
        self.assertEqual(row['awarded_scores']['mass'], 0.)
        self.assertEqual(row['metric_details']['mass']['diagnostic_value'], .6)

    def test_outer_execution_failure_keeps_unassessed_partial_modules_excluded(self):
        from src.contract import failed_case_result
        def replace(row):
            digest = row['run_manifest_sha256']
            row.clear()
            row.update(failed_case_result('Q01', 'worker launch failed', protocol_id=PROTOCOL))
            row['run_manifest_sha256'] = digest
        v3_fixture(self.root, postprocess=replace)
        row = read_source(self.root)['records'][0]
        self.assertFalse(row['participates'])
        self.assertIsNone(row['validation_passed'])
        self.assertEqual(row['execution']['status'], 'failed')
        self.assertIn('worker launch failed', ' '.join(row['exclusion_reasons']))
        self.assertEqual(row['metric_statuses']['shape'], 'ok')
        self.assertIsNone(row['measured_scores']['shape'])

    def test_reference_invalid_does_not_revoke_valid_candidate_admission(self):
        rows = modules()
        rows['validity'].update(status='reference_invalid', score=None)
        rows['validity']['quality']['measurement_valid'] = False
        rows['validity']['raw']['gt']['healthy'] = False
        v3_fixture(self.root, rows)
        row = read_source(self.root)['records'][0]
        self.assertTrue(row['validation_passed'])
        self.assertTrue(row['participates'])
        self.assertEqual(row['baseline_score'], 1.)

    def test_legacy_minimal_archive_can_omit_original_case_and_weight_files(self):
        legacy_fixture(self.root)
        (self.root / 'weights.json').unlink()
        (self.root / 'cases.json').unlink()
        data = read_source(self.root)
        self.assertAlmostEqual(data['records'][0]['baseline_score'], .7370370367036)
        self.assertEqual(len(data['source_files']), 8)
        self.assertEqual(len(data['warnings']), 3)

    def test_legacy_rejects_final_score_metric_rounding_and_angle_case_mismatch(self):
        for path, update in (
            ('results/final_scores.json', lambda d: d['cases'][0].update(final_score=.5)),
            ('results/final_scores.json', lambda d: d['cases'][0].update(shape=.5)),
            ('results/shape.json', lambda d: d.update(angle='mass')),
            ('results/shape.json', lambda d: d['cases'].append(d['cases'][0])),
            ('results/shape.json', lambda d: d['cases'][0].update(case='different')),
        ):
            with self.subTest(path=path):
                legacy_fixture(self.root)
                self.change(path, update)
                with self.assertRaises(ValueError):
                    read_source(self.root)


if __name__ == '__main__':
    unittest.main()
