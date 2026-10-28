"""Portable v4 result pages. Rendering never evaluates or changes scores."""
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re
import shutil
from urllib.parse import quote

BASE = Path(__file__).resolve().parent.parent
NAMES = {'shape': '形状符合度', 'dimension': '尺寸符合度', 'mass': '材料分布',
         'distance': '表面距离', 'face_match': '严格表面覆盖', 'section': '固定切面'}
ANGLES = (('iso', '等轴 ISO'), ('front', '前视 Front'), ('top', '俯视 Top'), ('right', '右视 Right'))
TASKS = {'generation': '生成', 'editing': '编辑', 'unknown': '未标注'}


def esc(value):
    return escape(str(value if value is not None else '—'), quote=True)


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def score(value):
    return f'{value * 100:.3f}' if is_number(value) else '—'


def percent(value):
    return f'{value * 100:.1f}%' if is_number(value) else '—'


def _score_html(row, *, detail=False):
    value = row.get('final_score')
    if not is_number(value):
        band, description = 'unavailable', '暂无分数，不作颜色分档'
    elif row.get('score_kind') == 'policy_lower_bound':
        band, description = 'lower_bound', '总分下界，缺测项未补全，不作颜色分档'
    else:
        band = 'high' if value >= .9 else 'middle' if value >= .6 else 'low'
        description = '已测总分；颜色仅表示展示区间'
    suffix = '<small> /100</small>' if detail else ''
    if band == 'lower_bound':
        suffix += '<small class="score-kind">下界</small>'
    return f'<strong class="score-value score-{band}" data-score-band="{band}" title="{description}">{score(value)}{suffix}</strong>'


def _score_legend():
    return ('<div class="score-legend" aria-label="分数颜色图例"><span class="score-legend-label">分数 /100</span>'
            '<span class="score-high">≥ 90</span><span class="score-middle">60–&lt;90</span><span class="score-low">&lt; 60</span>'
            '<span class="score-legend-note">仅为展示区间，不代表官方等级；缺测下界与无分数不分档。</span></div>')


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
        parts.append(f'<tr><th scope="row"><a href="#analysis" title="查看指标含义">{NAMES[name]}</a></th><td class="number">{score(measured) if measured is not None else "未测得"}</td><td>{weight}</td><td>{note}</td></tr>')
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
    url = _resource_url((view.get('previews', {}).get('iso') or {}).get('split') or view.get('thumbnail'))
    warning = '<span class="preview-warning">预览有缺失曲面</span>' if any(q.get('missingFaceCount', 0) > 0 for q in (view.get('metadata', {}).get('quality', {}) or {}).values() if isinstance(q, dict)) else ''
    if url:
        return warning + f'<img class="static-preview" src="{esc(url)}" alt="{esc(row.get("case"))}：左侧生成模型，右侧标准答案" loading="lazy">'
    return '<div class="preview-empty">暂无模型预览</div>'


def _resource_url(value):
    if not isinstance(value, str) or not value or value.startswith('/'):
        return None
    if any(char in value for char in ('\\', ':', '?', '#', '%')) or any(part in ('', '.', '..') for part in value.split('/')):
        return None
    return value


def _image_link(url, caption, group, *, image_class=''):
    url = _resource_url(url)
    if not url:
        return '<div class="preview-empty">此视角暂不可用</div>'
    return f'<a class="image-link" href="{esc(url)}" data-lightbox="{esc(group)}" data-caption="{esc(caption)}" aria-label="放大：{esc(caption)}"><img class="{esc(image_class)}" src="{esc(url)}" alt="{esc(caption)}" loading="lazy"></a>'


def _static_difference(view):
    if any((images or {}).get('difference') for images in (view.get('previews') or {}).values()):
        return True
    preview = (view.get('metadata') or {}).get('preview') or {}
    if preview.get('overlay_fallback') == 'ordinary_overlay' or preview.get('overlay_kind') == 'ordinary_overlay':
        return False
    return (view.get('difference') or {}).get('status') in ('ok', 'completed', 'ready', 'partial', 'empty')


