# 构建与检查

版本为 v3.0.0。完整便携包面向 Windows x64 / CPython 3.12；源码仓库本身不带运行时。

在专用构建环境安装[锁定依赖](../requirements-portable-plugin.lock.txt)，从仓库根执行：

```powershell
python -B scripts/build_portable_plugin.py --python (Get-Command python).Source --output ../build-output/fusion-benchmark-plugin-v3.0.0 --zip
```

输出必须是尚不存在的新目录。构建器核对依赖版本及安装 RECORD 哈希，按明确白名单收集源码、标准库和依赖文件，不复制整个虚拟环境。它包含发布文档、通用建模技能、示例配置和版本文件。评分算法文件不在构建中改写。

构建失败保留 `INCOMPLETE`；完整性检查不会接受此标记。成功后输出 `PACKAGE_MANIFEST.json`、`SHA256SUMS`、`runtime.lock.json`、依赖许可清单以及可选 ZIP 与独立 SHA256。

```powershell
python -B scripts/build_portable_plugin.py --verify ../build-output/fusion-benchmark-plugin-v3.0.0
```

发布前把整个便携目录搬到另一条含中文和空格的路径，从包外运行 `fusion-benchmark.cmd doctor`、MCP `initialize/tools/list` 与合成几何评测，并校验完整文件集。干净机器验收和真实 Fusion/模型运行需要独立记录。

`scripts/package_fusion_pipeline.py` 保留旧冻结 ZIP 叠加打包能力，只用于历史 v2 分发复现。它不是 v3.0.0 的推荐构建入口，也不会自动生成新版便携运行时。

源码测试命令见[README](../README.md)。三项可选历史回归测试从 `FUSION_BENCHMARK_REGRESSION_ROOT/Qxx/cases.json` 读取组织者提供的资产，默认跳过；仓库不包含这些数据。`SCORING_SOURCE_MANIFEST.json` 覆盖评分核心及 vendored 源码，不能替代便携包的全文件清单。
