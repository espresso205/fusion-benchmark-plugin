# 历史 OpenCode 流程

本文件仅用于旧冻结评测器 ZIP 叠加包的兼容说明。v3.0.0 推荐使用[统一入口](PORTABLE_PLUGIN_USAGE.md)，它支持 OpenCode、Codex 和 Claude Code。

旧 `python -m fusion_pipeline run` 路线需要 `evaluator_zip`、`opencode`、`python`、公开输入、私有 cases 和建模 skill 的接收方绝对路径，并受 `runner.MODEL` 的固定模型约束。`scripts/package_fusion_pipeline.py` 校验冻结 ZIP 并叠加白名单，不修改原档案字节。

旧 guard、预加载检查、单题时间与工具预算、未知 Fusion 状态保留锁规则仍由源码执行。不能把旧 ZIP 的算法身份称为新版 v3。具体主链路与安全边界见[架构](ARCHITECTURE.md)。

旧 guard 的只读 skill 白名单要求路径以 `skills/autodeskfusion/SKILL.md` 结尾。新版 `unified_gateway` 直接读取配置中明确指定的 `fusion_skill` 文件，可使用仓库内 `agent/fusion-modeling/SKILL.md`；两种路由的合同不能混用。
