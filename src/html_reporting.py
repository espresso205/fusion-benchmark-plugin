"""Portable v4 result pages. Rendering never evaluates or changes scores."""
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import math
from pathlib import Path
import shutil

BASE = Path(__file__).resolve().parent.parent
NAMES = {'shape': '形状符合度', 'dimension': '尺寸符合度', 'mass': '材料分布',
         'distance': '表面距离', 'face_match': '严格表面覆盖', 'section': '固定切面'}


def esc(value):
    return escape(str(value if value is not None else '—'), quote=True)


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def score(value):
    return f'{value * 100:.3f}' if is_number(value) else '—'


def percent(value):
    return f'{value * 100:.1f}%' if is_number(value) else '—'


def case_state(row):
    if row.get('integrity_status') in ('error', 'failed', 'untrusted', 'invalid'):
        return 'integrity', '结果无法核验'
    if row.get('geometry', {}).get('candidate_status') == 'invalid':
        return 'invalid', '模型无效'
    if row.get('participates_in_scoring'):
        return ('lower_bound', '缺测下界') if row.get('score_kind') == 'policy_lower_bound' else ('measured', '已评分')
    if row.get('pipeline_state') and row.get('status') in ('not_evaluated', 'unscored', 'pipeline_unscored', 'missing', None):
        return 'pipeline', '未评分'
    if row.get('geometry', {}).get('candidate_status') == 'unknown' or row.get('execution', {}).get('timed_out') or row.get('execution', {}).get('status') == 'timed_out':
        return 'incomplete', '验证未完成'
    return 'pipeline', '未评分'


def metric_table(row, *, diagnostics=False):
    names = ('distance', 'face_match', 'section') if diagnostics else ('shape', 'dimension', 'mass')
    descriptions = {'shape': 'IoU、Surface F1、Topology 等权平均',
                    'dimension': '外形尺寸、直径与几何特征的一致程度',
                    'mass': '体积与主惯性矩反映的材料分布',
                    'distance': '双向点到表面距离', 'face_match': '给定公差内的表面覆盖',
                    'section': '固定切面的材料与轮廓一致程度'}
    parts = []
    for name in names:
        metric = row.get('metrics', {}).get(name, {}) or {}
        measured = metric.get('measured_score')
        note = descriptions[name]
        if measured is None and not diagnostics and row.get('participates_in_scoring'):
            note = '未测得，按 0 分计入总分下界'
        weight = '不计分' if diagnostics else percent(row.get('weights', {}).get(name))
        parts.append(f'<tr><th scope="row">{NAMES[name]}</th><td class="number">{score(measured) if measured is not None else "未测得"}</td><td>{weight}</td><td>{note}</td></tr>')
        if name == 'shape':
            raw = metric.get('raw', {}) or {}
            for key, label in (('volumetric_iou', 'IoU'), ('surface_f1', 'Surface F1'), ('topology', 'Topology')):
                parts.append(f'<tr class="submetric"><th scope="row">{label}</th><td class="number">{score(raw.get(key))}</td><td colspan="2">形状分项</td></tr>')
    return '<div class="table-scroll"><table class="scores"><thead><tr><th>指标</th><th>分数 /100</th><th>权重</th><th>说明</th></tr></thead><tbody>' + ''.join(parts) + '</tbody></table></div>'


