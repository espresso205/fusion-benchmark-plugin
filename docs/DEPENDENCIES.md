# 依赖与来源

源码运行需要 Windows x64、CPython 3.12，以及[锁定依赖](../requirements-portable-plugin.lock.txt)。核心依赖包括 OCP/OCCT、NumPy、SciPy、Trimesh、Open3D、Manifold、NetworkX、build123d 和 DXF 解析用 ezdxf；完整传递闭包以锁文件和实际构建清单为准。

Autodesk Fusion、有效登录与许可、可交互桌面、已经安装的 Fusion MCP 桥接，以及 OpenCode/Codex/Claude Code CLI 和模型账号由接收方提供。桥接必须支持运行中实际发现的 Fusion 工具 schema，本仓库不安装或替代它。`doctor` 检查本地 CLI 能力，不验证模型授权或 Fusion 连通；`run --preflight-only` 会真实调用模型和只读 Fusion 工具。

`vendor/cadgenbench` 是选取的 [Hugging Face CADGenBench](https://github.com/huggingface/cadgenbench) 源码，保留[原许可证](../vendor/cadgenbench_LICENSE)与[项目元数据](../vendor/cadgenbench_pyproject.toml)。它与 `anniedoris/CADBench` 是不同项目。当前评分路径不调用 DeepCAD、CADBench、JoinABLe 或 MechVQA。

第三方许可位于 [licenses](../licenses/PORTABLE_RUNTIME_NOTICES.md)，OCCT 许可文本及其额外例外独立保留。完整便携构建还复制依赖原始许可/NOTICE 并生成 `licenses/inventory.json`；源码仓库不包含运行时的动态依赖许可清单。上游许可仍适用于对应文件，仓库没有替第三方重新授予许可。

通用[建模技能](../agent/fusion-modeling/SKILL.md)从已使用的 Fusion MCP 操作流程整理，仅保留公开工具约定、原生 API 执行及提交注意事项，不包含账号、题目、模型答案或机器专用地址。
