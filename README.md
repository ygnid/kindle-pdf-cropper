# kindle-pdf-cropper

自动剪裁 PDF 四周白边与页眉/页脚、为 Kindle 生成阅读优化 PDF/EPUB 的命令行工具。

- 剪掉每页四周白边 + 页眉/页脚 + 页码，只保留正文内容；
- 可选 `--split`：把每页**上下切成两页**，输出**横版**页面，两页在切点处保留少量
  重叠，保证中线附近的一行不会显示不全，Kindle 横屏阅读字号接近翻倍；
- **自适应**文字型与扫描图片型 PDF；输出结果保持原样——文字仍是文字、
  扫描图仍是扫描图，**文字/公式/表格的排版完全不变**，只做整页剪裁与切分；
- 输出 **PDF**（文字/矢量/图片保真）或 **EPUB**（固定版式，排版不重排）。

技术选型调研见 [docs/research.md](docs/research.md)，算法设计见 [docs/design.md](docs/design.md)。

## 安装

要求 Python ≥ 3.10。

```bash
cd kindle-pdf-cropper
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux / macOS
pip install -e .
```

## 快速开始

```bash
# 只裁白边和页眉页脚，输出 PDF（默认）
kindle-crop 书.pdf

# 裁切 + 上下切分成横版两页（适合 Kindle）
kindle-crop 书.pdf --split -o out/书_横版

# 同时输出 PDF 和 EPUB（自动：文字页=SVG，扫描页=位图）
kindle-crop 书.pdf --format both -o out/书

# 扫描件：调大安全边距、提高 EPUB 位图质量
kindle-crop 扫描.pdf --padding 10 --epub-dpi 220 --format epub

# 调试：把每页的分析叠加图（裁剪框/页眉页脚带/切分框）导出成 PNG
kindle-crop 书.pdf --debug-dir debug
```

## 常用参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--format pdf\|epub\|both` | `pdf` | 输出格式 |
| `--split` | 关 | 每页上下切成两页横版 |
| `--split-overlap-pt` | `20` | 两页最小重叠（点），默认≈1.5 行 |
| `--split-overlap-ratio` | `0.03` | 重叠 = max(比例×裁剪高, 最小点数) |
| `--padding` | `6` | 内容框外保留的安全边距（点） |
| `--no-header-footer` | 关 | 只裁边距，不删除页眉/页脚/页码 |
| `--epub-mode image\|svg\|auto` | `auto` | 文字页→SVG，扫描页→位图 |
| `--epub-dpi` | `200` | EPUB 位图分辨率 |
| `--epub-image-format jpeg\|png` | `jpeg` | EPUB 位图格式 |
| `--analysis-dpi` | `144` | 分析用渲染分辨率 |
| `--debug-dir DIR` | — | 导出每页分析叠加图 |
| `-o PATH` | 输入名+`_cropped` | 输出路径（`both` 时自动补 `.pdf`/`.epub`） |

## 输出说明

- **PDF 输出**：每个输出页的页面尺寸就是裁剪后的尺寸（物理裁剪，而非仅改 CropBox，
  任何阅读器都稳定显示）；文字保持可选中的文字，表格线/公式等矢量、嵌入字体原样保留；
  目录与元数据保留。
- **EPUB 输出**：EPUB 是流式重排容器，若要保持排版不变只能用**固定版式**：
  每页钉死一个 viewport。
  - 文字页 → 内联 **SVG**（文字仍为文字，矢量图形保留）；
  - 扫描页 → 整页 **JPEG/PNG 位图**；
  - 若你的阅读器对 SVG/固定版式支持不佳（如部分 Kindle 固件），请用
    `--epub-mode image` 或直接使用 PDF 输出。

## 示例文件

`examples/` 下有生成的样例（文字型、扫描型、旋转扫描型）：
运行 `python tests/make_samples.py text examples/sample_text.pdf` 等重新生成。

## 开发与测试

```bash
pip install -e .[dev]
python -m pytest
```

测试覆盖：边距/页眉页脚分析、扫描与旋转页、PDF 输出文字保真、切分重叠、
EPUB 结构与内容校验、CLI 端到端。

## 许可

MIT，见 [LICENSE](LICENSE)。