def case_analysis(row, mode):
    state, _ = case_state(row)
    if not row.get('participates_in_scoring'):
        execution = row.get('execution', {})
        if state == 'incomplete' and (execution.get('timed_out') or execution.get('status') == 'timed_out'):
            elapsed = execution.get('wall_s')
            timing = f'在约 {elapsed:.0f} 秒后' if is_number(elapsed) and elapsed > 0 else ''
            return (f'<p>本次评测{timing}超时，尚未取得完整的几何有效性验证结果，因此暂无分数。</p>'
                    '<p>模型预览能显示，不代表已经通过完整验证。这不是 0 分，也不能据此判定模型无效。</p>')
        messages = {'invalid': '生成模型未通过几何有效性检查，本题不计分。',
                    'incomplete': '几何验证未完成，本题暂无分数，不能据此判定模型无效。',
                    'integrity': '结果与输入证据未能一致核验，本题分数不予展示。',
                    'pipeline': '本题尚未形成可评分结果。'}
        return '<p>' + messages[state] + '</p>'
    metrics = row.get('metrics', {})
    measured = [(k, metrics.get(k, {}).get('measured_score')) for k in ('shape', 'dimension', 'mass')]
    measured = [(k, v) for k, v in measured if is_number(v)]
    sentences = []
    if row.get('execution', {}).get('timed_out') or row.get('execution', {}).get('status') == 'timed_out':
        sentences.append('评测执行超时，已完成的主指标分数予以保留。')
    elif row.get('execution', {}).get('status') not in (None, 'completed'):
        sentences.append('评测执行未完成，已完成的主指标分数予以保留。')
    if measured:
        lowest, val = min(measured, key=lambda pair: pair[1])
        if all(v >= .999999 for _, v in measured) and len(measured) == 3:
            sentences.append('三项主指标均达到满分。')
        else:
            sentences.append(f'已测主指标中，{NAMES[lowest]}最低，为 {score(val)} 分。')
    shape = metrics.get('shape', {}).get('raw', {}) or {}
    iou, surface, topology = (shape.get(k) for k in ('volumetric_iou', 'surface_f1', 'topology'))
    dimension = metrics.get('dimension', {}).get('measured_score')
    if is_number(iou) and iou >= .85 and is_number(dimension) and dimension < .5:
        sentences.append('整体体积重合较好，但尺寸检查得分较低，外形接近仍可能存在局部尺寸或几何特征差异。')
    elif is_number(iou) and is_number(surface) and iou - surface >= .15:
        sentences.append('表面符合度低于体积重合度，应重点对照局部表面和细小结构。')
    if is_number(topology) and topology < .8:
        sentences.append('拓扑分项存在差异，可结合模型检查连通结构和孔洞；该分项不能直接定位具体缺陷。')
    mass = metrics.get('mass', {}).get('raw', {}) or {}
    volume, inertia = mass.get('volume_relative_error'), mass.get('principal_moment_relative_error')
    if is_number(volume) and is_number(inertia) and is_number(metrics.get('mass', {}).get('measured_score')):
        sentences.append(f'体积相对误差 {percent(volume)}，主惯性矩相对误差 {percent(inertia)}。')
    original = (row.get('coordinate_diagnostics') or {}).get('raw', {}).get('volumetric_iou')
    if mode == 'geometry' and is_number(original) and is_number(iou):
        sentences.append(f'原坐标 IoU 为 {score(original)}，刚体对齐后为 {score(iou)}；可切换原始坐标查看位置差异。')
    missing = [NAMES[k] for k, v in [(k, metrics.get(k, {}).get('measured_score')) for k in ('shape', 'dimension', 'mass')] if v is None]
    if missing:
        sentences.append('、'.join(missing) + '未测得，当前总分为下界；缺测不等同于模型该项得零分。')
    return ''.join('<p>' + esc(sentence) + '</p>' for sentence in sentences)


def default_frame(row):
    views = (row.get('display') or {}).get('views', {})
    return 'world' if not (views.get('scoring') or {}).get('candidate') and (views.get('world') or {}).get('candidate') else 'scoring'


def _preview(row, frame):
    view = (row.get('display') or {}).get('views', {}).get(frame, {}) or {}
    url = view.get('thumbnail')
    warning = '<span class="preview-warning">预览有缺失曲面</span>' if any(q.get('missingFaceCount', 0) > 0 for q in (view.get('metadata', {}).get('quality', {}) or {}).values() if isinstance(q, dict)) else ''
    if url:
        return warning + f'<img class="static-preview" src="{esc(url)}" alt="{esc(row.get("case"))}：左侧生成模型，右侧标准答案" loading="lazy">'
    return '<div class="preview-empty">暂无模型预览</div>'


def _input_url(value):
    if not isinstance(value, str) or not value.startswith('inputs/'):
        return None
    if any(char in value for char in ('\\', ':', '?', '#', '%')) or any(part in ('', '.', '..') for part in value.split('/')):
        return None
    return value


