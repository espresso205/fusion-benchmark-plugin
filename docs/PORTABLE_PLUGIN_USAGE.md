# 安装、连接与运行

## 选择入口

源码检出：在自己的 Windows x64 / CPython 3.12 环境安装[锁定依赖](../requirements-portable-plugin.lock.txt)，从仓库根运行 `python -B fusion-benchmark-bootstrap.py`。完整便携发布包：保留全部目录结构，使用 `fusion-benchmark.cmd`，不需要接收方再安装 Python 或 pip 依赖。纯源码目录不含 `runtime/`，所以不能直接使用 CMD 启动器。

执行建模评测的接收方仍需已经安装并登录授权的 Autodesk Fusion、交互桌面、可用 Fusion MCP 桥接，以及自己的 OpenCode/Codex/Claude Code CLI 和模型权限。不要复制维护者的 CLI 配置、登录目录或凭据。已有结果的 `ablation` 分析只需 Python 标准库或完整便携包，使用方法见下文。

```powershell
python -B fusion-benchmark-bootstrap.py doctor
python -B fusion-benchmark-bootstrap.py --help
```

`doctor` 是本地配置/CLI 检查，不调用模型或 Fusion；其 JSON 中的 `model_and_fusion_connectivity=not_tested` 必须如实保留。配置中的 `cli_executable` 是实际运行选用路径；doctor 的自动探测还依赖 PATH 或 provider 专用配置字段。真实候选启动时会另行探测配置路径。

## 本地配置

[示例 JSON](../examples/fusion_pipeline_config.json)中的路径、模型、端口都是占位值，不代表已发现的安装。推荐使用[配置生成器](../scripts/create_config.py)，它会将路径转换为绝对路径，并使用当前 Python、仓库评测核心与通用建模技能。

从仓库根运行以下模板，替换 provider/model 与经过确认的本地 Fusion 端口；各目录由接收方选择：

```powershell
python -B scripts/create_config.py `
  --output ../local-settings/pipeline.local.json `
  --provider opencode --model 'provider/model' `
  --cli-executable (Get-Command opencode).Source `
  --questions-root ../fusion-benchmark-dataset/public `
  --private-cases ../local-settings/cases.json `
  --runs-root ../benchmark-output/runs `
  --private-runs-root ../benchmark-output/private-runs `
  --fusion-url 'http://127.0.0.1:8000/mcp' `
  --case-ids Q01
