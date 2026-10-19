# Fusion STEP v3 评分与使用

协议：`fusion-step-adapted-v3.0`。每个通过候选几何 validation 的产物都参与固定权重评分；validation 与参考测量资格分别记录。输出仍不是经过独立排名校准的正式排行榜成绩。

## 使用入口

便携包从包目录调用：

```powershell
.\runtime\python\python.exe .\evaluate.py --cases C:/benchmark/private/cases.json --output C:/benchmark/private/runs/new-v3 --protocol fusion-step-adapted-v3.0
```

源码维护者可使用已有依赖环境的 Python 执行相同命令。输出目录必须不存在。输入 manifest 的每题需有 case、candidate、gt、annotations 绝对路径和三份文件的 expected_hashes；GT 不得发给建模 Agent。F3D 自动流程应通过插件，由可信重开导出的 STEP 进入这里。

v2 历史规则可显式指定 `--protocol fusion-step-adapted-v2.0`；默认 v3。旧 run 不覆盖。使用便携解释器直接运行脚本时加 `-B`，避免生成字节码改变发布文件集。

## validation

验证每个实体的有效 BRep、闭壳、正体积及有限范围，显式检查空间自交、多实体正体积干涉和不属于实体的残留几何。正常接触与超过容差的体积重叠区分处理。检测器报错不等于确认模型无效。

- 通过：参与评分。
- 确认无效：不放行，保留自交/干涉等证据。
- 未能完成验证：保留产物与错误，不能声称已经通过。
- 输入或结果身份/哈希不符：已有验证证据失效，不能拿别的文件的验证结果继续评分。

## 六项与综合分

|指标|权重|含义|
|---|---:|---|
|shape|30%|固定世界坐标下的体积、表面与拓扑相似度|
|dimension|25%|冻结尺寸标注的检查通过率|
|distance|15%|双向表面采样距离的尾部偏差|
|mass|10%|体积与主惯性矩误差；采用积分收敛与备用网格测量|
|face_match|10%|完整有限曲面区域的双向面积覆盖；距离与法向约束|
|section|10%|参考冻结的双侧截面：分别比较对应侧后求平均|

JSON 内分数为 0–1，报告显示为 0–100。总分严格为六项分乘固定权重之和。validation 单列，不加权。

优先使用真实测量与声明的备用路径。对于备用后端也未能完成的指标：

- `measured_score: null`：没有得到可信实测分。
- `awarded_score: 0`：按照缺失测量不得分政策计入总分，不是声称实测结果为零。
- `score_source: unavailable_policy_floor`，总分的 `score_kind: policy_lower_bound`。
- 其他测量分数保留，不删题，不重新分配权重。`measurement_weight_coverage` 显示实际测得的权重比例。
- `score_interval` 显示未测项可能获得的分值范围；它不是计算误差的保证界限或统计置信区间。

例如五项满分、section 未测得：实测覆盖 90%，固定权重综合计分 90/100，可得分范围 90–100。section 的实测分显示“未测得”，计入分值为 0。恢复该测量后需在新 run 中重评，不能修改历史成绩。

已测但近似的指标另记实际 backend、parameters、uncertainty。积分未达到冻结收敛预算不伪装成合格实测分；诊断估计量保留在原始结果中。

## 报告

每次输出 manifest.json、summary.json、report.md、comparison.csv，以及逐题 results/*.json、aggregate.json、worker.log、reference_preflight.json、section_plan.json。读取总体结果应同时检查：

- validation_passed / participates_in_scoring
- final_score / score_kind
- measurement_weight_coverage / unavailable_metrics
- metrics 中的 measured_score、awarded_score、score_source
- execution 与输入/代码哈希一致性

批次均分涵盖全部 validation 通过且证据一致的参与者，包括保守计分；同时列出参与数量与总题数。确认无效、缺失提交和未完成 validation 的题目保留在报告中，不混入实测平均分。