def input_materials_html(materials):
    items = (materials or {}).get('items', [])
    if not items:
        return ''
    parts = []
    for item in items:
        url = _input_url(item.get('url'))
        kind = item.get('kind', 'download')
        label = esc(item.get('label') or '输入资料')
        kind_label = {'image': '图片', 'pdf': 'PDF', 'dxf': 'DXF'}.get(kind, '附件')
        link = ''
        if url:
            action = '打开 PDF' if kind == 'pdf' else '查看原图' if kind == 'image' else '下载 DXF' if kind == 'dxf' else '下载附件'
            attributes = 'target="_blank" rel="noopener"' if kind in ('image', 'pdf') else 'download'
            link = f'<a href="{esc(url)}" {attributes}>{action}</a>'
        pictures = []
        if kind == 'image' and url:
            pictures.append({'url': url, 'label': item.get('label') or '输入图片'})
        pictures.extend(item.get('previews') or [])
        figures = []
        for picture in pictures:
            image_url = _input_url(picture.get('url'))
            caption = esc(picture.get('label') or '预览')
            content = f'<img src="{esc(image_url)}" alt="{caption}" loading="lazy">' if image_url else '<p class="input-unavailable">此页预览不可用</p>'
            figures.append(f'<figure>{content}<figcaption>{caption}</figcaption></figure>')
        gallery = ''
        if figures:
            controls = '<div class="gallery-controls"><button type="button" data-gallery-step="-1" aria-label="上一张输入图">上一张</button><span aria-live="polite"></span><button type="button" data-gallery-step="1" aria-label="下一张输入图">下一张</button></div>' if len(figures) > 1 else ''
            gallery = f'<div class="input-gallery">{"".join(figures)}{controls}</div>'
        message = '<p class="input-unavailable">部分资料不可用，已有评测分数保留。</p>' if item.get('status') in ('partial', 'unavailable') or not url else ''
        parts.append(f'<article class="input-item"><header><h4>{label} <small>{kind_label}</small></h4>{link}</header>{message}{gallery}</article>')
    return f'<details class="input-materials"><summary>输入资料 <span>{len(items)} 份</span></summary><div class="input-content">{"".join(parts)}</div></details>'


def _case_detail(row, index, mode):
    state, label = case_state(row)
    interval = row.get('score_interval')
    range_text = ''
    if row.get('score_kind') == 'policy_lower_bound' and isinstance(interval, list) and len(interval) == 2:
        range_text = f'<p class="notice">主分覆盖 {percent(row.get("measurement_weight_coverage"))}。总分下界 {score(interval[0])}，缺测项补全后可能为 {score(interval[0])}–{score(interval[1])} 分。该范围不是统计置信区间。</p>'
    initial_frame = default_frame(row)
    frames = f'<button data-frame="scoring" aria-pressed="{str(initial_frame == "scoring").lower()}">对齐后</button><button data-frame="world" aria-pressed="{str(initial_frame == "world").lower()}">原始坐标</button>'  if mode == 'geometry' else '<span>原始坐标计分</span>'
    return f'''<section class="case-detail" id="case-{index}" data-index="{index}" aria-label="题目 {esc(row.get('case'))}">
<nav class="detail-nav"><a href="#overview">返回列表</a><div><button data-step="-1">上一题</button><button data-step="1">下一题</button></div></nav>
<div class="detail-heading"><h2>题目 {esc(row.get('case'))} <span class="state {state}">{label}</span></h2><strong>{score(row.get('final_score'))}<small> /100</small></strong></div>{range_text}
<section class="visual-panel"><div class="viewer-toolbar"><div class="frame-tabs" role="group" aria-label="模型坐标">{frames}</div><div class="modes"><button data-mode="split" aria-pressed="true">并排</button><button data-mode="overlay">叠加</button><button data-mode="candidate">生成模型</button><button data-mode="reference">标准答案</button></div><div class="view-buttons"><button data-view="iso">等轴</button><button data-view="front">前视</button><button data-view="top">俯视</button><button data-view="right">右视</button></div></div>
<div class="comparison-labels"><span>生成模型</span><span>标准答案</span></div><div class="preview-host">{_preview(row, default_frame(row))}</div><div class="viewer-mount"></div>
<div class="opacity-controls" hidden><label>生成模型透明度 <input data-opacity="candidate" type="range" min="0.05" max="1" step="0.05" value="0.65"></label><label>标准答案透明度 <input data-opacity="reference" type="range" min="0.05" max="1" step="0.05" value="0.35"></label></div>
<p class="viewer-status" role="status">静态预览，三维交互需启动本机服务。</p></section>
{input_materials_html(row.get('input_materials'))}
<div class="result-columns"><section class="metric-panel"><h3>指标分数</h3>{metric_table(row)}<details class="diagnostics"><summary>辅助指标（不计入总分）</summary>{metric_table(row, diagnostics=True)}</details></section><section class="case-analysis"><h3>评测分析</h3>{case_analysis(row, mode)}</section></div>
</section>'''


