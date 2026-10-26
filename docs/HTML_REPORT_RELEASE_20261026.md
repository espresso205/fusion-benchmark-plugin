# HTML 报告与 CADGenBench 重评

2026-10-26。此次发布包含 HTML 报告功能源码，以及一份使用当前 v4 规则重新计算的 CADGenBench 示例报告。评分公式和权重不变。

## 下载和打开

- [完整报告 ZIP](https://github.com/espresso205/fusion-benchmark-plugin/releases/download/html-report-20261026/cadgenbench-v4-html-report-20261026.zip)
- [SHA256 校验文件](https://github.com/espresso205/fusion-benchmark-plugin/releases/download/html-report-20261026/SHA256SUMS.txt)
- [发布页](https://github.com/espresso205/fusion-benchmark-plugin/releases/tag/html-report-20261026)

解压整个 ZIP 后打开其中的 `index.html`，可离线查看模型预览、指标和分析。三维旋转、叠加及坐标切换使用报告自带的本机服务：

```powershell
python -B serve_report.py --root . --open
```

在解压后的报告目录中运行即可；Windows 也可双击 `打开三维报告.cmd`。查看端只需要浏览器和 Python 标准库，无需 CAD 依赖、Fusion 或联网下载资源。这个 ZIP 是完整的结果报告，不是含 Python 运行时的插件安装包。

## 本次结果

来源为[公开提交的官方报告](https://huggingai4engineering-cadgenbench.hf.space/reports/local-user_codex-current-original-49-20261018-01_20261018-114008.html)。49 道生成题实际产出 18 个 STEP，全部重新验证；其余 31 题无候选，32 道编辑题未执行。18 对候选、GT 和冻结标注均校验身份，采用 `fusion-step-adapted-v4.0` 的 `geometry` 模式，每题 600 秒，3 个工作进程。

|结果|数量或分数|
|---|---:|
|参与评分|15 / 18|
|主指标完整|14|
|参与者均分（包含缺测下界）|53.449 / 100|
|147：材料分布缺测|下界 53.073，补分范围 53.073–68.458|
|130、146：确认自交无效|2 题，未计分|
|109：有效性验证超时|1 题，未计分|

109 可以读取、显示，基础 BRep 检查和 CADGenBench 网格检查也通过；额外的 OCCT 实体自交检查在 600 秒总预算耗尽时仍未完成。因此未进入对齐和计分，不能视为 0 分，也不能据此判定模型无效。官网历史的 `valid` 和 CAD 0.952 属于不同协议，不能移植为本次分数。原始坐标视图中的朝向差异不代表对齐失败。

GT 使用从官网取得并冻结的参考字节；官网未公开固定 GT 版本，不能声称与历史评分使用的 GT 完全相同。尺寸标注为 GT 派生几何检查，并非独立图纸真值。本地分数不用于官方榜单，`ranking_eligible=false`。

## 功能与验证

报告提供左侧生成模型、右侧标准答案的静态及三维对比，原坐标/对齐后切换，题目搜索、筛选、排序，主指标及辅助指标、缺测说明和消融分析。支持可选的 PNG/JPG/WebP、多图、PDF 原文与预先提供的分页预览、DXF 预览/下载和其他附件。详细接口见 [HTML_REPORT.md](HTML_REPORT.md)；这不代表建模流水线已增加 PDF/多图读取。

发布前独立评测器 183 项测试通过，插件 391 项通过、3 项可选测试跳过，Viewer 7 项通过。重评结果与网页完成 2870 项一致性检查，18 题预览均可加载，758 份历史证据保持不变。报告保留 CADViewer、Three.js 的许可和来源记录；源码仓库不存放原始模型或运行结果，完整报告单独作为本次 Release 附件提供。
