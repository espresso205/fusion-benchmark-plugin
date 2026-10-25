"""Human and tabular views of the exact same ablation document."""
import csv
import io


METRICS = ('shape', 'dimension', 'distance', 'mass', 'face_match', 'section')
COHORT_LABELS = {'complete_measurements': '正式计分项完整实测（主分析）', 'policy_awards': '全部参与者（政策授分分析）'}


def cell(value):
    """Keep labels and evidence text inside one inert Markdown table cell."""
    return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')


def fmt(value, scale=1):
    return '—' if value is None else f'{value * scale:.6f}'


def csv_text(document):
    stream = io.StringIO(newline='')
    fields = ['source_id', 'source_protocol_id', 'coordinate_mode', 'cohort', 'variant', 'removed_metric', 'mode',
              'case', 'score', 'baseline_score', 'delta', 'rank', 'baseline_rank', 'rank_change',
              'original_weight_coverage', 'retained_weight_coverage', 'score_kind']
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for analysis in document['analyses']:
        for cohort_name, cohort in analysis['cohorts'].items():
            for variant in cohort['variants']:
                for row in variant['ranking']:
                    data = {name: row.get(name) for name in fields}
                    data.update(source_id=analysis['source_id'], source_protocol_id=analysis['source_protocol_id'],
                                coordinate_mode=analysis.get('coordinate_mode'),
                                cohort=cohort_name, variant=variant['id'], removed_metric=variant['removed_metric'], mode=variant['mode'])
                    # These columns are labels, not formulas when opened in a spreadsheet.
                    for key, value in data.items():
                        if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                            data[key] = "'" + value
                    writer.writerow(data)
    return stream.getvalue()


def _correlation_table(matrix):
    lines = ['|指标|' + '|'.join(METRICS) + '|', '|---|' + '---:|' * len(METRICS)]
    for left in METRICS:
        values = []
        for right in METRICS:
            stat = matrix[left][right]
            value = fmt(stat['value'])
            reason = f"; {cell(stat['reason'])}" if stat.get('reason') else ''
            values.append(f"{value} (n={stat['n']}{reason})")
        lines.append('|' + left + '|' + '|'.join(values) + '|')
    return lines