def _difference_label(view, *, static=False):
    difference = view.get('difference') or {}
    if static and not _static_difference(view):
        return '透明叠加'
    if difference.get('status') in ('ok', 'completed', 'ready', 'partial', 'empty'):
        return 'Interface 区域差异' if difference.get('scope') == 'interface' else '全模型差异'
    return '透明叠加'


def _difference_legend(view, *, static=False):
    difference = view.get('difference') or {}
    if static and not _static_difference(view) and difference.get('status') in ('ok', 'completed', 'ready', 'partial', 'empty'):
        return '<p class="difference-note">此处为普通透明叠加；差异着色可在三维模式查看。</p>'
    if difference.get('status') not in ('ok', 'completed', 'ready', 'partial', 'empty'):
        return '<p class="difference-note">差异图暂不可用；已有叠加图仅展示两侧模型的透明重叠。</p>'
    scope = _difference_label(view)
    explanation = '依据提供的 KOR / KIR 区域显示，不代表 Interface 评分。' if difference.get('scope') == 'interface' else '三角网格的整体材料差异，仅供目视检查，不是接口评价。'
    visualization = difference.get('visualization') or {}
    matched = '匹配'
    if visualization.get('kind') == 'surface-distance-tolerance':
        matched = '重合或容差内'
        explanation = visualization.get('description') or f'显示容差 {visualization.get("tolerance_mm")} mm；仅用于网格显示，不改变评分。'
    partial = ' 部分差异层不可用。' if difference.get('status') == 'partial' else ''
    return f'<div class="difference-legend"><span class="matched">{matched}</span><span class="extra">多余</span><span class="missing">缺失</span></div><p class="difference-note">{scope}。{esc(explanation)}{partial}</p>'


def _multiview_html(row, frame):
    view = (row.get('display') or {}).get('views', {}).get(frame, {}) or {}
    previews = view.get('previews') or {}
    groups = []
    for kind, title in (('split', '四视角对比'), ('overlay', '透明叠加'), ('difference', _difference_label(view))):
        tiles = []
        for angle, label in ANGLES:
            url = (previews.get(angle) or {}).get(kind)
            # Older reports stored a difference image under the overlay key.
            # Never relabel that image as ordinary transparency.
            legacy_difference = _static_difference(view) and not any((p or {}).get('difference') for p in previews.values())
            if legacy_difference and kind == 'overlay':
                url = None
            elif legacy_difference and kind == 'difference':
                url = (previews.get(angle) or {}).get('overlay')
            if kind == 'split' and angle == 'iso' and not url:
                url = view.get('thumbnail')
            caption = f'{row.get("case")} {label} {"左侧生成模型，右侧标准答案" if kind == "split" else title}'
            tiles.append(f'<figure data-angle="{angle}">{_image_link(url, caption, kind)}<figcaption>{label}</figcaption></figure>')
        note = (_difference_legend(view, static=True) if kind == 'difference' else
                '<p class="gallery-note">橙色为生成模型，蓝色为标准答案。透明度仅用于观察重合，不表示误差。</p>' if kind == 'overlay' else
                '<p class="gallery-note">左侧生成模型，右侧标准答案。共用坐标、相机与尺度，点击后可缩放和拖动。</p>')
        groups.append(f'<section class="view-gallery" data-preview-kind="{kind}"><h3>{title}</h3>{note}<div class="view-grid">{"".join(tiles)}</div></section>')
    return '<div class="multiview-galleries">' + ''.join(groups) + '</div>'


def _case_title(row):
    return ' '.join(str(value) for value in (row.get('case'), row.get('enterprise_id'),
                    row.get('enterprise_title') or row.get('title')) if value)


def _notes_html(title, notes, *, folded=False):
    entries = [f'<p>{esc(note)}</p>' for note in notes or [] if isinstance(note, str) and note.strip()]
    if not entries:
        return ''
    if folded:
        return f'<details class="evidence-notes"><summary>{esc(title)}</summary>{"".join(entries)}</details>'
    return f'<section class="measurement-notes"><h4>{esc(title)}</h4>{"".join(entries)}</section>'