def _analysis_html(analysis):
    if not analysis or analysis.get('status') == 'not_provided':
        return '<p class="muted">本次未附指标消融结果。</p>'
    if analysis.get('status') != 'ok':
        return '<p class="notice">消融结果未通过核验，暂不展示。</p>'
    cohort = analysis.get('cohorts', {}).get('complete_measurements', {})
    variants = cohort.get('variants', [])
    baseline = next((v.get('mean_score') for v in variants if v.get('id') == 'baseline'), None)
    rows = []
    for variant in variants:
        removed, mode = variant.get('removed_metric'), variant.get('mode')
        label = '完整指标' if mode == 'baseline' else '移除' + NAMES.get(removed, str(removed))
        policy = '原始权重' if mode == 'baseline' else '剩余权重归一化' if mode == 'renormalized' else '剩余权重不变'
        mean = variant.get('mean_score')
        delta = f'{(mean-baseline)*100:+.3f}' if is_number(mean) and is_number(baseline) else '—'
        rows.append(f'<tr><th scope="row">{esc(label)}</th><td>{policy}</td><td class="number">{score(mean)}</td><td class="number">{delta}</td></tr>')
    return f'<p>固定 {cohort.get("case_count", 0)} 个主指标完整的案例，比较移除指标后的平均分变化。</p><div class="table-scroll"><table><thead><tr><th>设置</th><th>权重处理</th><th>平均分 /100</th><th>相对完整指标</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div><p class="muted">归一化后的升降反映各指标对当前样本的影响。不同题目的分数比较不代表模型排行榜。</p>'


