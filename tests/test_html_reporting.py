"""Consumer-visible HTML contract: inert data, complete fallback, faithful credit."""
import json
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.scripts=[]; self.data=[]; self.text=[]; self.in_data=False
        self.feed(text)
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
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
