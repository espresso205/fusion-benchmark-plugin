# HTML 四视角与可旋转模型报告

2026-10-27。本次发布增加四视角对比、材料差异显示和 CADViewer 交互，并提供现有 18 题结果的新版完整报告。只重建展示，不重跑评分；算法、权重和原始评分证据不变。

## 下载与打开

- [完整报告 ZIP](https://github.com/espresso205/fusion-benchmark-plugin/releases/download/html-report-20261027/cadgenbench-v4-multiview-report-20261027.zip)（108,510,054 字节，约 103.5 MiB）
- [SHA256 校验文件](https://github.com/espresso205/fusion-benchmark-plugin/releases/download/html-report-20261027/SHA256SUMS.txt)
- [发布页](https://github.com/espresso205/fusion-benchmark-plugin/releases/tag/html-report-20261027)

SHA256：`bf87ed6bfddf5c2b15393fe7fa078a28c06bb8aeef6997ee3012c942d58012b5`。

解压整个目录后打开 `index.html`，可离线阅读四视角图片、指标和分析。三维交互在解压后的目录运行：

```powershell
python -B serve_report.py --root . --open
```

Windows 也可双击 `打开三维报告.cmd`。查看端使用浏览器和 Python 标准库，不需要 CAD 库、Fusion 或 CDN。本附件是结果报告，不是包含运行时的完整插件安装包。

## 新增功能

- ISO、Front、Top、Right 四视角中，生成模型在左、标准答案在右；共用原点、相机与尺度。
- 生成 STEP 和标准答案的显示模型可同步旋转、缩放、平移，支持重置、单侧查看、透明叠加、差异着色和方向轴。
- 四视角比较图与差异图可放大，支持左右键切图和 Esc 关闭。直接打开 HTML 时保留静态角度切换。
- 任务类型、任务描述、类型均分、筛选排序、题号深链接和当前位置导航；下界与无分数不作分数颜色分档。
- 原始生成 STEP、标准答案及已有对齐 STEP 下载；提交 ZIP 只含 18 个原始生成模型。

公开资料可提供图片、多图、PDF、DXF、附件及预先生成的预览。可选私有接口清单与 GT 的 SHA256 绑定，详见 [HTML_REPORT.md](HTML_REPORT.md)。报告资料兼容不代表建模流水线已经支持 PDF/多图输入。

## 差异图的含义与边界

当前报告显示全模型材料差异：蓝色为匹配、红色为多余、琥珀色为缺失。它基于显示网格实体布尔，仅供观察，不产生新分数。

已支持提供真实 jig 区域后的 KOR/KIR Interface overlay。官方真实题的 jig 属于私有数据，本报告未取得这些区域，因此不能宣称复现官方 Interface Match 分数或接口标注。没有提供原始编辑模型和编辑真值时，也不构造编辑基准差异。

报告有 18 题、36 个坐标视图及 288 张四视角比较/叠加 PNG。32 个坐标视图完成差异着色；109、130、146 没有已对齐候选，另有 130 的原始坐标网格在布尔库导入时发生约 0.97% 体积变化。体积保护使该差异不可用，保留原模型和明确标注的普通透明叠加，避免展示失真的分区。

## 分数与验证

评分沿用 2026-10-26 的 v4 geometry 结果：15/18 参与、14 完整，均分 53.449/100（含缺测下界）。147 材料分布缺测，得分下界 53.073、补分范围 53.073–68.458；130、146 无效；109 验证超时，未计分，不是 0 分。详见[上版重评说明](HTML_REPORT_RELEASE_20261026.md)。

源码验证：评测器 215 项通过；插件 423 项通过、3 项可选历史测试跳过；Viewer 15 项通过。独立审查与体积守恒审计通过。535 份清单资源、164 个网格、51 个 STEP 下载均核验；全部 587 份原评分证据字节不变。报告保留 CADViewer 和 Three.js 的许可与来源记录。

通用源码仓库不放原始模型或评测产物；完整报告作为此次 Release 附件提供。上版报告和历史分数保持原样。