def _case_verdict(row):
    """Concise summary of existing measurements; never infer a new score."""
    if row.get('report_conclusion'):
        return esc(row['report_conclusion'])
    if not row.get('participates_in_scoring'):
        return '未获得正式评分。' + {'invalid': '生成模型未通过几何验证。',
            'integrity': '评分证据未通过核验。', 'incomplete': '几何验证尚未完成。'}.get(case_state(row)[0], '本次运行没有形成可评分提交。')
    values = [(k, (row.get('metrics', {}).get(k) or {}).get('measured_score')) for k in ('shape', 'dimension', 'mass')]
    available = [(k, v) for k, v in values if is_number(v)]
    if len(available) != 3:
        return '部分主指标未测得，当前只能给出总分下界，不能判定整体通过。'
    if all(v >= .99 for _, v in available):
        return '三项主指标均达到 99 分，当前几何检查显示高度一致。'
    lowest, value = min(available, key=lambda item: item[1])
    checks = (row.get('metrics', {}).get('dimension', {}).get('raw') or {})
    counts = f'尺寸检查通过 {checks["passed"]}/{checks["total"]} 项。' if is_number(checks.get('passed')) and is_number(checks.get('total')) else ''
    return f'{counts}得分最低的主指标为{NAMES[lowest]}（{score(value)} 分），具体差异见下表。'


def _dimension_findings(row):
    """Report probe facts without turning BRep face counts into hole counts."""
    raw = (row.get('metrics', {}).get('dimension', {}).get('raw') or {})
    items = [item for item in raw.get('items', []) if isinstance(item, dict) and item.get('pass') is False]
    if not items:
        return ''
    def number(value, unit, missing):
        return f'{value:.4f}'.rstrip('0').rstrip('.') + ' ' + unit if is_number(value) else missing
    rows = []
    for item in items:
        kind, ident = item.get('type'), str(item.get('id') or '')
        label = '圆柱直径' if kind == 'cylinder_diameter' else '圆锥半角' if ident.startswith('cone-') else '平面间距' if ident.startswith('plane-span-') or kind == 'plane_pair' else '几何探针'
        unit = '°' if label == '圆锥半角' else 'mm'
        reason = {'feature_not_found': '未检出对应特征', 'outside_tolerance': '超出容差',
                  'face_count_mismatch': '曲面数量不同'}.get(item.get('failure_type'), '检查未通过')
        if item.get('failure_type') == 'face_count_mismatch':
            delta = item.get('diameter_difference_mm')
            if kind == 'cylinder_diameter' and is_number(delta) and abs(delta) < 1e-8:
                reason = '直径相同，曲面数量不同'
            elif kind == 'cylinder_diameter' and item.get('fallback_association'):
                reason = '最近直径关联，直径与曲面数量均有差异'
            elif label == '圆锥半角' and is_number(item.get('expected')) and is_number(item.get('measured')):
                same_angle = abs(item['measured'] - item['expected']) < 1e-8
                reason = '半角相同，曲面数量不同' if same_angle else '半角测量值不同，曲面数量不同'
            if is_number(item.get('expected_count')) and is_number(item.get('count')):
                reason += f'（标准 {item["expected_count"]}，生成 {item["count"]}）'
        rows.append(f'<tr><th>{label}</th><td class="number">{number(item.get("expected"), unit, "未提供")}</td><td class="number">{number(item.get("measured"), unit, "未检出")}</td><td>{esc(reason)}</td></tr>')
    return ('<section class="dimension-findings"><h3>未通过的尺寸检查</h3><p class="muted">来自标准模型的几何探针。曲面数量不能直接当作孔数；完整工程图公差未在此认证。</p>'
            '<div class="table-scroll"><table class="scores"><thead><tr><th>检查项</th><th>标准值</th><th>生成值</th><th>结果</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div></section>')


