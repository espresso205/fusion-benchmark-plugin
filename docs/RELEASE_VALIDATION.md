# v3.0.0 源码验证

本记录对应发布源码整理，不宣称新机器已完成真实 Fusion 建模或所有 CLI 的端到端运行。

|检查|结果|
|---|---|
|原有源码测试：`python -B -m unittest discover -s tests -v`|配置生成器专项修复前运行 220 项；217 通过、3 项可选历史资产测试跳过；0 失败|
|配置生成器隔离回归：`python -B -m unittest tests.test_create_config -v`|新增 3 项：保持默认严格模式、Codex 默认拒绝且不写文件、显式允许原生工具后生成宽松配置；修复前复现 2 个失败，修复后通过|
|`node --test tests/fusion_guard.test.mjs`|21 项通过|
|MCP stdio `initialize` / `tools/list` / `capabilities`|从仓库外工作目录启动成功；服务版本 3.0.0；6 个管理工具|
|`doctor`|成功输出本地 CLI 能力；模型与 Fusion 连通性明确为 not_tested|
|OpenCode / Codex / Claude `connect --dry-run`|三种配置格式均生成成功，未写入宿主配置|
|本地配置生成器|使用当前解释器、仓库评测核心和建模 skill；已有文件拒绝覆盖|
|评分源码 SHA256|`SCORING_SOURCE_MANIFEST.json` 内 33 文件均保持原字节|
|相对文档链接|README、AGENTS、docs、agent 中本地 Markdown 链接均有目标|

源码保留原评分和流水线测试。历史资产路径改为显式 `FUSION_BENCHMARK_REGRESSION_ROOT`，不自动读取维护机文件。公开示例使用虚构占位路径，真实本机配置、凭据、数据、历史 run 和运行时均未收录。

本仓库的便携构建白名单已增加发布文档、VERSION、通用建模 skill、配置示例与生成器，并取消生成简版 README 覆盖现有 README 的行为。完整便携运行时与 ZIP 由发布协调者单独组装及验证；本次源码验证没有启动模型或 Fusion。
