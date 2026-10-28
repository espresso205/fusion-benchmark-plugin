# HTML 评测报告

报告使用固定的 `comparison-first-v2` 模板。案例列表只放题号、名称与模型缩略图；单题顺序固定为题名、三维对比、四视角图片、测试结果和结论、折叠的原始资料。分数和状态只在图片下方的结果区展示，批次统计放在评测分析页。三维分屏和静态并排图均为左侧生成模型、右侧标准答案（GT），双方共用视角和尺度。

所有入口使用 `src/html_reporting.py` 与 `report_assets/`，不得生成后再以字符串替换追加第二套分数表或执行说明。企业编号和题名使用 `enterprise_id`、`enterprise_title`；已有核对说明使用 `measurement_notes`、`enterprise_geometry_notes`；简短结论可用 `report_conclusion`，且必须来自既有证据，不能改写评分。候选自述 `candidate_notes` 与任务原文保持折叠。结构化字段保留原始证据，渲染器负责统一布局和转义。

页面只展示建模软件、结果状态和相关提示；软件/桥接/适配器版本、SHA256、导出协议及配置等追溯字段保留在内嵌报告数据中，不占用阅读界面。任务要求按标题、段落、列表、粗体和代码排版，合并段内换行；原始任务文本和评分数据保持不变。任务中的 HTML 与链接按文本显示，不执行或加载外部内容。

每题包含 ISO、Front、Top、Right 的三套独立图片：并排、普通透明叠加、差异着色。静态资源键固定为 `previews[angle].split / overlay / difference`；差异不可用时明确显示缺失，不拿普通叠加冒充差异。点击图片后支持滚轮与按钮缩放、拖动平移、适应窗口、原始像素 1:1、左右切换和 Esc 关闭。本机服务提供 CADViewer / Three.js 三维查看，可同步旋转、缩放、平移、切换标准方向，以及分别查看两侧模型或差异区域。静态与三维使用一致的 Z-up 方向约定，Top 的屏幕上方是 +Y。

每次进入题目或点“重置对比”，恢复该题默认坐标、并排、等轴、适配视野、候选透明度 0.65、标准答案透明度 0.35，并关闭资料折叠区、恢复资料第一页。geometry 默认对齐坐标；只有缺少对齐模型时才退回原始坐标。切换坐标保留当前模式、方向和透明度，初始化回调不得覆盖这些状态。页面重载不读取上次临时交互状态。

四视图通过并排、透明叠加、差异标签切换，每次只显示一组四张大图。放大窗口也能在同一角度切换这三种显示，并保留放大倍数和拖动位置。三维差异模式提供蓝色区域、多余、缺失三个独立开关，便于单独检查错误区域。

提供经过原始身份校验的生成 STEP、标准答案 STEP 和已有对齐副本下载；批次 ZIP 只含可用的原始生成 STEP，不含 GT 或标注，也不是历史官方提交 ZIP 的复刻。未通过文件身份校验的文件不提供下载。

## 生成报告

v4 评测结束后，独立评测器自动在结果目录的 `html-report/index.html` 生成报告。插件在维护端 `private_runs_root/<run>/html-report/index.html` 生成完整流水线报告，保留未提交、导出失败等尚无评分的题目。单题调用使用同一格式。管理 MCP 的结果在文件存在时返回 `files.html` 和 `files.html_report_status`。

也可以从已有最终 v4 结果离线生成，不重跑评分：

```powershell
python -B render_report.py --input C:/benchmark/finished-v4 --output C:/benchmark/reports/new-report
python -B render_report.py --input C:/benchmark/finished-v4 --output C:/benchmark/reports/with-analysis --analysis C:/benchmark/analysis/ablation.json
python -B render_report.py --input C:/benchmark/finished-v4 --output C:/benchmark/reports/scores-only --no-mesh
```

`--input` 接受评测结果目录或其中的 `summary.json`；`--output` 必须是新目录。`--analysis` 读取已有消融结果，并核对其来源和评分口径。`--no-mesh` 仅生成分数与分析页面，只需 Python 标准库；生成模型预览需要评测环境中的 OCP 和 VTK。`--mesh-timeout` 设置每个显示任务的超时秒数，默认 60。

