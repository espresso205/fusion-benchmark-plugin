"""Consumer-visible HTML contract: inert data, complete fallback, faithful credit."""
import json
import copy
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.scripts=[]; self.data=[]; self.text=[]; self.tags=[]; self.in_data=False
        self.feed(text)
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        self.tags.append((tag, attrs))
        if tag=='script':
            self.scripts.append(attrs)
            self.in_data=attrs.get('id')=='report-data'
    def handle_endtag(self, tag):
        if tag=='script': self.in_data=False
    def handle_data(self, text):
        (self.data if self.in_data else self.text).append(text)


def public_report():
    return {'schema_version':'fusion-html-report/1','protocol_id':'fusion-step-adapted-v4.0',
            'coordinate_mode':'geometry','run_name':'fixture','generated_at':'2026-10-26',
            'ranking_eligible':False,'warnings':[],'analysis':None,'source_files':[],
            'summary':{'total_case_count':1,'participating_case_count':1,'complete_case_count':0,
                       'lower_bound_case_count':1,'invalid_case_count':0,'validation_incomplete_case_count':0,
                       'pipeline_unscored_case_count':0,'batch_mean':8.5/13},
            'cases':[{'case':'Q1','status':'scored_with_unavailable_metrics','pipeline_state':None,
                      'final_score':8.5/13,'score_kind':'policy_lower_bound','score_interval':[8.5/13,10.5/13],
                      'measurement_weight_coverage':11/13,'participates_in_scoring':True,
                      'validation_passed':True,'geometry':{'candidate_status':'valid'},
                      'execution':{'status':'completed'},'weights':{'shape':6/13,'dimension':5/13,'mass':2/13},
                      'unavailable_metrics':['mass'],'diagnostic_unavailable_metrics':['distance'],
                      'integrity_status':'verified','pose':{},'coordinate_diagnostics':{},'reasons':[],
                      'metrics':{'shape':{'measured_score':1.,'awarded_score':1.,'metric_role':'scored','status':'ok',
                                          'raw':{'volumetric_iou':1.,'surface_f1':1.,'topology':1.}},
                                 'dimension':{'measured_score':.5,'awarded_score':.5,'metric_role':'scored','status':'ok','raw':{}},
                                 'mass':{'measured_score':None,'awarded_score':0.,'metric_role':'scored','status':'evaluator_error','raw':{}},
                                 'distance':{'measured_score':None,'awarded_score':None,'metric_role':'diagnostic','status':'missing','raw':{}}}}]}


