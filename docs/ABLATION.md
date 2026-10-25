# 离线指标消融

本工具只读取已完成的评测 JSON，不调用 Fusion、模型或几何 worker，不改评分算法、权重或历史结果。Python 3.12 标准库即可运行，无需安装 OCCT/numpy/scipy。输出属于探索性分析，`ranking_eligible=false`。

## 使用

独立评测器仓库：

```powershell
python -B analyze_ablation.py --input C:/benchmark/completed-run --output C:/benchmark/analysis/new-run
```

完整插件源码：

```powershell
python -B fusion-benchmark-bootstrap.py ablation --input C:/benchmark/completed-run --output C:/benchmark/analysis/new-run
```

包含本功能的新便携构建可使用 `fusion-benchmark.cmd ablation ...`。旧版 ZIP 不会因源码更新自动获得本命令。

`--input` 支持一个或多个结果目录或入口 JSON：

- 当前 v4 与历史 v3：包含 `summary.json`、`manifest.json`、逐题 `aggregate.json` 和 `results/*.json` 的完整结果目录；也可指定其中的 `summary.json`。v4 同时验证坐标模式与派生量测身份。
- 历史 `legacy-20261017`：旧六指标实验根目录，包含 `results/final_scores.json` 和七个角度 JSON；`weights.json`、`cases.json` 存在时也核对并记录。也可传 `final_scores.json`。
- v2 明确拒绝；不能将它解释成 v3。v2 兼容读取不在此版本范围。

新输出目录必须不存在，且与历史输入目录分开；不允许输出到任何历史 run 内。结果可搬移后分析，不要求原机器上的 candidate/GT 路径仍可访问。工具校验的是保存的 JSON 证据，**不会重新验证原几何或证明输入模型现在仍与历史哈希一致**。

多个目录同名时，用 manifest 指定不同来源 ID。相对路径以 manifest 所在目录解析：

```json
{
  "sources": [
    {"id": "run-a", "path": "../runs/a/evaluation"},
    {"id": "run-b", "path": "../runs/b/evaluation"}
  ]
}
```

```powershell
python -B analyze_ablation.py --manifest sources.json --output analysis/new-comparison
```

输入错误退出码为 2，不生成成功报告；写盘中断时保留 `INCOMPLETE`，不能将该目录当作交付结果。任何已有输出目录均不覆盖。

## 输出

|文件|内容|
|---|---|
|`ablation.md`|中文报告、变体均分、排序相关、实测相关矩阵、主要名次变化和排除原因|
|`ablation.csv`|每个来源、cohort、变体、样本的完整得分/名次/覆盖率；缺失值留空|
|`ablation.json`|完整结构化结果、源状态、统计样本数/不可计算原因、输入JSON及分析代码SHA256|

JSON/CSV 使用0–1分数；Markdown均分与分差显示0–100分。CSV为UTF-8 BOM，文字标签中的潜在电子表格公式带保护前缀。JSON保存原始标签。

`schema_version=fusion-ablation/1`，`analysis_version=1.0.0`。`source_protocol_id` 区分当前 v3 与旧实验，各来源单独分析。旧结果从各指标文件的原始精度重建，不使用 `final_scores.json` 的六位小数展示列作计算输入。

## 两种消融

六指标为 shape、dimension、distance、mass、face_match、section。读取来源权重，不重新校准。对原授分 a_i、权重 w_i，基线 S=Σw_i a_i；移除 j：

- `without_<metric>_zero_weight`：Σ(i≠j) w_i a_i，满分为1−w_j。
- `without_<metric>_renormalized`：Σ(i≠j) w_i a_i / Σ(i≠j) w_i，满分仍为1。

v3/legacy 基线加六项各两种模式，共13个变体；v4 仅对正权重的 shape/dimension/mass 消融，共7个变体。诊断项无授分，不制造“删除零权重项”的伪变体。validation独立保留，不做“删除准入门”的消融。若删除唯一有正权重的项，归一模式无定义，分数、排名和保留指标覆盖率均为null并附原因；置零模式仍为0。