插件可使用同一脚本，或用 `python -B fusion-benchmark-bootstrap.py report`（便携包为 `fusion-benchmark.cmd report`）传入相同参数。重建完整流水线报告时额外指定 `--pipeline`：

```powershell
python -B render_report.py --input C:/benchmark/private/runs/run-001/evaluation --pipeline C:/benchmark/runs/run-001/pipeline-status.json --output C:/benchmark/private/reports/run-001
```

若流水线尚无评测结果，可省略 `--input`，仅提供 `--pipeline`。含 GT 的报告保留在维护端私有目录，不放入候选工作区或 Worker Gateway 的公开输入目录。

## 查看和迁移

直接打开报告目录中的 `index.html` 可离线查看静态对比图、分数和分析。三维交互通过报告自带的本机服务打开：

```powershell
python -B C:/benchmark/reports/new-report/serve_report.py --root C:/benchmark/reports/new-report --open
```

也可以从源码目录运行 `python -B serve_report.py --root <报告目录> --open`。服务仅监听 `127.0.0.1`，默认自动选择端口，按 Ctrl+C 停止；可用 `--port 8765` 指定端口。Windows 且 PATH 中已有 Python 时，可双击报告里的 `打开三维报告.cmd`。便携包可用包内 `runtime/python/python.exe` 替代命令中的 `python`。

迁移或分享时复制整个报告目录。查看报告只需浏览器；启动三维服务另需 Python 标准库，不需 CAD 依赖或网络下载。脚本、Three.js 和显示资源均随报告提供，不使用 CDN。

## 分数与对比含义

报告只读取已完成的评分证据，不修改输入、评分算法或权重。v4 主分为 shape、dimension、mass，权重分别为 6/13、5/13、2/13；shape 内的 IoU、Surface F1、Topology 等权。诊断指标不计入总分。测量缺失保留为空，计分项按既定政策计零并展示总分下界与可能范围；验证超时与候选无效分别呈现。

`geometry` 报告可比较原始坐标与评分所用的刚体对齐坐标；`strict` 按原始坐标评分。浏览器不估计对齐、不分别归一化两个模型，也不重新计算分数。显示网格是用于观察的近似，不替代 BRep 或评分证据。预览生成失败时分数仍保留，HTML 生成失败不会改变已完成的评分结果。

普通透明叠加独立显示候选橙色与标准蓝色，用于观察轮廓重合。它不使用红黄差异层，也不表示误差大小。

全模型的默认差异着色显示**超过显示容差的表面区域**：蓝色为重合或显示容差内的区域，红色为候选超出的表面，琥珀色为候选缺少的表面。容差取双方显示网格的最大线性挠度，并在页面明确标明；低于该阈值的独立三角化细差不涂满整个表面。该容差不参与评分，也不构成工程验收公差。

“精确体积差异”入口保留未经显示容差过滤的原始布尔层：蓝色为相交体积，红色为多余材料，琥珀色为缺失材料。极薄布尔片层也会完整着色，因此颜色面积不等于缺陷体积。两种差异均采用不透明表面和深度遮挡，不再叠加完整模型的半透明背景。非闭合或不完整网格不能产生可信差异时，只保留普通透明叠加并标明状态；不能用预览可用替代评分网格有效性。

默认容差表面只显示双方实体并集的外边界，防止布尔分区共享的内部面相互遮挡产生条纹；不能用它排除封闭内腔差异。检查内部差异时切换精确体积模式并关闭蓝色区域，单独观察红色和琥珀色分区。精确模式保留全部原始布尔材料证据。

官方 **Interface overlay** 使用真实 mating jig 的 KOR（应留空）/KIR（应填满）区域。提供以下维护端私有清单后，可通过 `--interfaces` 生成该局部区域图；没有 jig 时不会把全模型差异称为 Interface Match，也不会虚构对应分数。

```powershell
python -B render_report.py --input C:/benchmark/finished-v4 --interfaces C:/benchmark/private/interfaces.json --output C:/benchmark/reports/with-interfaces
```

```json
{
  "schema_version": "fusion-report-interfaces/1",
  "cases": [{
    "case": "101",
    "gt_sha256": "<与本次评测GT一致的64位SHA256>",
    "regions": [{"path": "101/jig_1__1__KOR.step", "sha256": "<区域STEP的64位SHA256>", "fit_type": "KOR"}]
  }]
}
```

