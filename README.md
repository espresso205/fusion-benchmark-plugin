# Fusion Benchmark Plugin

版本 **v4.0.0**。为 OpenCode、Codex、Claude Code 提供统一 MCP 管理入口：每题启动独立 CLI 会话，通过现有 Fusion MCP 建模，冻结 F3D，再由维护端独立重开、导出可信 STEP 并执行 v4 几何符合评分并单列坐标诊断。

本仓库包含完整流水线、评测核心、测试、技能说明和便携包构建脚本。源码仓库不包含 Python 运行时、Fusion、CLI 可执行文件、模型账号或题目数据。

- [架构与边界](docs/ARCHITECTURE.md)
- [安装、连接与运行](docs/PORTABLE_PLUGIN_USAGE.md)
- [v4 评分合同](docs/SCORING_V4.md)；[历史 v3](docs/SCORING_V3.md)
- [已有结果的指标消融分析](docs/ABLATION.md)
- [HTML 结果报告与三维对比](docs/HTML_REPORT.md)
- [PNG/JPEG、多图与混合建模输入](docs/INPUT_FORMATS.md)：显式清单、冻结哈希和原生 MCP 图片；旧 TASK/DXF 目录继续支持。
- [维护端技能](agent/fusion-benchmark/SKILL.md) · [候选建模技能](agent/fusion-modeling/SKILL.md)
- [配置示例](examples/fusion_pipeline_config.json) · [依赖与许可](docs/DEPENDENCIES.md)
- 独立评测器：[espresso205/fusion-step-evaluator](https://github.com/espresso205/fusion-step-evaluator)
- 数据集：[espresso205/fusion-benchmark-dataset](https://github.com/espresso205/fusion-benchmark-dataset)

## 源码运行

在 Windows x64 的 CPython 3.12 环境，从仓库根目录运行：

```powershell
python -m pip install -r requirements-portable-plugin.lock.txt
python -B fusion-benchmark-bootstrap.py doctor
python -B scripts/create_config.py --help
python -B fusion-benchmark-bootstrap.py connect --help
```

配置生成器把接收方路径转为绝对路径，并自动使用本仓库中的评测器与建模技能。它不安装 Fusion 桥接，不发现端口，也不读取账号。实际运行前，按[使用说明](docs/PORTABLE_PLUGIN_USAGE.md)配置自己的 Fusion MCP、CLI、模型和数据。

默认保持严格工具隔离。当前 Codex 适配器不能关闭全部原生工具，配置生成器会拒绝其默认配置；仅在受信任同账号环境主动接受原生工具仍可用时，显式添加 `--allow-native-tools`。该选项设定 `strict_tool_isolation=false`，不提供操作系统沙箱。

完整便携发布包使用 `fusion-benchmark.cmd`，自带 Python 和锁定依赖；该启动器在纯源码检出中会明确报告运行时缺失。完整包可从仓库构建，见[构建说明](docs/BUILD.md)。

已有结果可离线做指标消融分析，只需 Python 标准库，无需流水线配置或 Fusion：

```powershell
python -B fusion-benchmark-bootstrap.py ablation --help
python -B fusion-benchmark-bootstrap.py ablation --input C:/benchmark/private/runs/finished-v3 --output C:/benchmark/analysis/new-ablation
```

完整便携包用 `fusion-benchmark.cmd ablation` 传入相同参数。分析输出到新目录，保留正式评分和测量缺失语义；分析排名不赋予正式排名资格。多来源、系统比较和输出字段见[分析说明](docs/ABLATION.md)。

v4 流水线自动在维护端 `private_runs_root/<run>/html-report/index.html` 生成 HTML，保留全部题目，并以左侧生成模型、右侧标准答案展示差异、分数和评测分析。已有评分可用 `python -B render_report.py --input <最终结果目录> --output <新报告目录>` 重建；`--analysis <ablation.json>` 加入已有分析，`--no-mesh` 仅生成分数与分析页面。也可通过 `fusion-benchmark.cmd report` 使用相同参数。直接打开 HTML 可离线阅读，`python -B serve_report.py --root <报告目录> --open` 启动本机三维对比。完整流水线重建、单题报告和私有目录约定见 [HTML 说明](docs/HTML_REPORT.md)。

最新示例：[下载 CADGenBench 18 题四视角 HTML 报告](https://github.com/espresso205/fusion-benchmark-plugin/releases/download/html-report-20261027/cadgenbench-v4-multiview-report-20261027.zip)，包含 ISO/Front/Top/Right 对比、可旋转模型、材料差异、STEP 下载与原有分数分析。解压后打开 `index.html`；[本次更新与使用说明](docs/HTML_REPORT_RELEASE_20261027.md)。[上一版重评说明](docs/HTML_REPORT_RELEASE_20261026.md)保留。

## 源码测试

```powershell
python -B -m unittest discover -s tests -v
node --test tests/fusion_guard.test.mjs
```

第二条只验证旧 OpenCode guard，Node 不属于 Python 评分运行依赖。三个可选历史回归测试需要另行提供资产，默认跳过。依赖检查、MCP 发现与合成几何测试不代表真实模型/Fusion 执行已经完成。

`SCORING_SOURCE_MANIFEST.json` 记录发布时评分核心文件的 SHA256。v4 主分只含 shape/dimension/mass 的固定权重，诊断项不计分，缺失测量以明确的政策下界计分，排名资格保持关闭。新 CLI 会话隔离聊天上下文；同一操作系统账号并不隔离私有 GT 的文件权限。