def _download_html(downloads, *, batch=False):
    labels = {'candidate': '生成 STEP', 'reference': '标准答案 STEP', 'aligned_candidate': '对齐后 STEP', 'submission_zip': '下载提交 ZIP'}
    links = []
    for key in (('submission_zip',) if batch else ('candidate', 'reference', 'aligned_candidate')):
        item = (downloads or {}).get(key) or {}
        url = _resource_url(item.get('url'))
        if url:
            links.append(f'<a class="download-link" href="{esc(url)}" download>{esc(item.get("label") or labels[key])}</a>')
    return f'<div class="download-links">{"".join(links)}</div>' if links else ''


def _task_type(row):
    value = row.get('task_type')
    return value if value in TASKS else 'unknown'


def _task_summary(cases):
    parts = []
    for key, label in TASKS.items():
        group = [row for row in cases if _task_type(row) == key]
        if not group:
            continue
        participants = [row for row in group if row.get('participates_in_scoring')]
        values = [row.get('final_score') for row in participants]
        mean = sum(values) / len(values) if values and all(is_number(value) for value in values) else None
        parts.append(f'<div class="task-summary-item" data-task="{key}" data-participants="{len(participants)}" data-total="{len(group)}" data-mean="{score(mean)}"><span>{label}</span><strong>{score(mean)}<small> /100</small></strong><span>参与评分 {len(participants)} / {len(group)}</span></div>')
    return '<div class="task-summary" aria-label="按任务类型汇总">' + ''.join(parts) + '</div>'


def _health_html(row):
    candidate = (((row.get('metrics') or {}).get('validity') or {}).get('raw') or {}).get('candidate') or {}
    if not candidate:
        return ''
    items = []
    if candidate.get('healthy') is True and candidate.get('brep_valid') is True:
        items.append('几何验证通过')
    elif candidate.get('healthy') is False or candidate.get('brep_valid') is False:
        items.append('几何验证未通过')
    solids = candidate.get('solids') or []
    if solids and all(solid.get('closed') is True for solid in solids):
        items.append('实体闭合')
    bounds = [solid.get('bbox_mm') for solid in solids]
    if bounds and all(isinstance(box, list) and len(box) == 6 and all(is_number(value) for value in box) and all(box[i+3] >= box[i] for i in range(3)) for box in bounds):
        extents = [max(box[i+3] for box in bounds) - min(box[i] for box in bounds) for i in range(3)]
        items.append('外包尺寸 ' + ' × '.join(f'{value:.3f}'.rstrip('0').rstrip('.') for value in extents) + ' mm（原坐标）')
    return '<div class="health-line">' + ''.join(f'<span>{esc(item)}</span>' for item in items) + '</div>' if items else ''


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
            content = _image_link(image_url, picture.get('label') or '预览', 'inputs') if image_url else '<p class="input-unavailable">此页预览不可用</p>'
            figures.append(f'<figure>{content}<figcaption>{caption}</figcaption></figure>')
        gallery = ''
        if figures:
            controls = '<div class="gallery-controls"><button type="button" data-gallery-step="-1" aria-label="上一张输入图">上一张</button><span aria-live="polite"></span><button type="button" data-gallery-step="1" aria-label="下一张输入图">下一张</button></div>' if len(figures) > 1 else ''
            gallery = f'<div class="input-gallery">{"".join(figures)}{controls}</div>'
        message = '<p class="input-unavailable">部分资料不可用，已有评测分数保留。</p>' if item.get('status') in ('partial', 'unavailable') or not url else ''
        parts.append(f'<article class="input-item"><header><h4>{label} <small>{kind_label}</small></h4>{link}</header>{message}{gallery}</article>')
    return f'<details class="input-materials"><summary>输入资料 <span>{len(items)} 份</span></summary><div class="input-content">{"".join(parts)}</div></details>'


def _prompt_inline(text):
    """A small inert Markdown subset; raw HTML and links remain plain text."""
    parts, offset = [], 0
    for match in re.finditer(r'`([^`\n]+)`|\*\*([^*\n]+)\*\*', text):
        parts.append(esc(text[offset:match.start()]))
        tag, value = ('code', match[1]) if match[1] is not None else ('strong', match[2])
        parts.append(f'<{tag}>{esc(value)}</{tag}>')
        offset = match.end()
    return ''.join(parts) + esc(text[offset:])


