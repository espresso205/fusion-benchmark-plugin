"""Write new/old comparisons without imputing blocked scores."""
import csv
from pathlib import Path
from .io_utils import strict_json


def report(output, manifest, summary):
    if summary.get('protocol_id') == 'fusion-step-adapted-v3.0':
        return report_v3(output, manifest, summary)
    angles = ('validity','shape','dimension','distance','mass','face_match','section')
    rows = []
    lines = ['# STEP 评测器 v2 影子复评', '',
        '这是保留原权重的 GT 派生探针适配评测；不具备正式排名资格。', '',
        '参考测量或执行契约失败的题目不计零分、不删题、不重新分配权重；因此批次均分可能为空。', '',
        '下表将执行、几何有效性、测量质量和分数聚合分别列出。兼容字段 `status` 仍保留旧状态名；判读是否几何无效请看 GT/候选几何列。', '',
        '|题目|v1 历史分 /100|执行|几何（GT/候选）|测量质量|模块覆盖|v2 分 /100|阻断原因|',
        '|---|---:|---|---|---|---:|---:|---|']
    source = {r['case']: r for r in manifest['cases']}
    for final in summary['cases']:
        case = final['case']
        folder = output/case
        old_dir = source[case].get('historical_results')
        old_final = strict_json(Path(old_dir)/'final_scores.json')['cases'][0]['final_score'] if old_dir else None
        preflight = strict_json(folder/'reference_preflight.json') if (folder/'reference_preflight.json').exists() else {}
        new_score = final['final_score']
        old_text = f'{old_final*100:.3f}' if old_final is not None else '—'
        new_text = f'{new_score*100:.3f}' if new_score is not None else '不聚合'
        execution = final.get('execution', {}).get('status', 'unknown')
        geometry = final.get('geometry', {})
        geometry_text = f"{geometry.get('reference_status', 'unknown')}/{geometry.get('candidate_status', 'unknown')}"
        quality_values = final.get('quality_flags') or [final.get('quality_status') or 'unknown']
        quality = '; '.join(str(value) for value in quality_values)
        profile = final.get('scores', {}).get(final.get('protocol_id'), {})
        coverage = profile.get('coverage', {})
        coverage_text = f"{coverage.get('scored_modules', 0)}/{coverage.get('required_modules', len(angles))}"
        blocked = profile.get('blocked_reasons', [])
        blocked_text = '; '.join(
            f"{reason.get('module') or '聚合'}: {reason.get('detail') or reason.get('status', 'blocked')}"
            for reason in blocked if isinstance(reason, dict)
        ).replace('|', '\\|') or '—'
        lines.append(f"|{case}|{old_text}|{execution}|{geometry_text}|{quality}|{coverage_text}|{new_text}|{blocked_text}|")
        for angle in angles:
            p = folder/'results'/f'{angle}.json'
            new = strict_json(p) if p.exists() else {}
            old = strict_json(Path(old_dir)/f'{angle}.json')['cases'][0] if old_dir else {}
            metric = final.get('metrics', {}).get(angle, {})
            oscore, nscore = old.get('score'), metric.get('score')
            rows.append({'case': case, 'module': angle, 'old_status': old.get('status'), 'old_score': oscore,
                'new_status': new.get('status','missing'), 'new_score': nscore,
                'new_diagnostic_score': metric.get('diagnostic_value'), 'new_metric_quality': metric.get('quality'),
                'delta': nscore-oscore if nscore is not None and oscore is not None else None,
                'compute_s': new.get('timing',{}).get('compute_s'), 'aggregate_status': final['status'],
                'quality_status': final.get('quality_status'),
                'reference_geometry_status': final.get('geometry',{}).get('reference_status'),
                'candidate_geometry_status': final.get('geometry',{}).get('candidate_status')})
    lines += ['', '## 协议变化与解释', '',
        '- 新增 `execution`、`geometry`、`metrics`、`scores`、`failure` 分层字段；模块测量不可靠不会被表述为参考实体无效。当前仍只有既有 v2 适配评分协议，没有临时增加或重归一 core/extended 权重。',
        '- section：拓扑图闭环和真曲线积分；开链、分支、共面、切触或未声明空参考阻断评分。没有移动探测平面以追求高分。',
        '- face_match：无向支撑轴、平面符号规范化、圆锥/环面位置及有限域摘要检查；仍是一对一面匹配，不承诺拆面/并面不变。',
        '- dimension：GT 自检门控，去除参与判定的四舍五入；现有探针仍不覆盖完整孔位/深度/GD&T。',
        '- distance：主分公式、5 万点与 seed=0 保留；对称均值修正为两方向 mean 的平均，原求和值另列。采样底噪尚未消除。',
        '- shape：固定世界坐标计分；aligned 单独诊断并检查失败，复用进程内解析/网格缓存。',
        '- mass：保留原公式并新增默认/1e-6/1e-8 积分稳定性门控（体积和惯性相对极差均不超过 1e-6）；不稳定时仅保留旧公式诊断值。惯性使用质心张量，不重复移轴。',
        '- Boolean：历史双向差整体标为 diagnostic_unreliable，禁止作为严格等价证据。', '',
        '## 运行证据', '',
        f"- 墙钟：{summary['wall_s']:.3f} s；case 并发：{manifest['workers']}；每 case 超时：{manifest['timeout_per_case_s']} s。",
        f"- 执行期间代码未变：{summary['code_unchanged']}；原输入未变：{summary['original_inputs_unchanged']}。",
        '- manifest.json 绑定脚本、vendored 后端、依赖、权重、输入与标注 SHA256。',
        '- 每题 reference_preflight.json、results/*.json、aggregate.json、worker.log 和 events/ 保留状态与原始量；`aggregate.json` 内新字段摘要给出指标质量和证据文件引用。',
        '- comparison.csv 的 `new_score` 只保留契约有效的正式模块分；被拒绝的原始分留在 `new_diagnostic_score`，不参与差值。缺失或无效测量的正式分留空，不填零。', '',
        '## 仍需工作', '',
        '本轮 GT 预检仍随每题评测 worker 执行，尚未前移到冻结的 reference registry。统一服务/API、幂等恢复、可信 F3D 导出和访问沙箱也未交付。独立图纸语义标注、分面不变的 patch 覆盖、点到面距离与多 seed 校准、Boolean 根因及完整突变矩阵仍待实现。',
        '旧 144.928 s 仅作历史参考；本次新增参考预检和收敛诊断，不能据此直接声称同工作量提速。']
    with (output/'comparison.csv').open('x', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with (output/'report.md').open('x', encoding='utf-8') as f:
        f.write('\n'.join(lines)+'\n')


def report_v3(output, manifest, summary):
    """Show awarded credit separately from unavailable physical measurements."""
    names = ('shape', 'dimension', 'distance', 'mass', 'face_match', 'section')
    def fmt(score):
        return f'{score*100:.3f}' if isinstance(score, (int, float)) else '—'
    lines = ['# Fusion STEP 评测 v3', '',
        'validation 通过的产物全部参与固定权重评分。六项权重为 30%、25%、15%、10%、10%、10%。', '',
        '测量失败先采用预定计算路径；仍未测得的项不获得分值。该项实测分保持为空，总分标为保守计分，明确显示可得分范围；不重分配权重，不把未测得说成实测零分。', '',
        '本报告仍为适配评测，不具备正式排名资格。分数范围描述未测项可能获得的分值，不是数值误差的置信区间。', '',
        f"参与评分：{summary.get('participating_case_count', 0)}/{summary.get('total_case_count', len(summary['cases']))}；参与者均分：{fmt(summary.get('batch_mean'))} /100。", '',
        '|题目|validation|总分 /100|分数类型|实测权重覆盖|shape|dimension|distance|mass|face_match|section|',
        '|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|']
    rows = []
    for case in summary['cases']:
        values = []
        for name in names:
            metric = case.get('metrics', {}).get(name, {})
            measured = metric.get('measured_score')
            award = metric.get('awarded_score')
            values.append(fmt(measured) if measured is not None else '未测得（计0）' if award == 0 else '—')
            rows.append({'case': case['case'], 'module': name, 'measured_score': measured,
                'awarded_score': award, 'new_score': measured, 'status': metric.get('status'),
                'score_source': metric.get('score_source'), 'backend': metric.get('backend'),
                'final_score': case['final_score'], 'score_kind': case.get('score_kind'),
                'validation_passed': case.get('validation_passed'),
                'participates_in_scoring': case.get('participates_in_scoring')})
        kind = '完整实测' if case.get('score_kind') == 'measured' else '保守计分' if case.get('participates_in_scoring') else '未放行'
        validation = case.get('geometry', {}).get('candidate_status', 'unknown')
        lines.append(f"|{case['case']}|{validation}|{fmt(case['final_score'])}|{kind}|{case.get('measurement_weight_coverage', 0)*100:.1f}%|" + '|'.join(values) + '|')
    lines += ['', '## 逐题说明', '']
    for case in summary['cases']:
        lines.append(f"### {case['case']}")
        lines.append('')
        interval = case.get('score_interval')
        if interval:
            lines.append(f"固定权重总分：{fmt(case['final_score'])}；未测项可得分范围：{fmt(interval[0])}–{fmt(interval[1])} /100。")
        for reason in case.get('reasons', []):
            lines.append(f"- {reason.get('module', 'execution')}: {reason.get('detail', reason.get('status', 'unknown'))}")
        for name in names:
            m = case.get('metrics', {}).get(name, {})
            if m.get('backend'):
                lines.append(f"- {name}: {m['backend']}；{m.get('score_source')}。")
        lines.append('')
    lines += ['## 可复核证据', '',
        '- manifest.json 冻结协议、权重、代码、依赖和输入哈希。',
        '- 每题 section_plan.json 记录仅由参考与固定规则生成的截面定义；results/*.json 保存实测量、后端及误差信息。',
        '- aggregate.json 同时保存 measured_score 与 awarded_score；二者不能混用。',
        f"- 运行时长：{summary['wall_s']:.3f} 秒；代码未变：{summary['code_unchanged']}；原输入未变：{summary['original_inputs_unchanged']}。"]
    with (output/'comparison.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (output/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
