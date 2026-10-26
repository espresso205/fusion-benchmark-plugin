# HTML 评测报告

报告按题展示生成模型与标准答案的差异、指标分数和评测分析。对比图及三维分屏均为左侧生成模型、右侧标准答案（GT），双方共用视角和尺度。首页提供批次概况、搜索和筛选，点击题目可查看详细对比。

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