def _prompt_html(text):
    """Format task prose for reading without altering its original report data."""
    parts, paragraph, items = [], [], []
    list_tag, fence, code = None, None, []

    def flush():
        nonlocal list_tag
        if paragraph:
            parts.append('<p>' + _prompt_inline(' '.join(paragraph)) + '</p>')
            paragraph.clear()
        if items:
            start = f' start="{items[0][0]}"' if list_tag == 'ol' else ''
            entries = []
            for i, (number, lines) in enumerate(items):
                value = f' value="{number}"' if list_tag == 'ol' and number != items[0][0] + i else ''
                entries.append(f'<li{value}>' + _prompt_inline(' '.join(lines)) + '</li>')
            parts.append(f'<{list_tag}{start}>' + ''.join(entries) + f'</{list_tag}>')
            items.clear()
        list_tag = None

    for raw in str(text).splitlines():
        line = raw.strip()
        if fence:
            if re.fullmatch(re.escape(fence[0]) + '{' + str(len(fence)) + ',}', line):
                parts.append('<pre><code>' + esc('\n'.join(code)) + '</code></pre>')
                fence, code = None, []
            else:
                code.append(raw)
            continue
        opening = re.fullmatch(r'(`{3,}|~{3,})[^`~]*', line)
        heading = re.match(r'^(#{1,6})\s+(.+)$', line)
        item = re.match(r'^(?:[-+*]|(\d{1,9})[.)])\s+(.+)$', line)
        if opening:
            flush()
            fence = opening[1]
        elif not line:
            flush()
        elif heading:
            flush()
            level = min(len(heading[1]) + 3, 6)
            parts.append(f'<h{level}>' + _prompt_inline(heading[2]) + f'</h{level}>')
        elif item:
            kind = 'ol' if item[1] else 'ul'
            if paragraph or list_tag != kind:
                flush()
            list_tag = kind
            items.append((int(item[1]) if item[1] else None, [item[2]]))
        elif items:
            items[-1][1].append(line)
        else:
            paragraph.append(line)
    flush()
    if fence:
        parts.append('<pre><code>' + esc('\n'.join(code)) + '</code></pre>')
    return '<div class="task-body">' + ''.join(parts) + '</div>'


