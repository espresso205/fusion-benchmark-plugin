"""The comparison-first report contract is shared by all report entry points."""
import copy
import json
import unittest

from test_html_reporting import Page, public_report
from src.html_reporting import render_html


class ReportLayoutContractTests(unittest.TestCase):
    def report(self):
        data = public_report()
        row = data['cases'][0]
        row.update(enterprise_id='E017', enterprise_title='切刀固定板', prompt='完整原始执行说明',
                   measurement_notes=['测量限制说明'], enterprise_geometry_notes=['面分割核对'],
                   candidate_notes=['候选原始自述'])
        row['display'] = {'views': {'scoring': {
            'difference': {'status': 'ok', 'scope': 'whole_model'},
            'metadata': {'preview': {'overlay_kind': 'ordinary_overlay', 'difference_kind': 'difference'}},
            'previews': {angle: {kind: f'display/{angle}-{kind}.png'
                                for kind in ('split', 'overlay', 'difference')}
                         for angle in ('iso', 'front', 'top', 'right')}}}}
        return data

    def test_list_contains_identity_and_images_but_no_results_or_execution_narrative(self):
        html = render_html(self.report())
        overview = html.split('<section id="overview"', 1)[1].split('<div id="case-details"', 1)[0]
        for absent in ('score-value', 'task-summary', '<table', '完整原始执行说明', '候选原始自述'):
            self.assertNotIn(absent, overview)
        self.assertIn('E017', overview)
        self.assertIn('切刀固定板', overview)

    def test_results_and_folded_source_records_follow_all_comparison_images(self):
        html = render_html(self.report())
        detail = html.split('<section class="case-detail"', 1)[1].split('<section id="analysis"', 1)[0]
        self.assertLess(detail.index('visual-panel'), detail.index('multiview-galleries'))
        self.assertLess(detail.index('multiview-galleries'), detail.index('case-results'))
        self.assertLess(detail.index('case-results'), detail.index('score-value'))
        self.assertLess(detail.index('case-results'), detail.index('完整原始执行说明'))
        self.assertIn('<details class="task-prompt">', detail)
        self.assertEqual(detail.count('class="case-results"'), 1)
        for value in ('测量限制说明', '面分割核对', '候选原始自述'):
            self.assertIn(value, detail)

    def test_overlay_and_difference_have_separate_images_and_truthful_captions(self):
        page = Page(render_html(self.report()))
        links = [a for tag, a in page.tags if tag == 'a' and 'data-lightbox' in a]
        for angle in ('iso', 'front', 'top', 'right'):
            overlay = next(a for a in links if a['href'] == f'display/{angle}-overlay.png')
            difference = next(a for a in links if a['href'] == f'display/{angle}-difference.png')
            self.assertIn('透明叠加', overlay['data-caption'])
            self.assertIn('差异', difference['data-caption'])
        self.assertNotIn('·', ''.join(page.text))

    def test_presentation_does_not_mutate_or_replace_scoring_evidence(self):
        data = self.report()
        before = copy.deepcopy(data)
        page = Page(render_html(data))
        self.assertEqual(data, before)
        self.assertEqual(json.loads(''.join(page.data)), before)

    def test_face_count_difference_is_not_described_as_diameter_or_hole_error(self):
        data = self.report()
        data['cases'][0]['metrics']['dimension']['raw'] = {
            'passed': 0, 'total': 1, 'items': [{'id': 'cylinder-005', 'type': 'cylinder_diameter',
                'pass': False, 'expected': 6., 'measured': 6., 'diameter_difference_mm': 0.,
                'count': 2, 'expected_count': 3, 'failure_type': 'face_count_mismatch'}]}
        html = render_html(data)
        self.assertIn('直径相同，曲面数量不同', html)
        self.assertIn('不能直接当作孔数', html)

    def test_cone_count_reason_preserves_the_angle_difference(self):
        for measured, expected_reason in ((48.3309, '半角测量值不同，曲面数量不同'),
                                          (67.5, '半角相同，曲面数量不同')):
            with self.subTest(measured=measured):
                data = self.report()
                data['cases'][0]['metrics']['dimension']['raw'] = {
                    'passed': 0, 'total': 1, 'items': [{'id': 'cone-002', 'type': 'probe',
                        'pass': False, 'expected': 67.5, 'measured': measured, 'count': 4,
                        'face_count_difference': -4, 'failure_type': 'face_count_mismatch'}]}
                html = render_html(data)
                self.assertIn('圆锥半角', html)
                self.assertIn(expected_reason, html)


if __name__ == '__main__':
    unittest.main()
