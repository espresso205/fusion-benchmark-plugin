"""Human-readable core scores, separate coordinate and diagnostic evidence."""
import csv
from .contract_v4 import DEFAULT_WEIGHTS,SCORED_METRICS


def report(output,manifest,summary):
    def fmt(value):
        return f'{100*value:.3f}' if isinstance(value,(int,float)) else '—'
    mode=summary['coordinate_mode']
    lines=['# Fusion STEP 评测 v4','',
        '主分保留 CADGenBench IoU、Surface F1、Topology（合为 shape），并补充 dimension、mass。',
        '默认相对权重沿用 30:25:10 后归一；distance、face_match、section 仅作诊断，不进入总分或主分覆盖率。','',
        f"坐标模式：{'几何符合（只允许刚体平移、旋转）' if mode=='geometry' else '严格世界坐标'}。原始模型不修改；坐标偏差单列。",
        '自动对齐不是工程基准或装配定位认证；对称零件可能存在多个等价姿态。不允许缩放、镜像或剪切。',
        '主分缺测保留实测空值、政策授分 0、覆盖率和可得分范围；诊断缺测不扣主分。可得分范围不是数值置信区间。',
        '未经过独立权重/排行榜校准，ranking_eligible=false。','',
        f"参与评分：{summary['participating_case_count']}/{summary['total_case_count']}；参与者均分：{fmt(summary['batch_mean'])} /100。",'',
        '|题目|validation|总分 /100|分数类型|主分实测权重覆盖|shape|dimension|mass|诊断未测|执行|',
        '|---|---|---:|---|---:|---:|---:|---:|---|---|']
    rows=[]
    for case in summary['cases']:
        values=[]
        for name in DEFAULT_WEIGHTS:
            metric=case['metrics'][name]
            measured=metric['measured_score']; award=metric['awarded_score']
            if name in SCORED_METRICS:
                values.append(fmt(measured) if measured is not None else '未测得（计0）' if award==0 else '—')
            rows.append({'case':case['case'],'module':name,'metric_role':metric['metric_role'],
                'coordinate_mode':mode,'measured_score':measured,'awarded_score':award,'new_score':measured,
                'status':metric['status'],'score_source':metric['score_source'],'backend':metric.get('backend'),
                'final_score':case['final_score'],'score_kind':case['score_kind'],'validation_passed':case['validation_passed'],
                'participates_in_scoring':case['participates_in_scoring'],'measurement_weight_coverage':case['measurement_weight_coverage']})
        kind='完整实测' if case['score_kind']=='measured' else '保守计分' if case['participates_in_scoring'] else '未放行'
        missing=', '.join(case['diagnostic_unavailable_metrics']) or '无'
        lines.append(f"|{case['case']}|{case['geometry']['candidate_status']}|{fmt(case['final_score'])}|{kind}|{case['measurement_weight_coverage']*100:.1f}%|"
            +'|'.join(values)+f"|{missing}|{case.get('execution',{}).get('status','unknown')}|")
    for case in summary['cases']:
        lines.extend(['',f"## {case['case']}",''])
        interval=case.get('score_interval')
        if interval: lines.append(f'主分：{fmt(case["final_score"])}；未测项可得分范围：{fmt(interval[0])}–{fmt(interval[1])} /100。')
        pose=case.get('pose',{}); raw=pose.get('raw',{})
        if pose.get('status')=='ok':
            lines.append(f"对齐变换诊断：旋转 {raw.get('rotation_angle_deg')}°，平移向量 {raw.get('translation_mm',raw.get('t'))} mm；对齐 RMSE {raw.get('rmse_mm',raw.get('rmse'))} mm。")
            lines.append('这些量描述自动匹配所用变换，不能直接当作图纸基准公差误差；变换和派生文件哈希见 results/pose.json。')
        else: lines.append(f"对齐诊断：{pose.get('status','未测得')}。{raw.get('error','')}")
        lines.extend(['','|指标|角色|实测 /100|授分 /100|','|---|---|---:|---:|'])
        for name in DEFAULT_WEIGHTS:
            m=case['metrics'][name]
            lines.append(f"|{name}|{'主分' if name in SCORED_METRICS else '诊断'}|{fmt(m['measured_score'])}|{fmt(m['awarded_score'])}|")
        lines.append('')
        for reason in case.get('reasons',[]):
            lines.append(f"- {reason.get('module','执行')}: {str(reason.get('detail',reason.get('status','unknown'))).replace('|','/')}")
        lines.append(f"- 原始与对齐后几何、尺度和各子项证据：{case['case']}/results/；坐标原分见 world_shape.json（geometry 模式）。")
    lines.extend(['','## 运行证据','',
        f"- 代码未变：{summary['code_unchanged']}；原始输入未变：{summary['original_inputs_unchanged']}；墙钟 {summary['wall_s']:.3f} 秒。",
        '- 所有输入、派生几何、协议、坐标模式、权重、依赖与代码均有来源记录。',
        '- dimension 仍是 GT 派生探针；圆柱面数不是孔数，不能替代完整图纸尺寸、孔深或 GD&T 检查。',
        '- mass 衡量体积和质心主惯性矩；既不单独认证形状，也不评价绝对位置和朝向。'])
    with (output/'report.md').open('x',encoding='utf-8') as stream: stream.write('\n'.join(lines)+'\n')
    with (output/'comparison.csv').open('x',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