def _case_detail(row, index, mode, total):
    state, label = case_state(row)
    interval = row.get('score_interval')
    range_text = ''
    if row.get('score_kind') == 'policy_lower_bound' and isinstance(interval, list) and len(interval) == 2:
        range_text = f'<p class="notice">主分覆盖 {percent(row.get("measurement_weight_coverage"))}。总分下界 {score(interval[0])}，缺测项补全后可能为 {score(interval[0])}–{score(interval[1])} 分。该范围不是统计置信区间。</p>'
    initial_frame = default_frame(row)
    frames = f'<button data-frame="scoring" aria-pressed="{str(initial_frame == "scoring").lower()}">对齐后</button><button data-frame="world" aria-pressed="{str(initial_frame == "world").lower()}">原始坐标</button>'  if mode == 'geometry' else '<span>原始坐标计分</span>'
    prompt = f'<details class="task-prompt"><summary>原始任务要求 <span class="task-badge">{TASKS[_task_type(row)]}</span></summary>{_prompt_html(row["prompt"])}</details>' if row.get('prompt') else ''
    failure_notes = [reason.get('detail') for reason in row.get('reasons') or [] if isinstance(reason, dict) and reason.get('module') == 'pipeline' and reason.get('detail')]
    return f'''<section class="case-detail" id="case-{index}" data-index="{index}" aria-label="题目 {esc(row.get('case'))}"><span id="fixture={esc(row.get('case'))}" class="fixture-anchor"></span>
<nav class="detail-nav"><a href="#overview">返回列表</a><div><button data-step="-1">上一题</button><span class="detail-position" aria-live="polite">{index + 1} / {total}</span><button data-step="1">下一题</button></div></nav>
<div class="detail-heading"><h2>{esc(_case_title(row))}</h2></div>
<section class="visual-panel"><div class="viewer-toolbar"><div class="frame-tabs" role="group" aria-label="模型坐标">{frames}</div><div class="modes"><button data-mode="split" aria-pressed="true">并排</button><button data-mode="overlay" aria-pressed="false">透明叠加</button><button data-mode="difference" aria-pressed="false">差异着色</button><button data-mode="difference_raw" aria-pressed="false">精确体积差异</button><button data-mode="candidate" aria-pressed="false">生成模型</button><button data-mode="reference" aria-pressed="false">标准答案</button></div><div class="view-buttons"><button data-view="iso" aria-pressed="true">等轴</button><button data-view="front" aria-pressed="false">前视</button><button data-view="top" aria-pressed="false">俯视</button><button data-view="right" aria-pressed="false">右视</button><button data-reset>重置对比</button></div></div>
<div class="comparison-labels"><span>生成模型</span><span>标准答案</span></div><div class="preview-host">{_preview(row, default_frame(row))}</div><div class="viewer-mount"></div>
<div class="opacity-controls" hidden><label>生成模型透明度 <input data-opacity="candidate" type="range" min="0.05" max="1" step="0.05" value="0.65"></label><label>标准答案透明度 <input data-opacity="reference" type="range" min="0.05" max="1" step="0.05" value="0.35"></label></div>
<div class="viewer-difference-legend" hidden>{_difference_legend((row.get('display') or {}).get('views', {}).get(initial_frame, {}) or {})}</div><div class="viewer-footer"><span class="view-state" aria-live="polite">等轴 ISO</span><p class="viewer-status" role="status">静态预览，三维交互需启动本机服务。</p></div></section>
{_multiview_html(row, initial_frame)}
<section class="case-results"><div class="result-heading"><h3>测试结果 <span class="state {state}">{label}</span></h3>{_score_html(row, detail=True)}</div><p class="case-verdict">{_case_verdict(row)}</p>{range_text}
{_notes_html('测量限制', row.get('measurement_notes'))}{_notes_html('停止原因详情', failure_notes, folded=True)}
<div class="result-columns"><section class="metric-panel"><h3>指标分数</h3>{metric_table(row)}<details class="diagnostics"><summary>辅助指标（不计入总分）</summary>{metric_table(row, diagnostics=True)}</details></section><section class="case-analysis"><h3>结论依据</h3>{case_analysis(row, mode)}{_health_html(row)}</section></div>
{_dimension_findings(row)}{_notes_html('几何核对详情', row.get('enterprise_geometry_notes'), folded=True)}{_cad_html(row)}{_download_html(row.get('downloads'))}</section>
{input_materials_html(row.get('input_materials'))}
{prompt}{_notes_html('候选自述的假设与简化', row.get('candidate_notes'), folded=True)}
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


def _cad_html(value, *, include_backend=False):
    """Show useful context; technical provenance stays in the embedded data."""
    export = value.get('trusted_export') or {}
    fields = []
    if include_backend and value.get('cad_backend'):
        backend = value['cad_backend']
        fields.append(('建模软件', {'solidworks': 'SolidWorks', 'fusion': 'Autodesk Fusion'}.get(backend, backend)))
    stage = value.get('failure_stage')
    if stage:
        stages = {'preparation': '准备', 'preflight': '环境检查', 'modeling': '建模',
                  'trusted_export': '模型导出', 'evaluation': '评分', 'scoring': '评分'}
        fields.append(('失败阶段', stages.get(stage, stage)))
    items = [f'<span>{esc(label)}：{esc(item)}</span>' for label, item in fields if item is not None]
    for warning in export.get('warnings') or []:
        if warning.get('classification') == 'allowed':
            phase = {'native_reopen': '模型打开', 'step_export': '模型导出'}.get(warning.get('phase'), 'CAD')
            items.append(f'<span>{phase}提示：{esc(warning.get("reason") or warning.get("name"))}</span>')
    return '<div class="health-line">' + ''.join(items) + '</div>' if items else ''


def _batch_context(data):
    stats = data.get('enterprise_summary') or {}
    if not stats:
        return _notes_html('运行说明', data.get('report_notes'), folded=True)
    complete_mean = stats.get('complete_main_metrics_mean')
    coverage = stats.get('full_batch_weighted_measurement_coverage')
    text = (f'<p>三项主指标完整的 {esc(stats.get("complete_main_metrics_cases"))} 题均分 {score(complete_mean)}；'
            f'全部 {esc(stats.get("selected_cases"))} 题的主指标实测权重覆盖 {percent(coverage)}。</p>')
    notes = [f'正式尝试 {stats.get("formal_attempts")} 次，基础设施重跑 {stats.get("infrastructure_retries")} 次，按分数追加重试 {stats.get("score_based_retries")} 次。',
             '模型服务身份仅有请求记录，使用受信任同账户运行。',
             '本批只代表选定的十个企业单零件；尺寸探针由标准模型派生，不构成完整工程图尺寸或 GD&T 认证。']
    return text + _notes_html('运行说明', notes + list(data.get('report_notes') or []), folded=True)


def render_html(data):
    """Static results remain readable without JavaScript, HTTP or WebGL."""
    summary, cases, mode = data.get('summary', {}), data.get('cases', []), data.get('coordinate_mode', 'geometry')
    stats = [('已评分记录均分', score(summary.get('batch_mean'))), ('参与评分', f'{summary.get("participating_case_count", 0)} / {len(cases)}'),
             ('完整评分', summary.get('complete_case_count', 0)), ('缺测下界', summary.get('lower_bound_case_count', 0)),
             ('模型无效', summary.get('invalid_case_count', 0)), ('验证未完成', summary.get('validation_incomplete_case_count', 0))]
    if summary.get('pipeline_unscored_case_count'):
        stats.append(('未评分', summary['pipeline_unscored_case_count']))
    stat_html = ''.join(f'<span><span class="summary-label">{label}</span><b>{esc(value)}</b></span>' for label, value in stats)
    cards = []
    for index, row in enumerate(cases):
        state, label = case_state(row)
        cards.append(f'<a class="case-card" href="#fixture={quote(str(row.get("case", "")), safe="")}" data-index="{index}" data-state="{state}" data-task="{_task_type(row)}"><div class="card-preview"><div class="comparison-labels"><span>生成模型</span><span>标准答案</span></div>{_preview(row, default_frame(row))}</div><div class="card-body"><b>{esc(_case_title(row))}</b></div></a>')
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    weight_sets = {tuple((row.get('weights') or {}).get(k) for k in ('shape', 'dimension', 'mass')) for row in cases if row.get('participates_in_scoring')}
    weights_note = '各题采用的权重见单题指标表。'
    if len(weight_sets) == 1:
        weights_note = '本批次总分权重依次为 ' + '、'.join(percent(v) for v in next(iter(weight_sets))) + '。'
    mode_note = '刚体对齐后比较几何，坐标差异单独查看' if mode == 'geometry' else '使用原始坐标比较几何'
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(data.get('run_name'))} - CAD 评测结果</title><link rel="stylesheet" href="assets/report.css"></head>
<body data-report-template="comparison-first-v2"><main><header class="report-header"><div class="report-heading"><h1>{esc(str(data.get('run_name') or 'CAD 评测').replace(' · ', ' '))}</h1><p class="subtitle">{len(cases)} 个案例。左侧生成模型，右侧标准答案。</p></div></header>
<nav class="page-nav"><a href="#overview">案例列表</a><a href="#analysis">评测分析</a></nav>
<section id="overview" class="overview"><div class="filters"><input aria-label="搜索题号或任务" id="case-search" type="search" placeholder="搜索题号、企业编号或名称" autocomplete="off"><select aria-label="筛选任务类型" id="task-filter"><option value="all">全部任务</option><option value="generation">生成</option><option value="editing">编辑</option><option value="unknown">未标注</option></select><select aria-label="筛选状态" id="case-filter"><option value="all">全部状态</option><option value="measured">已评分</option><option value="lower_bound">缺测下界</option><option value="invalid">模型无效</option><option value="incomplete">验证未完成</option><option value="pipeline">未评分</option><option value="integrity">结果无法核验</option></select><select aria-label="排序" id="case-sort"><option value="case">按题号</option><option value="desc">分数从高到低</option><option value="asc">分数从低到高</option></select><span id="visible-count">{len(cases)} 个案例</span></div>
<div class="case-grid">{''.join(cards)}</div><p id="empty-filter" hidden>没有符合筛选的案例。</p></section>
<div id="case-details">{''.join(_case_detail(row, i, mode, len(cases)) for i, row in enumerate(cases))}</div>
<section id="analysis" class="appendix"><h2>评测分析</h2><div class="summary-line">{stat_html}</div><p>均分只统计参与评分的记录，包含缺测下界；未评分题不按零分解释。</p>{_batch_context(data)}{_task_summary(cases)}{_cad_html(data, include_backend=True)}{_download_html(data.get('downloads'), batch=True)}{_score_legend()}<h3>指标含义</h3><p>{mode_note}。形状符合度衡量体积重合、表面接近程度与拓扑结构；尺寸符合度检查外形尺寸、直径和几何特征；材料分布检查体积与主惯性矩。{weights_note}</p><p>体积重合较高仍可能遗漏小孔或薄壁。尺寸检查未覆盖完整图纸公差，体积与惯性一致也不能唯一确定形状。几何对齐仅允许平移和旋转。</p><details><summary>指标消融</summary>{_analysis_html(data.get('analysis'))}</details></section>
</main><dialog id="image-lightbox" class="image-lightbox" aria-label="图片放大"><div class="lightbox-toolbar"><p class="lightbox-caption" aria-live="polite"></p><button data-lightbox-close aria-label="关闭放大图片">关闭 ×</button></div><img class="lightbox-image" alt=""><div class="lightbox-controls"><button data-lightbox-step="-1" aria-label="上一张图片">← 上一张</button><span class="lightbox-count" aria-live="polite"></span><button data-lightbox-step="1" aria-label="下一张图片">下一张 →</button></div></dialog><script id="report-data" type="application/json">{encoded}</script><script src="assets/report.js" defer></script></body></html>'''


