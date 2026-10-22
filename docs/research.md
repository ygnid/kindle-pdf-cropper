# 技术调研：裁剪 PDF 的开发语言与库选型

> 结论先行：**Python + PyMuPDF + NumPy/Pillow** 是本项目最合适的技术栈。
> 本文档说明调研过程与理由，并附参考来源。

## 1. 需求回顾

程序要做的三件事：

1. **自动剪裁**每页四周白边 + 页眉/页脚，只保留中间内容；
2. 可选地把每页**上下切成两页**，转成横版（landscape）页面，两页之间保留少量重合，
   便于 Kindle 上放大阅读；
3. **自适应**文字型 PDF 和扫描图片型 PDF，输出仍是文字/扫描图，
   文字、公式、表格的排版不能被改变，只能做整体剪裁和切分。

"排版不能变 + 两种内容类型都要自适应 + 输出 PDF 或 EPUB" 这三个约束，直接决定了选型：

- 排版不能变 → 输出必须是**页面级的剪裁/切分**，而不是重新排版（reflow）；
- 两种内容自适应 → 必须能同时拿到**栅格（渲染）信号**和**文本（矢量）信号**；
- 输出 PDF/EPUB → 需要一个能写 PDF 的库，以及能生成固定版式 EPUB 的能力。

## 2. 现有工具对比（调研对象）

