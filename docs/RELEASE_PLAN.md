# v3.0.0 源码整理计划

范围：在独立发布仓库收集已验收主链路，保持评分源码字节不变；不修改原冻结包，不进行 Git 或 GitHub 操作。

- [x] 按便携白名单收集 pipeline、evaluator、vendor、scheduler、tests、agent 与许可。
- [x] 补齐相对链接文档、通用建模说明、配置示例与本地配置生成器。
- [x] 检查配置、MCP 发现、doctor、必要源代码测试与评分源码哈希。
- [x] 交付文件清单和依赖限制，便携 ZIP 由发布协调者后续构建。

源码包不收录 runtime、runs、deliverables、历史 baseline、私有 manifests、凭据或本机配置。源码测试使用合成输入；历史资产需显式启用。