class HtmlTests(unittest.TestCase):
    def test_score_colors_use_reference_cutoffs_without_grading_lower_bounds(self):
        from src.html_reporting import render_html
        data = public_report()
        template = data['cases'][0]
        variants = [(1., 'measured', 'high'), (.9, 'measured', 'high'),
                    (.89999, 'measured', 'middle'), (.6, 'measured', 'middle'),
                    (.59999, 'measured', 'low'), (0., 'measured', 'low'),
                    (.95, 'policy_lower_bound', 'lower_bound'), (None, 'unscored', 'unavailable')]
        data['cases'] = []
        for index, (value, kind, _) in enumerate(variants):
            row = copy.deepcopy(template)
            row.update(case=f'case-{index}', final_score=value, score_kind=kind,
                       participates_in_scoring=value is not None)
            data['cases'].append(row)
        before = copy.deepcopy(data)
        page = Page(render_html(data))
        badges = [attrs for tag, attrs in page.tags if tag == 'strong' and 'data-score-band' in attrs]
        self.assertEqual(len(badges), len(variants) * 2)
        self.assertEqual([badge['data-score-band'] for badge in badges[:len(variants)]], [band for _, _, band in variants])
        text = ' '.join(page.text)
        for label in ('≥ 90', '60–<90', '< 60', '仅为展示区间', '缺测下界与无分数不分档'):
            self.assertIn(label, text)
        self.assertEqual(data, before)
        self.assertEqual(json.loads(''.join(page.data)), before)

    def test_static_detail_navigation_includes_position_between_buttons(self):
        from src.html_reporting import render_html
        data = public_report()
        row = copy.deepcopy(data['cases'][0]); row['case'] = 'Q2'; data['cases'].append(row)
        html = render_html(data)
        positions = [attrs for tag, attrs in Page(html).tags if attrs.get('class') == 'detail-position']
        self.assertEqual(len(positions), 2)
        self.assertIn('>1 / 2</span><button data-step="1">', html)
        self.assertIn('>2 / 2</span><button data-step="1">', html)

    def test_four_views_are_readable_without_javascript_and_open_full_images(self):
        from src.html_reporting import render_html
        data = public_report()
        previews = {angle: {kind: f'display/0/{angle}-{kind}.png' for kind in ('split', 'overlay')}
                    for angle in ('iso', 'front', 'top', 'right')}
        data['cases'][0]['display'] = {'views': {'scoring': {
            'previews': previews, 'difference': {'status': 'ok', 'scope': 'whole_model', 'layers': {}}}}}
        before = copy.deepcopy(data)
        page = Page(render_html(data))
        links = [a for tag, a in page.tags if tag == 'a' and 'data-lightbox' in a]
        for angle in previews:
            for kind in ('split', 'overlay'):
                self.assertTrue(any(a['href'] == previews[angle][kind] for a in links))
        text = ' '.join(page.text)
        for value in ('四视角对比', '全模型差异', '匹配', '多余', '缺失', '透明叠加', '差异着色'):
            self.assertIn(value, text)
        self.assertNotIn('Interface Match', text)
        self.assertEqual(data, before)
        self.assertEqual(json.loads(''.join(page.data)), before)

    def test_difference_scope_and_missing_views_are_not_fabricated(self):
        from src.html_reporting import render_html
        data = public_report()
        data['cases'][0]['display'] = {'views': {'scoring': {
            'thumbnail': 'display/0/legacy.svg',
            'previews': {'front': {'split': 'display/0/front.png'}},
            'difference': {'status': 'unavailable', 'scope': 'whole_model', 'layers': {}, 'reason': 'jig unavailable'}}}}
        html = render_html(data)
        self.assertIn('此视角暂不可用', html)
        self.assertNotIn('Interface Match', ' '.join(Page(html).text))
        self.assertIn('差异图暂不可用', html)
        data['cases'][0]['display']['views']['scoring']['difference'] = {'status': 'ok', 'scope': 'interface', 'layers': {}}
        self.assertIn('Interface 区域差异', render_html(data))

    def test_explicit_tasks_use_participant_denominators_and_metadata_is_escaped(self):
        from src.html_reporting import render_html
        data = public_report()
        original = data['cases'][0]
        original.update(task_type='generation', final_score=.5, prompt='<img onerror=x> 建模')
        omitted = copy.deepcopy(original)
        omitted.update(case='0061', final_score=None, participates_in_scoring=False, task_type='generation')
        unknown = copy.deepcopy(original)
        unknown.update(case='0062', task_type='unexpected', final_score=1)
        data['cases'] += [omitted, unknown]
        page = Page(render_html(data))
        summaries = [a for tag, a in page.tags if a.get('class') == 'task-summary-item']
        self.assertTrue(summaries, 'Explicit task groups must have participant-based summaries')
        generation = next(a for a in summaries if a['data-task'] == 'generation')
        self.assertEqual(generation['data-participants'], '1')
        self.assertEqual(generation['data-total'], '2')
        self.assertEqual(generation['data-mean'], '50.000')
        cards = [a for tag, a in page.tags if a.get('class') == 'case-card']
        self.assertEqual(cards[-1]['data-task'], 'unknown')
        self.assertEqual(cards[-1]['href'], '#fixture=0062')
        self.assertNotIn('<img onerror=x>', render_html(data))
        self.assertTrue(any(tag == 'select' and a.get('id') == 'task-filter' for tag, a in page.tags))

    def test_download_hooks_and_input_enlargement_keep_safe_relative_urls(self):
        from src.html_reporting import render_html
        data = public_report()
        row = data['cases'][0]
        row['downloads'] = {'candidate': {'url': 'downloads/0/candidate.step', 'label': '生成 STEP'},
                            'reference': {'url': 'https://invalid.test/gt.step', 'label': '外部'}}
        data['downloads'] = {'submission_zip': {'url': 'downloads/submission.zip', 'label': '下载提交 ZIP'}}
        row['input_materials'] = {'items': [{'label': '编辑前', 'kind': 'image', 'purpose': 'before',
                                           'url': 'inputs/0/before.gif', 'status': 'completed'}]}
        page = Page(render_html(data))
        anchors = [a for tag, a in page.tags if tag == 'a']
        for url in ('downloads/0/candidate.step', 'downloads/submission.zip'):
            self.assertTrue(any(a.get('href') == url and 'download' in a for a in anchors))
        self.assertFalse(any(a.get('href', '').startswith('https:') for a in anchors))
        self.assertTrue(any(a.get('href') == 'inputs/0/before.gif' and 'data-lightbox' in a for a in anchors))

    def test_validation_health_only_uses_existing_raw_evidence(self):
        from src.html_reporting import render_html
        data = public_report()
        self.assertNotIn('实体闭合', render_html(data))
        data['cases'][0]['metrics']['validity'] = {'raw': {'candidate': {
            'healthy': True, 'brep_valid': True,
            'solids': [{'closed': True, 'bbox_mm': [0, -2, 1, 12, 18, 31]}]}}}
        text = ' '.join(Page(render_html(data)).text)
        for value in ('实体闭合', '12 × 20 × 30', '原坐标', '几何验证通过'):
            self.assertIn(value, text)
        data['cases'][0]['metrics']['validity']['raw']['candidate']['healthy'] = False
        self.assertNotIn('几何验证通过', ' '.join(Page(render_html(data)).text))

    def test_ordinary_overlay_fallback_is_not_labeled_as_difference(self):
        from src.html_reporting import render_html
        data = public_report()
        data['cases'][0]['display'] = {'views': {'scoring': {
            'previews': {'iso': {'overlay': 'previews/ordinary.svg'}},
            'difference': {'status': 'ok', 'scope': 'interface', 'layers': {}},
            'metadata': {'preview': {'overlay_kind': 'difference', 'overlay_fallback': 'ordinary_overlay'}}}}}
        page = Page(render_html(data))
        link = next(a for tag, a in page.tags if tag == 'a' and a.get('href') == 'previews/ordinary.svg')
        self.assertIn('透明叠加', link['data-caption'])
        self.assertNotIn('Interface', link['data-caption'])

    def test_script_termination_is_inert_and_round_trips(self):
        from src.html_reporting import render_html
        data=public_report(); data['run_name']='</script><script>alert(1)</script>&中文'
        data['cases'][0]['case']='</section><img src=x onerror=alert(1)>'
        page=Page(render_html(data))
        self.assertEqual(len(page.scripts),2)
        self.assertEqual(json.loads(''.join(page.data)),data)
        self.assertNotIn('<script>alert(1)</script>',render_html(data))

    def test_static_dom_preserves_missing_credit_and_three_submetrics(self):
        from src.html_reporting import render_html
        text=' '.join(Page(render_html(public_report())).text)
        for literal in ('65.385','80.769','84.6%','未测得','按 0 分计入总分下界',
                        'IoU','Surface F1','Topology','不是统计置信区间','参与评分'):
            self.assertIn(literal,text)

    def test_timeout_is_not_called_invalid(self):
        from src.html_reporting import case_state
        row=public_report()['cases'][0]
        row.update(final_score=None,score_kind='unscored',participates_in_scoring=False,
                   validation_passed=False,status='evaluator_error',geometry={'candidate_status':'unknown'},
                   execution={'status':'timed_out'})
        self.assertEqual(case_state(row),('incomplete','验证未完成'))

    def test_unscored_timeout_explains_missing_score_and_preview_independence(self):
        from src.html_reporting import case_analysis
        row = {'participates_in_scoring': False,
               'geometry': {'candidate_status': 'unknown'},
               'execution': {'status': 'timed_out', 'timed_out': True, 'wall_s': 600.155}}
        text = ' '.join(Page(case_analysis(row, 'geometry')).text)
        for literal in ('600', '超时', '暂无分数', '预览', '不代表', '不是 0 分'):
            self.assertIn(literal, text)
        row['execution'] = {'status': 'failed', 'wall_s': 12}
        self.assertNotIn('超时', case_analysis(row, 'geometry'))
        row['execution'] = {'status': 'timed_out', 'wall_s': None}
        text = case_analysis(row, 'geometry')
        self.assertIn('超时', text)
        self.assertNotIn('None', text)

    def test_diagnostic_credit_is_not_fabricated(self):
        from src.html_reporting import metric_table
        row=public_report()['cases'][0]
        text=' '.join(Page(metric_table(row,diagnostics=True)).text)
        self.assertIn('不计分',text)
        self.assertNotIn('0.000',text)

    def test_pipeline_only_is_unscored_not_validation_timeout(self):
        from src.html_reporting import case_state
        row = {'pipeline_state': 'not_submitted', 'status': 'not_evaluated',
               'geometry': {'candidate_status': 'unknown'}}
        self.assertEqual(case_state(row), ('pipeline', '未评分'))

    def test_actual_weights_and_interrupted_execution_are_explained(self):
        from src.html_reporting import render_html
        data = public_report()
        data['cases'][0]['weights'] = {'shape': .5, 'dimension': .3, 'mass': .2}
        data['cases'][0]['execution'] = {'status': 'timed_out', 'timed_out': True}
        text = ' '.join(Page(render_html(data)).text)
        self.assertIn('50.0%、30.0%、20.0%', text)
        self.assertIn('评测执行超时，已完成的主指标分数予以保留', text)
        data['cases'][0]['execution'] = {'status': 'failed'}
        text = ' '.join(Page(render_html(data)).text)
        self.assertIn('评测执行未完成，已完成的主指标分数予以保留', text)

    def test_build_is_portable_and_never_overwrites_evidence(self):
        from src.html_reporting import build_report
        from report_fixtures import make_run, sha
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source'; source.mkdir(); make_run(source)
            before={p.relative_to(source).as_posix():sha(p) for p in source.rglob('*') if p.is_file()}
            output=Path(folder)/'报告 空格'
            result=build_report(source,output,meshes=False)
            self.assertEqual(result['status'],'completed')
            page=Page((output/'index.html').read_text(encoding='utf-8'))
            self.assertEqual(len(json.loads(''.join(page.data))['cases']),4)
            self.assertNotIn(str(source),''.join(page.data))
            self.assertTrue((output/'assets/report.js').is_file())
            self.assertTrue((output/'serve_report.py').is_file())
            manifest=json.loads((output/'report-manifest.json').read_text(encoding='utf-8'))
            self.assertIn('index.html',manifest['files'])
            self.assertEqual(before,{p.relative_to(source).as_posix():sha(p) for p in source.rglob('*') if p.is_file()})
            with self.assertRaises(FileExistsError): build_report(source,output,meshes=False)

    def test_mixed_input_materials_keep_order_without_exposing_source_paths(self):
        from src.html_reporting import render_html
        data = public_report()
        data['cases'][0]['input_materials'] = {'status': 'completed', 'items': [
            {'label': '零件图', 'kind': 'pdf', 'url': 'inputs/0/0.pdf', 'status': 'completed',
             'previews': [{'label': '第1页', 'url': 'inputs/0/0-0.png'}, {'label': '第2页', 'url': 'inputs/0/0-1.png'}]},
            {'label': '补充视图', 'kind': 'image', 'url': 'inputs/0/1.jpg', 'status': 'completed', 'previews': []},
            {'label': '工程图', 'kind': 'dxf', 'url': 'inputs/0/2.dxf', 'status': 'completed', 'previews': []}]}
        html = render_html(data)
        text = ' '.join(Page(html).text)
        self.assertIn('输入资料', text)
        self.assertIn('打开 PDF', text)
        self.assertIn('下载 DXF', text)
        self.assertLess(html.index('第1页'), html.index('第2页'))
        self.assertNotIn('iframe', html)
        self.assertIn('inputs/0/1.jpg', html)
        self.assertNotIn('输入资料', ' '.join(Page(render_html(public_report())).text))

    def test_input_links_cannot_load_external_or_parent_paths(self):
        from src.html_reporting import input_materials_html
        for url in ('https://example.com/file.png', '../secret.png', 'inputs/../secret.png', 'javascript:alert(1)'):
            html = input_materials_html({'items': [{'label': '<script>x</script>', 'kind': 'image', 'url': url, 'previews': []}]})
            self.assertNotIn('src=', html)
            self.assertNotIn('href=', html)
            self.assertNotIn('<script>x</script>', html)

    def test_generation_failure_keeps_original_results(self):
        from src.html_reporting import try_build_report
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source'; source.mkdir()
            (source/'summary.json').write_text('{bad',encoding='utf-8')
            output=Path(folder)/'html-report'
            result=try_build_report(source,output,meshes=False)
            self.assertEqual(result['status'],'failed')
            self.assertEqual((source/'summary.json').read_text(),'{bad')
            self.assertTrue((Path(folder)/'html-report-generation.json').is_file())


if __name__=='__main__': unittest.main()