| 工具 | 语言 | 是否开源 | 主要能力 | 与本需求的差距 |
| --- | --- | --- | --- | --- |
| [k2pdfopt](https://www.willus.com/k2pdfopt/) | C | 是 | 专为 Kindle 优化：位图扫描页面、自动分栏、重排文字、OCR 文字层、原生 PDF 输出 | 功能最强，但是**独立二进制**，无 Python/库 API，重在“重排优化”而非“保持排版不变的裁剪”；页眉页脚处理主要靠人工参数；二次开发难 |
| [Briss](https://sourceforge.net/projects/briss/) | Java (Swing) | 是 | 交互式剪裁白边、自动检测边界、可拆分页面 | 交互式 GUI 为主；**只是设置 CropBox“隐藏”边距**，用户实测不同阅读器显示/打印时边距会“重新出现”；无页眉页脚检测；维护停滞（2013 年后） |
| [pdfcrop](https://ctan.org/pkg/pdfcrop)（TeX Live） | Perl | 是 | 用 Ghostscript 渲染后按空白边框裁剪，或按固定边框裁剪 | 只能裁“整页空白边”，对扫描件背景灰、噪点不鲁棒；**无法识别并去掉页眉页脚**；依赖 Perl/Ghostscript |
| [OCRmyPDF](https://ocrmypdf.readthedocs.io/) | Python | 是 | 给扫描 PDF 加 OCR 文字层 | 不做裁剪；可作为本项目后续增强 OCR 文字层的参考 |
| [pypdf](https://pypdf.readthedocs.io/) | Python | 是 | 纯 Python 读写 PDF，可改 cropbox | 纯解析，**不能渲染页面** → 无法做“墨水投影/页眉页脚”这类视觉分析 |
| [PyMuPDF](https://pymupdf.readthedocs.io/) | Python (C 绑定 MuPDF) | 是 | 原生 PDF 编辑（`set_cropbox`、`show_pdf_page`、`remove_rotation`）、**渲染位图**、**提取带坐标的文本/图片/矢量**、生成 SVG、目录/元数据 | 全部所需能力都在一个活跃维护的库里 |

## 3. 为什么选 Python

1. **生态**：PDF 分析/生成（PyMuPDF、pypdf、reportlab）、图像处理（NumPy、Pillow、OpenCV）、
   EPUB 生成（zipfile/ebooklib）、测试（pytest）都是成熟 Python 包；
2. **算法迭代快**：本项目核心是“视觉投影 + 文本启发式”算法，Python + NumPy 向量化实现
   直接、可读、易测试；页眉页脚这类启发式规则需要反复调参，解释型语言明显更快；
3. **与 OCR 生态衔接**：后续要加 OCR 文字层/歪斜校正时，可以直接复用 Tesseract / OpenCV，
   不必换语言（k2pdfopt 的 C 代码在这个方向上改动成本极高）。

## 4. 为什么核心库选 PyMuPDF

对比过 pypdf / PyPDF2 / reportlab 后，只有 PyMuPDF 同时提供：

- `Page.get_pixmap(dpi=…)` —— 把页面渲染成灰度位图（做墨水投影分析）；
- `Page.get_text("dict")` —— 返回**带 bbox / 字号 / 字体 / 颜色**的文本行，用于识别
  “重复出现的运行页眉、页脚页码”，并且能判断一个页面是文字型还是扫描型；
- `Page.get_image_info()` / `Page.get_drawings()` —— 图片框与矢量（表格线、公式路径）信息；
- `Page.show_pdf_page(rect, src, pno, clip=…)` —— **把源页内容按 clip 区域“物理复制”到
  新页面**，文字仍是文字、矢量仍是矢量、图片仍是图片（Form XObject，不栅格化）——这是
  实现“排版不变的整体裁剪”的关键 API；
- `Page.remove_rotation()` —— 把页面旋转“烘焙”进内容坐标，统一分析/渲染坐标系；
- `Document.set_toc / metadata` —— 输出 PDF 保留目录与元数据。

参考：
- [PyMuPDF Page API（show_pdf_page / get_pixmap / get_text / set_cropbox）](https://pymupdf.readthedocs.io/en/latest/page.html)
- [PyMuPDF 裁剪示例（recipes）](https://pymupdf.readthedocs.io/en/latest/recipes-cropping.html)
- [PyMuPDF PyPI](https://pypi.org/project/PyMuPDF/)

## 5. 为什么输出采用“物理裁剪”而非改 CropBox

Briss 采用只改 CropBox 的方式，用户在 SourceForge 评论中反馈：
“margins are just hidden … they will appear or not depending on your display/print
configuration”（[来源](https://sourceforge.net/projects/briss/)）。
不同阅读器对 CropBox 的支持不一致。因此本项目用
`show_pdf_page(clip=…)` 把裁剪区域**重新生成成新的页面尺寸**，任何阅读器看到的
页面大小就是裁剪后的大小，稳定可靠。

## 6. EPUB 输出方案调研

EPUB 是“流式重排”容器，天然会改变排版。要在 EPUB 里保持“排版不变”，只能使用
**固定版式（fixed-layout）**：每个输出页固定一个 viewport，页面内容不参与重排。

- W3C EPUB 3.3 固定版式规范：`rendition:layout=pre-paginated`（[来源](https://www.w3.org/TR/epub-33/#sec-fixed-layouts)）；
- Apple Books 用 `META-INF/com.apple.ibooks.display-options.xml` 开启固定版式；
- Amazon KDP/Kindle 也支持固定版式（[来源](https://kdp.amazon.com/en_US/help/topic/G200634390)）。

因此本项目产出两种固定版式页面策略：

- **image**：整页位图（JPEG/PNG），任何阅读器都能显示，排版绝对保真（等价于扫描书）；
- **svg**：用 PyMuPDF 的 `get_svg_image()` 生成内联 SVG 页面，**文字保持为文字、
  表格线/公式保持矢量**，同时用固定 viewport 钉住排版。

对于扫描型 PDF，输出自然是图片页；文字型 PDF 默认走 SVG 页，从而尽量满足
“输出仍是文字”的要求。

## 7. 结论

| 需求 | 技术方案 |
| --- | --- |
| 文字/扫描自适应分析 | PyMuPDF 渲染灰度位图 → NumPy 投影 → 文本 bbox 补充 |
| 排版不变的 PDF 裁剪/切分 | PyMuPDF `show_pdf_page(clip=…)` 物理裁剪 |
| EPUB 输出 | 固定版式 EPUB 3，SVG（文字页）/位图（扫描页） |
| 页眉页脚/页码 | 文本行聚类（跨页重复检测 + 页码模式）+ 墨水带间隙检测 |
| 开发语言 | Python ≥3.10 |

选型与同类项目（k2pdfopt、Briss、pdfcrop、OCRmyPDF、pypdf、PyMuPDF）的详细对比见上表。