def markdown_text(document):
    lines = ['# CAD 评测器离线指标消融', '',
        '本报告为探索性分析，正式评分、权重和排名资格均未改变。JSON/CSV分数范围为0–1；下表均分和差值显示为0–100分。', '',
        '主分析固定使用正式计分项均有实测的共同样本集合：v3/历史六项，v4三项。政策授分分析包括所有原本参与评分的样本。相关矩阵只使用实际测量的配对值，缺测不会填成实测零分。', '',
        '移除一项的两种算法：置零不归一 S−w×a；剩余权重归一 (S−w×a)/(1−w)。validation不参与消融。', '',
        '下方为**样本排序**，不应把不同题目的样本名次解释为模型榜单。原覆盖率保持不变；保留指标覆盖率只描述消融后保留的权重。', '']
    for analysis in document['analyses']:
        counts = analysis['counts']
        is_v4 = analysis['source_protocol_id'] == 'fusion-step-adapted-v4.0'
        complete_label = '三项正式计分完整实测' if is_v4 else '六项完整实测'
        lines += [f"## {cell(analysis['source_id'])}", '', f"来源协议：{cell(analysis['source_protocol_id'])}；坐标模式：{cell(analysis.get('coordinate_mode') or '历史固定口径')}。", '',
            f"样本 {counts['total']}，参与 {counts['participating']}，{complete_label} {counts['complete']}，排除 {counts['excluded']}。",
            f"评测器/执行故障案例 {counts['evaluator_failure_cases']}；案例故障率 {fmt(counts['evaluator_failure_rate'], 100)}%。", '',
            '缺测数量：' + '；'.join(f'{name}={counts["unavailable_metric_counts"].get(name, 0)}' for name in METRICS) + '。', '']
        for warning in analysis.get('warnings', []):
            lines += [f'- {cell(warning)}']
        for cohort_name, cohort in analysis['cohorts'].items():
            label = complete_label+'（主分析）' if cohort_name=='complete_measurements' else COHORT_LABELS[cohort_name]
            lines += ['', f'### {label}', '', f"固定样本数：{cohort['case_count']}。", '',
                '|变体|均分 /100|相对基线 /100|Spearman|Kendall τ-b|', '|---|---:|---:|---:|---:|']
            baseline = next(v for v in cohort['variants'] if v['id'] == 'baseline')['mean_score']
            for variant in cohort['variants']:
                mean = variant['mean_score']
                delta = mean - baseline if mean is not None and baseline is not None else None
                comp = variant['rank_comparison']
                lines.append(f"|{cell(variant['id'])}|{fmt(mean,100)}|{fmt(delta,100)}|{fmt(comp['spearman']['value'])}|{fmt(comp['kendall_tau_b']['value'])}|")
            for method in ('pearson', 'spearman'):
                lines += ['', f'#### {method.title()} 实测相关矩阵', '']
                lines += _correlation_table(cohort['correlations'][method])
            lines += ['', '#### 逐样本消融与名次变化', '',
                      '完整逐行记录见 ablation.csv；每个变体均保留原分、消融分、名次变化及两种覆盖率。以下列出名次变化最大的5条。', '',
                      '|样本|变体|消融分 /100|原名次|新名次|名次提升|原覆盖率|保留指标覆盖率|', '|---|---|---:|---:|---:|---:|---:|---:|']
            changes = [(variant['id'], row) for variant in cohort['variants'] if variant['id'] != 'baseline'
                       for row in variant['ranking'] if row['rank_change'] is not None]
            changes.sort(key=lambda pair: (-abs(pair[1]['rank_change']), pair[0], pair[1]['case']))
            for variant_id, row in changes[:5]:
                lines.append(f"|{cell(row['case'])}|{cell(variant_id)}|{fmt(row['score'],100)}|{fmt(row['baseline_rank'])}|{fmt(row['rank'])}|{fmt(row['rank_change'])}|{fmt(row['original_weight_coverage'],100)}%|{fmt(row['retained_weight_coverage'],100)}%|")
        excluded = [row for row in analysis['records'] if not row['participates']]
        if excluded:
            lines += ['', '### 未参与评分的样本', '', '|样本|源状态|原因|', '|---|---|---|']
            for row in excluded:
                lines.append(f"|{cell(row['case'])}|{cell(row['status'])}|{cell('; '.join(row['exclusion_reasons']))}|")
    comparison = document['system_comparisons']
    lines += ['', '## 显式系统身份比较', '']
    if comparison['status'] != 'available':
        lines += ['未生成系统榜单：' + cell(', '.join(comparison.get('reasons', [])) or '未提供至少两个可比较的系统身份') + '。']
    else:
        lines += ['仅比较相同协议、坐标模式、权重、题集及逐题GT/标注身份；系统名为使用者声明，结果不取得正式排名资格。']
        for name, cohort in comparison['cohorts'].items():
            lines += ['', '### ' + COHORT_LABELS[name], '']
            if cohort['status'] != 'available':
                lines += ['不可计算：' + cell(cohort['reason'])]
                continue
            lines += [f"共同题目数：{cohort['case_count']}。", '', '|变体|系统|均分 /100|名次|名次提升|', '|---|---|---:|---:|---:|']
            for variant in cohort['variants']:
                for row in variant['ranking']:
                    lines.append(f"|{cell(variant['id'])}|{cell(row['system'])}|{fmt(row['score'],100)}|{fmt(row['rank'])}|{fmt(row['rank_change'])}|")
    lines += ['', '## 证据与限制', '',
        '- ablation.json 包含来源文件SHA256、分析代码SHA256、原始状态、每个统计量的样本数与不可计算原因。',
        '- 置零不归一变体的理论满分为1−被移除权重；不能把它与归一变体的绝对分值混为一谈。',
        '- 这里没有重跑几何或构造受控错误，不能单凭分数变化推断指标的错误识别能力。',
        '- v4仅消融正权重的正式计分项；诊断项保留实测相关性，不生成移除零权重项的无效分数比较。',
        '- 旧实验数据、v3、v4及不同坐标模式分别分析。重复候选、同一模型不同题目及不同评测协议不构成独立模型排行榜。', '']
    return '\n'.join(lines)
