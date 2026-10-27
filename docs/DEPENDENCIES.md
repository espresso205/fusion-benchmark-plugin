# 依赖与来源

源码运行需要 Windows x64、CPython 3.12，以及[锁定依赖](../requirements-portable-plugin.lock.txt)。核心依赖包括 OCP/OCCT、NumPy、SciPy、Trimesh、Open3D、Manifold、NetworkX、build123d 和 DXF 解析用 ezdxf；完整传递闭包以锁文件和实际构建清单为准。

建模图片使用 Pillow 12.2.0 校验 PNG/JPEG、解码和规范化 EXIF 方向；现作为流水线直接依赖声明，沿用已有 portable lock 的版本。便携构建按 RECORD 收集 Pillow 及其许可，不依赖本机额外图像程序；不会自动缩放、裁切或解释工程尺寸。

Autodesk Fusion、有效登录与许可、可交互桌面、已经安装的 Fusion MCP 桥接，以及 OpenCode/Codex/Claude Code CLI 和模型账号由接收方提供。桥接必须支持运行中实际发现的 Fusion 工具 schema，本仓库不安装或替代它。`doctor` 检查本地 CLI 能力，不验证模型授权或 Fusion 连通；`run --preflight-only` 会真实调用模型和只读 Fusion 工具。

`vendor/cadgenbench` 是选取的 [Hugging Face CADGenBench](https://github.com/huggingface/cadgenbench) 源码，保留[原许可证](../vendor/cadgenbench_LICENSE)与[项目元数据](../vendor/cadgenbench_pyproject.toml)。它与 `anniedoris/CADBench` 是不同项目。当前评分路径不调用 DeepCAD、CADBench、JoinABLe 或 MechVQA。

第三方许可位于 [licenses](../licenses/PORTABLE_RUNTIME_NOTICES.md)，OCCT 许可文本及其额外例外独立保留。完整便携构建还复制依赖原始许可/NOTICE 并生成 `licenses/inventory.json`；源码仓库不包含运行时的动态依赖许可清单。上游许可仍适用于对应文件，仓库没有替第三方重新授予许可。

HTML 显示部分适配 CADViewer，并随包提供 Three.js 0.160.0，不通过 CDN 加载。两者均为 MIT 许可，文本分别位于 `report_assets/licenses/CADViewer-MIT.txt` 和 `report_assets/licenses/Three-MIT.txt`；来源与改动记录在 [VIEWER_SOURCES.json](../report_assets/VIEWER_SOURCES.json)，详见 [第三方说明](../THIRD_PARTY_NOTICES.md)。静态预览沿用现有 OCP 和 VTK，查看报告无需这些 CAD 依赖。

通用[建模技能](../agent/fusion-modeling/SKILL.md)从已使用的 Fusion MCP 操作流程整理，仅保留公开工具约定、原生 API 执行及提交注意事项，不包含账号、题目、模型答案或机器专用地址。