def _write_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def build_report(source, output, *, pipeline=None, analysis=None, meshes=True, timeout=60.0, run_name=None, inputs=None, interfaces=None):
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
                row.update({key: materials[row['case']][key] for key in ('task_type', 'prompt')
                            if key in materials[row['case']]})
    interface_sources = None
    if interfaces is not None:
        from .report_interfaces import load_interfaces
        interface_sources = load_interfaces(interfaces, bundle.model_sources)
    try:
        from .report_downloads import build_model_downloads
        downloads = build_model_downloads(bundle.model_sources, output)
        data['downloads'] = {'submission_zip': downloads['submission_zip']}
        for row in data.get('cases', []):
            row['downloads'] = downloads['cases'].get(row['case'], {})
    except Exception:
        data.setdefault('warnings', []).append('模型下载包生成失败；评分和模型预览不受影响。')
    assets=BASE/'report_assets'
    if not assets.is_dir(): raise ValueError('report assets are absent from this source distribution')
    shutil.copytree(assets,output/'assets')
    display={}
    if meshes and bundle.model_sources:
        try:
            from .display_assets import build_display_assets
            options = {'interfaces': interface_sources} if interface_sources is not None else {}
            display=build_display_assets(bundle.model_sources,output,timeout=timeout,workers=2,**options)
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
              'report_generator':{name:hashlib.sha256((BASE/name).read_bytes()).hexdigest() for name in ('src/report_data.py','src/report_inputs.py','src/report_interfaces.py','src/report_downloads.py','src/html_reporting.py','serve_report.py') if (BASE/name).is_file()}}
    if interface_sources is not None:
        manifest['interface_sources'] = {case: {'gt_sha256': entry['gt_sha256'],
            'regions': [{key: region[key] for key in ('sha256', 'fit_type')} for region in entry['regions']],
            'status': 'unavailable' if entry.get('error') else 'verified'}
            for case, entry in interface_sources.items()}
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
