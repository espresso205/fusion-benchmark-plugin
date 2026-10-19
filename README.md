# Fusion Benchmark Plugin

版本 **v3.0.0**。为 OpenCode、Codex、Claude Code 提供统一 MCP 管理入口：每题启动独立 CLI 会话，通过现有 Fusion MCP 建模，冻结 F3D，再由维护端独立重开、导出可信 STEP 并执行 v3 评分。

本仓库包含完整流水线、评测核心、测试、技能说明和便携包构建脚本。源码仓库不包含 Python 运行时、Fusion、CLI 可执行文件、模型账号或题目数据。

- [架构与边界](docs/ARCHITECTURE.md)
- [安装、连接与运行](docs/PORTABLE_PLUGIN_USAGE.md)
- [v3 评分合同](docs/SCORING_V3.md)
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

## 源码测试

```powershell
python -B -m unittest discover -s tests -v
node --test tests/fusion_guard.test.mjs
```

第二条只验证旧 OpenCode guard，Node 不属于 Python 评分运行依赖。三个可选历史回归测试需要另行提供资产，默认跳过。依赖检查、MCP 发现与合成几何测试不代表真实模型/Fusion 执行已经完成。

`SCORING_SOURCE_MANIFEST.json` 记录发布时评分核心文件的 SHA256。v3 总分采用固定权重，缺失测量以明确的政策下界计分，排名资格保持关闭。新 CLI 会话隔离聊天上下文；同一操作系统账号并不隔离私有 GT 的文件权限。
