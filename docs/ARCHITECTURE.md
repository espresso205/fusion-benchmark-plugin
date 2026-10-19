# 架构

```mermaid
flowchart LR
    A[受信任协调器 / MCP 管理] --> B[每题独立 CLI 会话]
    B --> C[单题 Worker Gateway]
    C --> D[已有 Fusion MCP 桥接]
    D --> E[Fusion 原生建模与 F3D 提交]
    E --> F[冻结提交字节与依赖证据]
    F --> G[受信任 Fusion 重开 / STEP 导出]
    G --> H[v3 评测快照]
    H --> I[固定权重报告与覆盖率]
    J[私有 GT / 冻结标注] --> H
```

## 管理与执行

`fusion_pipeline/service.py` 提供 stdio MCP 的 `capabilities`、`preflight`、`start_run`、`status`、`cancel`、`results`。连接配置由 `discovery.py` 安全合并，宿主技能由接收方按宿主机制安装。MCP 发现不会自动创建宿主原生子代理；`adapters.py` 负责以已配置模型启动新的 OpenCode、Codex 或 Claude Code CLI 进程。

`runner.py` 创建新 run、记录源码与输入身份、准备逐题工作区，并串行占用 Fusion 桌面。候选只接收本题公开 TASK/DXF、通用建模说明和提交路径。`worker_gateway.py` 公开合同读取、输入读取、DXF 解析、Fusion schema 查询/调用、报告与提交工具；它记录哈希链日志及 `mcp_tools_scope` 调用预算，提交后撤销后续工具调用。

## 冻结与可信导出

候选提交 `submission.f3d`。调度器冻结字节、检查公开输入身份和依赖证据；维护端使用 `fusion_scripts.py` 在 Fusion 中独立重开归档，检查设计和实体健康状况，再导出 STEP。候选自行写出的 STEP 只属于候选材料，不会凭文件名获得可信导出身份。

Fusion MCP 请求超时、取消或执行状态未知时，保留锁及请求证据，停止继续建模；结束 CLI 进程不证明 Fusion 后台操作停止。恢复需要操作员确认当前桌面与请求状态，不能盲目重发同一尝试。

## v3 评分

`evaluate.py` 校验输入及哈希，保存评测快照；`worker.py` 调用 `src/metrics.py` 与 `src/robust_metrics.py`。validation 通过后按六项固定权重聚合，测量失败保留 `measured_score=null`，以 `awarded_score=0` 计入政策下界并给出覆盖率和可能得分范围。具体含义见[评分合同](SCORING_V3.md)。

本仓库包含与独立[评测器仓库](https://github.com/espresso205/fusion-step-evaluator)相应的核心，用于单包部署；流水线不要求在运行时从 GitHub 下载源码。评分核心发布身份见[文件清单](../SCORING_SOURCE_MANIFEST.json)。

## 权限与产物

管理 MCP 拥有私有配置和评分访问权，不能交给候选会话。Worker Gateway 的输入边界和严格工具选项不是操作系统沙箱；Fusion 中的任意脚本仍以桌面用户身份运行。需要强 GT 隔离时，组织者须采用不同身份或虚拟机与受限桥接服务。

直接 CLI `run` 返回运行目录，进度和结果位于该目录及维护端私有输出目录。MCP `start_run` 另返回服务 job ID，`status/results/cancel` 使用此 ID。始终同时检查逐题终态、可信导出证据、评分覆盖和失败原因；`rank_eligible` / `ranking_eligible` 保持 false。
