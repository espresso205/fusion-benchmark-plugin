# v4：几何符合与坐标诊断

默认协议 `fusion-step-adapted-v4.0`，默认坐标模式 `geometry`。原始候选先通过 BRep 准入；随后仅用平移和 proper rotation 匹配参考，生成有独立 SHA256 的派生 STEP。主分使用该坐标下的 shape、dimension，mass 使用原始候选的体积与质心主惯性矩。原件与历史输出均不改写。

工业场景中，整体重新定位通常不改变单个零件的可制造形状与尺寸；但会影响装配关系、加工工件坐标、机器人位姿及外部基准。更改坐标系定义时若同步维护相应变换，可保持实际装配位置。因此几何评价与坐标交付要求分别表达，不能一概认为原点偏移无关。

```powershell
python -B evaluate.py --cases C:/benchmark/cases.json --output C:/benchmark/new-v4
python -B evaluate.py --cases C:/benchmark/cases.json --output C:/benchmark/new-strict --coordinate-mode strict
python -B evaluate.py --cases C:/benchmark/cases.json --output C:/benchmark/new-v3 --protocol fusion-step-adapted-v3.0
```

`geometry` 为默认；`strict` 在原世界坐标计分。模式写入 manifest、job、每项结果及汇总；不同模式不能合并为同口径系统比较。旧 v3 显式调用时仍为固定世界坐标，旧公式、参数与缺测政策保留。

## 主分与诊断

|模块|角色|默认权重|说明|
|---|---|---:|---|
|shape|计分|6/13 ≈ 46.154%|CADGenBench IoU、Surface F1、Topology 三项等权平均；三个上游函数保持原样|
|dimension|计分|5/13 ≈ 38.462%|原 GT 派生尺寸探针通过率，保留 GT 自检；增加尺寸差、面数差的解释字段|
|mass|计分|2/13 ≈ 15.385%|原 v3 体积与主惯性矩误差/收敛检查；不评价整体位置和朝向|
|distance|诊断|0|双向采样点到对方网格表面的 p99 距离；修复尺度路径|
|face_match|诊断|0|原 v3 严格表面覆盖|
|section|诊断|0|原 v3 双侧固定切面；仍在参考坐标中比较|

权重仅将原 30:25:10 归一，不是对小样本拟合的最优权重。`ranking_eligible=false`。如提供自定义权重，三个主分项必须为正，三个诊断项必须为零，总和为1。

只有计分项缺测才得到 `measured_score=null`、`awarded_score=0`，总分标为 `policy_lower_bound`，披露可得分范围与实测权重覆盖。诊断项始终 `awarded_score=null`，缺测记 `diagnostic_unavailable_metrics`，不影响主分覆盖。覆盖的分母是三个计分模块，不是全部六项。

## 姿态和尺度的证据

`results/pose.json` 记录原始/派生哈希、刚体 R/t、旋转角、平移范数、RMSE、原始包围盒中心差、随机种子以及刚体检查。`derived/candidate_aligned.step` 保留派生模型；几何模式的原世界坐标 shape 另见 `results/world_shape.json`。每项结果同时携带原 `input_hashes` 和实际测量的 `evaluation_input_hashes`。

非同字节候选在验证上游 R/t 后，从原始 BRep 以 STEP `GREATEST` 精度导出派生件，并记录导出策略及导出前后体积；体积相对变化仍须不超过 `1e-5`。

不允许缩放、剪切或镜像。对齐失败时，geometry 的 shape/dimension 不悄悄回退原坐标，未测项按主分缺测政策处理，mass 可继续；strict 对齐诊断失败仍可对原件计分。核心测量先于昂贵诊断，后续超时保留已测核心，但执行未完成必须明确披露。任何证据身份错误会撤销计分。

对齐使用固定 seed 的上游刚体匹配。它不是人工基准配准，不保证全局最优；对称零件可有多组等价姿态，角度/位移不能直接当作工程基准公差。

v4 shape 包装层使用 `AddOptimal(useTriangulation=False)` 的 GT 包围盒作为尺度，双方共用该尺度确定网格挠度；上游函数调用 `align=False`，避免再次对齐。distance 同样使用可靠 GT 尺度和点到网格表面距离，避免将点云采样间距当作表面误差；仍属于有限采样和三角网格近似。

dimension 的圆柱计数是 BRep 面数，不是孔数，失败类型只解释探针，不能替代位置、孔深、完整 GD&T 或人工图纸尺寸真值。mass 也不能区分所有等体积、等惯性异形件。

## 本地服务与插件

MCP 和插件配置支持 `protocol` 与 `coordinate_mode`，默认 v4/geometry；坐标模式只适用于 v4。CLI、服务、插件快照和离线消融必须保留协议和模式。离线完整集合仅要求三个主分项实测；诊断仍可作相关分析，消融只删除正权重项。

当前来源见 `SOURCE_HASHES.json`（独立评测器）或 `SCORING_SOURCE_MANIFEST.json`（插件）；旧清单单独留存，历史评分不由新清单重新解释。完整便携运行时的构建与验收另按发布文档执行。