区域路径相对于清单目录，须保留在该目录内且不能经过链接。清单不能和公开输入混用。显示区域采用官方的验证壳层定义：区域 AABB 外扩 `max(2 mm, 20% × 最长边)`，KOR 的相邻实体应填满，KIR 的相邻空间应留空。仅用已有坐标，不进行新的位姿搜索。三维图和四角度图都保持这个范围；网格近似不构成官方 Interface 分数复现。

单题 `#fixture=<题号>` 链接可直接打开案例；上一题/下一题支持 `j/k` 和左右方向键，Esc 返回列表。放大图片时这些按键只作用于图片。

页面中的分数排序用于浏览题目，不构成模型榜单；`ranking_eligible` 保持 `false`。评分口径详见 [v4 说明](SCORING_V4.md)，消融口径见 [分析说明](ABLATION.md)。CADViewer 与 Three.js 的 MIT 许可随 `report_assets/licenses/` 分发，来源记录见 [VIEWER_SOURCES.json](../report_assets/VIEWER_SOURCES.json)。

## 不同类型的输入资料

输入资料与评分结果分别接入。报告接受可选 `--inputs <资料清单.json>`，不要求固定文件名或 DXF 扩展名；资料显示在单题页“输入资料”折叠区，不占用左右模型对比位。

- PNG、JPG/JPEG、WebP 直接显示原图，可打开大图。
- PDF 提供原文入口；如清单带有分页预览图，则按给定顺序翻页。报告不额外解析或转换 PDF。
- DXF 提供下载入口，可附渲染预览图。
- 其他格式保留为普通附件下载，不在网页执行或解析。
- 多份图片、多页文档及混合格式按清单顺序展示。缺失/损坏资料单独标记，已有分数保留。

```powershell
python -B render_report.py --input C:/benchmark/finished-v4 --inputs C:/benchmark/public/report-inputs.json --output C:/benchmark/reports/with-inputs
```

清单示例（SHA256 替换为对应文件的真实摘要）：

```json
{
  "schema_version": "fusion-report-inputs/1",
  "cases": [
    {
      "case": "101",
      "task_type": "generation",
      "prompt": "根据图纸重建零件。",
      "assets": [
        {
          "label": "零件图",
          "path": "drawings/part.pdf",
          "sha256": "<文件的64位SHA256>",
          "previews": [
            {"label": "第1页", "path": "previews/page-1.png", "sha256": "<预览图的64位SHA256>"},
            {"label": "第2页", "path": "previews/page-2.png", "sha256": "<预览图的64位SHA256>"}
          ]
        },
        {"label": "补充视图", "path": "drawings/detail.jpg", "sha256": "<文件的64位SHA256>"}
      ]
    }
  ]
}
```

只将明确公开给建模方的输入列入清单。路径相对于清单所在文件夹，须留在该文件夹内；拒绝绝对路径、父目录跳转及链接目录。报告复制已验证的资料到自己的 `inputs/`，移动报告后仍可读取。清单只描述输入，不能代替评测证据，也不能引入或改写分数。

Python 接口 `build_report(..., inputs=manifest_path)` 与插件 `build_private_report(..., inputs=manifest_path)` 使用同一契约。后续建模输入扩展只需把公开输入映射到此清单；本次兼容性覆盖报告展示，当前建模流水线的 PDF/多图读取能力另行扩展。

可选 `task_type` 为 `generation`、`editing` 或 `unknown`，`prompt` 为纯文本。没有标注时保持未标注，不从题号推断类型；分类均分只统计该类型中参与 v4 评分的案例，包含缺测下界。编辑类图片或 WebP 可用 `purpose` 指定 `original`、`edit_target`、`edit_output`，按资料列表展示；已有结果未提供的编辑步骤/动画不会自动补造。

Python 接口另支持 `interfaces=private_manifest_path`。插件维护端配置可通过 `report_inputs` 和 `report_interfaces` 指向相应清单；它们只交给收尾报告生成器，不进入候选工具的输入集合。便携报告仍不需要联网或 CDN。