def render_html(data):
    """Static results remain readable without JavaScript, HTTP or WebGL."""
    summary, cases, mode = data.get('summary', {}), data.get('cases', []), data.get('coordinate_mode', 'geometry')
    stats = [('平均分', score(summary.get('batch_mean'))), ('参与评分', f'{summary.get("participating_case_count", 0)} / {len(cases)}'),
             ('完整评分', summary.get('complete_case_count', 0)), ('缺测下界', summary.get('lower_bound_case_count', 0)),
             ('模型无效', summary.get('invalid_case_count', 0)), ('验证未完成', summary.get('validation_incomplete_case_count', 0))]
    if summary.get('pipeline_unscored_case_count'):
        stats.append(('未评分', summary['pipeline_unscored_case_count']))
    stat_html = ''.join(f'<span><span class="summary-label">{label}</span><b>{esc(value)}</b></span>' for label, value in stats)
    cards = []
    for index, row in enumerate(cases):
        state, label = case_state(row)
        cards.append(f'<a class="case-card" href="#case-{index}" data-index="{index}" data-state="{state}"><div class="card-preview"><div class="comparison-labels"><span>生成模型</span><span>标准答案</span></div>{_preview(row, default_frame(row))}</div><div class="card-body"><b>{esc(row.get("case"))}</b><span class="state {state}">{label}</span><strong>{score(row.get("final_score"))}</strong></div></a>')
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    weight_sets = {tuple((row.get('weights') or {}).get(k) for k in ('shape', 'dimension', 'mass')) for row in cases if row.get('participates_in_scoring')}
    weights_note = '各题采用的权重见单题指标表。'
    if len(weight_sets) == 1:
        weights_note = '本批次总分权重依次为 ' + '、'.join(percent(v) for v in next(iter(weight_sets))) + '。'
    mode_note = '刚体对齐后比较几何，坐标差异单独查看' if mode == 'geometry' else '使用原始坐标比较几何'
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(data.get('run_name'))} - CAD 评测结果</title><link rel="stylesheet" href="assets/report.css"></head>
<body><main><header class="report-header"><div class="report-heading"><h1>评测结果：{esc(data.get('run_name'))}</h1><p class="subtitle">{mode_note}</p></div><div class="summary-line">{stat_html}</div></header>
<nav class="page-nav"><a href="#overview">案例列表</a><a href="#analysis">评测分析</a></nav>
<section id="overview" class="overview"><div class="filters"><input aria-label="搜索题号" id="case-search" type="search" placeholder="搜索题号" autocomplete="off"><select aria-label="筛选状态" id="case-filter"><option value="all">全部状态</option><option value="measured">已评分</option><option value="lower_bound">缺测下界</option><option value="invalid">模型无效</option><option value="incomplete">验证未完成</option><option value="pipeline">未评分</option><option value="integrity">结果无法核验</option></select><select aria-label="排序" id="case-sort"><option value="case">按题号</option><option value="desc">分数从高到低</option><option value="asc">分数从低到高</option></select><span id="visible-count">{len(cases)} 个案例</span></div>
<p class="list-note">点击查看模型对比与各项分数。均分仅统计参与评分的案例，包含缺测下界。</p><div class="case-grid">{''.join(cards)}</div><p id="empty-filter" hidden>没有符合筛选的案例。</p></section>
<div id="case-details">{''.join(_case_detail(row, i, mode) for i, row in enumerate(cases))}</div>
<section id="analysis" class="appendix"><h2>评测分析</h2><h3>指标含义</h3><p>形状符合度衡量体积重合、表面接近程度与拓扑结构；尺寸符合度检查外形尺寸、直径和几何特征；材料分布检查体积与主惯性矩。{weights_note}</p><p>体积重合较高仍可能遗漏小孔或薄壁。尺寸检查未覆盖完整图纸公差，体积与惯性一致也不能唯一确定形状。几何对齐仅允许平移和旋转。</p><h3>指标消融</h3>{_analysis_html(data.get('analysis'))}</section>
</main><script id="report-data" type="application/json">{encoded}</script><script src="assets/report.js" defer></script></body></html>'''


def _write_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def build_report(source, output, *, pipeline=None, analysis=None, meshes=True, timeout=60.0, run_name=None, inputs=None):
    from .report_data import load_report
    output=Path(output).resolve()
    if output.exists(): raise FileExistsError('report output already exists; use a new directory')
    if not is_number(timeout) or timeout<=0: raise ValueError('mesh timeout must be positive and finite')
    bundle=load_report(source,pipeline=pipeline,analysis=analysis,run_name=run_name)
    output.mkdir(parents=True,exist_ok=False)
    data=bundle.data
    if inputs is not None:
        from .report_inputs import build_input_assets
        materials = build_input_assets(inputs, output, [row['case'] for row in data.get('cases', [])])
        for row in data.get('cases', []):
            if row['case'] in materials:
                row['input_materials'] = materials[row['case']]
    assets=BASE/'report_assets'
    if not assets.is_dir(): raise ValueError('report assets are absent from this source distribution')
    shutil.copytree(assets,output/'assets')
    display={}
    if meshes and bundle.model_sources:
        try:
            from .display_assets import build_display_assets
            display=build_display_assets(bundle.model_sources,output,timeout=timeout,workers=2)
        except Exception:
            data.setdefault('warnings',[]).append('显示网格生成失败；评分与文本证据保留，可在依赖就绪后重建预览。')
    for row in data.get('cases',[]):
        row['display']=display.get(row['case'],{'status':'not_requested' if not meshes else 'unavailable','views':{}})
    shutil.copyfile(BASE/'serve_report.py',output/'serve_report.py')
    (output/'打开三维报告.cmd').write_text('@echo off\r\ncd /d "%~dp0"\r\npython -B "%~dp0serve_report.py" --root "%~dp0." --open\r\npause\r\n',encoding='utf-8')
    (output/'index.html').write_text(render_html(data),encoding='utf-8')
    (output/'README.txt').write_text('离线阅读：双击 index.html。\n三维交互：使用 Python 3 执行 python -B serve_report.py --root . --open\n服务仅绑定本机，按 Ctrl+C 停止。整个目录可移动，查看不需要 CAD 库。\n',encoding='utf-8')
    files={}
    for path in sorted(output.rglob('*')):
        if path.is_file(): files[path.relative_to(output).as_posix()]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size}
    manifest={'schema_version':'fusion-html-report-manifest/1','generated_at':datetime.now(timezone.utc).isoformat(),
              'protocol_id':data.get('protocol_id'),'coordinate_mode':data.get('coordinate_mode'),
              'source_files':bundle.source_files,'files':files,'ranking_eligible':False,
              'report_generator':{name:hashlib.sha256((BASE/name).read_bytes()).hexdigest() for name in ('src/report_data.py','src/report_inputs.py','src/html_reporting.py','serve_report.py') if (BASE/name).is_file()}}
    _write_json(output/'report-manifest.json',manifest)
    return {'status':'partial' if any(row.get('input_materials',{}).get('status', 'completed') != 'completed' or row.get('display',{}).get('status') not in ('ok','completed','not_requested') for row in data.get('cases',[])) else 'completed',
            'index_html':str(output/'index.html'),'manifest':str(output/'report-manifest.json'),'case_count':len(data.get('cases',[]))}


def try_build_report(source, output, **kwargs):
    """Best-effort presentation; never reinterpret a completed evaluation."""
    output=Path(output)
    try:
        result=build_report(source,output,**kwargs)
    except Exception as error:
        result={'status':'failed','error':type(error).__name__+': HTML report generation failed; scores are unchanged.'}
    try:
        status=output.parent/(output.name+'-generation.json')
        # Preserve earlier generation evidence on repeated calls.
        with status.open('x',encoding='utf-8') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2,allow_nan=False)
    except OSError:
        pass
    return result