`rank_change=baseline_rank-rank`，正数表示名次上升。并列使用未舍入数值的平均名次，如两个并列第一均为1.5。指标保持原 `score_kind`，删除唯一缺测项也不会改写历史测量标签。

## 样本集合与缺测

- `complete_measurements`：原本参与评分且必需计分项都有实测的共同样本集；v3要求六项，v4要求三项主分。各变体保持同一集合，作为指标价值的主分析。
- `policy_awards`：全部原参与者，保留 v3 缺测实测值null、授分0的政策，作为固定权重影响分析。
- 未通过validation、完整性失败、未完成验证等记录保留在 `records` 和排除清单中，不凭空授分。
- `original_weight_coverage` 保留原值；`retained_weight_coverage` 为剩余指标实测权重之和/全部剩余权重之和。后者变高不表示原评测质量提高。
- 每项缺测数量统计参与评分的题目；评测器故障率按全题数为分母，每题最多计一次。明确的worker错误/超时/非零退出或模块 `evaluator_error` 计为故障，候选或参考几何无效本身不算评测器故障。

v3参与者的原总分、缺测政策、coverage与保存证据会被核对。身份错配、被改写的manifest、summary/aggregate不一致、非有限分数或无法重建总分时停止分析。

v4 另外区分 `scored` 与 `diagnostic`：诊断未测不改变主分完整集合或政策授分，相关统计仍只取实际测量。geometry 和 strict 的跨系统比较被拒绝，不能将两个坐标合同当作同口径评测。

## 相关性与系统比较

Pearson/Spearman矩阵只使用对应两项均有实测的样本，不把政策0混入。每格给出 `n`，常量列或不足2个配对样本返回null及原因，不写NaN或伪装为0相关。

排序变化用Spearman与处理并列的Kendall τ-b。Kendall当前采用O(n²)配对计数，适合数十到数百样本的本轮实验，不适合直接输入百万级记录。

默认是**样本排序**；不同题目的候选不能称为模型排行榜。需要探索系统顺序时，在manifest中给至少两个来源显式声明不同的 `system`：

```json
{
  "sources": [
    {"id": "system-a-run", "system": "A", "path": "../runs/a/evaluation"},
    {"id": "system-b-run", "system": "B", "path": "../runs/b/evaluation"}
  ]
}
```

系统比较要求相同来源协议、六项权重、完整题集以及逐题GT和标注哈希。否则 `system_comparisons.status=not_applicable` 并列原因。主分析取各系统共同完全实测题集；政策分析要求相同参与题集，防止只保留各自容易完成的题目后比较均分。系统身份为使用者声明，工具不认证模型名称。

同名协议也不保证不同机器、依赖版本、参数或代码实现完全相同；跨运行实验仍需组织者冻结这些实验条件。保存JSON的内部一致性不等于独立签名认证。

当前工具不生成受控错误，也不计算未经标签/阈值定义的识别率。指标冗余和排序变化只是证据的一部分；敏感性、选择性、单调性、重复性及漏误检验证属于后续受控错误功能。

## 验证与同步

```powershell
python -B -S -m unittest discover -s tests -p "test_ablation*.py" -v
python -B -S -m unittest discover -s tests -p "test_result_reader.py" -v
python -B -S -m unittest discover -s tests -p "test_system_comparison.py" -v
```

插件另有 `test_ablation_entrypoint.py`，通过真实启动器验证无config、无管理服务和无几何依赖的分派，并检查便携构建白名单。

两仓库的 `analyze_ablation.py`、`analysis/*.py` 和本说明保持字节一致。分析模块独立于 `src/` 评分核心；该功能不更新冻结的评分来源清单。以后修改评分公式须按各仓库AGENTS要求另外更新协议/来源证据。