```

端口 `8000` 仅为模板，必须替换成已有 Fusion 桥接的实际地址。生成器不验证连通性、不修改身份认证、不覆盖已有配置。完整便携包用 `runtime/python/python.exe` 执行同一生成器，从而自动选中包内 Python。需要 Claude 时同时替换 provider、可执行文件和模型。

默认生成 `strict_tool_isolation=true`。当前 Codex 适配器不能禁用全部原生工具，因此 `--provider codex` 默认报错且不写配置。如果组织者明确接受在受信任同账号环境中让原生工具仍可用，可在上述生成命令中改用 Codex、对应可执行文件/模型，并显式添加 `--allow-native-tools`；该参数生成 `strict_tool_isolation=false`。要求严格工具隔离时使用支持它的其他适配器。此选择不建立 OS 沙箱，模型或 Fusion 脚本仍受同账号文件权限约束；不得将宽松配置视为私有 GT 隔离保证。

公开输入按 `questions_root/Qxx/inputs.json` 显式列出 TASK 与有序 DXF/PNG/JPEG；旧 `TASK.md + drawing.dxf` 目录继续支持。图片与多附件要求 `execution_backend=unified_gateway`，合同、限额及生成示例见 [输入格式](INPUT_FORMATS.md)。维护端私有 `cases.json` 的 `cases` 数组必须包含 `case`、`gt`、`annotations` 和 `expected_hashes.gt/annotations`；路径必须绝对化，标注的 `gt_sha256` 需匹配对应 GT。[数据仓库](https://github.com/espresso205/fusion-benchmark-dataset)提供 `python scripts/prepare_config.py --output local/private_cases.json`，可用 `--cases Q01 Q02` 选题；把其生成文件路径传给本生成器的 `--private-cases`。私有配置和输出放在仓库/便携包外。

选定图片模型后显式执行 `python -B fusion-benchmark-bootstrap.py visual-check --config <pipeline.json> --output <新目录>`，验证两张图片经正式 Gateway 传输及模型识别。该命令使用真实模型额度，但不调用 Fusion；`doctor`/管理 `preflight` 不会做此验证。保存本次 CLI 版本、请求模型及实际返回证据，不能推广成所有模型均支持图片。

## 连接管理 MCP

```powershell
python -B fusion-benchmark-bootstrap.py connect --cli opencode --target ../workspace/opencode.json --config ../local-settings/pipeline.local.json --dry-run
python -B fusion-benchmark-bootstrap.py connect --cli codex --target ../workspace/.codex/config.toml --config ../local-settings/pipeline.local.json --dry-run
python -B fusion-benchmark-bootstrap.py connect --cli claude --target ../workspace/.mcp.json --config ../local-settings/pipeline.local.json --dry-run
```

检查计划后，对需要的 CLI 省略 `--dry-run` 写入。连接器安全合并支持的 MCP 字段，冲突会拒绝。OpenCode 目标是严格 JSON，不是 JSONC。配置记录当前解释器和 bootstrap 的绝对路径；移动仓库或便携包后重新生成。宿主技能需要按相应机制安装/引用，`connect` 不安装技能；见[维护端技能](../agent/fusion-benchmark/SKILL.md)和[宿主模板](../agent/templates/opencode.md)。

MCP 提供 `capabilities/preflight/start_run/status/cancel/results`。管理连接只能给受信任协调器，不能给候选。`start_run` 返回服务 job ID；后续状态、取消和结果查询使用此 ID。工具发现成功不等于模型/桥接已通过实跑验证。

## 直接运行

```powershell
python -B fusion-benchmark-bootstrap.py run --config ../local-settings/pipeline.local.json --preflight-only
python -B fusion-benchmark-bootstrap.py run --config ../local-settings/pipeline.local.json
```

第一条会真实调用模型和只读 Fusion 工具，会消耗模型请求；它与只读 `doctor` 不同。直接 CLI `run` 返回运行目录，读取该目录的 `pipeline-status.json` 与维护端报告。直接运行没有管理服务 job ID；需要统一 `status/results/cancel` 生命周期时通过 MCP `start_run` 创建任务。

每题为新会话和新尝试。默认合同采用 200 次 Gateway 工具调用和 1800 秒预算，具体以 `get_contract` 返回值为准。`strict_tool_isolation` 会拒绝无法禁用原生工具的适配器；它不是 OS 沙箱。单账号 Fusion 脚本仍可能访问该账号文件权限范围，强隔离需额外身份/虚拟机与受限工具代理。

候选交付 F3D 后，维护端冻结、独立重开并导出 STEP，再执行默认[v4 几何符合评分](SCORING_V4.md)，坐标偏差单列。未知 Fusion 状态保留锁和请求证据，不自动重试或改写旧 run。成绩应同时报告验证状态、参与题数、三项主分的测量覆盖、缺失项及诊断状态，不能把未测得当成实测零分。

## 已有结果的离线消融分析

`ablation` 读取已完成评测的结果，调用与独立评测器相同的公共分析 CLI。无需 `--config`、模型账号或 Fusion；分析不读取 STEP，也不重跑几何测量。源码入口：

```powershell
python -B fusion-benchmark-bootstrap.py ablation --help
python -B fusion-benchmark-bootstrap.py ablation --input C:/benchmark/private/runs/finished-v3 --output C:/benchmark/analysis/new-ablation
```

完整便携包入口：

```powershell
.\fusion-benchmark.cmd ablation --input C:/benchmark/private/runs/finished-v3 --output C:/benchmark/analysis/new-ablation
```

`--input` 可接收一个或多个结果目录、`summary.json` 或受支持的历史 `final_scores.json`。需要指定来源 ID 或系统名时使用 `--manifest`；它与 `--input` 互斥。`--output` 必须是尚不存在的新目录，且位于输入目录之外；便携包用户还应放在包目录外。

结果包括 `ablation.json`、`ablation.csv` 和 `ablation.md`，分别呈现正式计分项完整实测的主分析与政策计分分析，保留排除原因和测量覆盖率。v4 完整样本只要求 shape/dimension/mass 三项，v3/历史协议仍要求六项；v4 诊断缺测另列，geometry 与 strict 不合并作同口径系统比较。分析中的排序不赋予正式排名资格。输入合同、系统比较条件和报告解释见[消融分析说明](ABLATION.md)。

## 完整性与许可

完整便携包的 `PACKAGE_MANIFEST.json`、`SHA256SUMS` 和 `runtime.lock.json` 绑定实际文件及依赖版本；验证命令见[构建说明](BUILD.md)。向包内写运行结果或缓存会使精确文件集验证失败。依赖许可证及外部安装要求见[依赖说明](DEPENDENCIES.md)。
