#!/usr/bin/env python3
"""
export_pdf.py — 将 Markdown 报告导出为 PDF
1. 提升标题层级（## → # 章级，### → ## 节级，…）
2. 渲染 Mermaid 代码块为 PNG（利用系统 Chrome）
3. 用 pandoc + xelatex 生成 PDF（中文支持）
"""

import os
import re
import subprocess
import shutil
import sys
from pathlib import Path

REPORT_DIR = Path(__file__).parent
REPORT_MD = REPORT_DIR / "多智能体协作与演化技术调研报告.md"
FIGURES_DIR = REPORT_DIR / "figures"
OUTPUT_PDF = REPORT_DIR / "多智能体协作与演化技术调研报告.pdf"
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


# ── Mermaid rendering ──────────────────────────────────────

def render_mermaid_blocks(md_text: str) -> str:
    """Replace ```mermaid blocks with rendered PNG images."""
    mermaid_dir = FIGURES_DIR / "mermaid"
    mermaid_dir.mkdir(exist_ok=True)
    puppeteer_config = mermaid_dir / "puppeteer-config.json"
    puppeteer_config.write_text('{"args":["--no-sandbox"]}', encoding="utf-8")

    # Determine mmdc command
    mmdc_cmd = None
    if shutil.which("mmdc"):
        mmdc_cmd = ["mmdc"]
    else:
        # Try npx with Chrome path
        mmdc_cmd = ["npx", "--yes", "@mermaid-js/mermaid-cli"]

    counter = [0]

    def replace_block(match):
        counter[0] += 1
        code = match.group(1).strip()
        idx = counter[0]

        mmd_file = mermaid_dir / f"mermaid_{idx}.mmd"
        png_file = mermaid_dir / f"mermaid_{idx}.png"

        mmd_file.write_text(code, encoding="utf-8")

        env = os.environ.copy()
        env["PUPPETEER_EXECUTABLE_PATH"] = CHROME_PATH

        try:
            cmd = mmdc_cmd + [
                "-i", str(mmd_file),
                "-o", str(png_file),
                "-t", "default",
                "-b", "white",
                "-w", "1200",
                "-s", "2",
                "-p", str(puppeteer_config),
            ]
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=60,
                env=env,
            )
            if png_file.exists() and png_file.stat().st_size > 100:
                rel = os.path.relpath(png_file, REPORT_DIR)
                print(f"    mermaid_{idx}: OK → {rel}")
                return f"\n![](  {rel})\n"
            if result.returncode != 0:
                print(f"    mermaid_{idx}: SKIP ({result.stderr[-300:].strip()})")
        except Exception as e:
            print(f"    mermaid_{idx}: SKIP ({e})")

        # Fallback: keep as code block
        return f"\n```\n{code}\n```\n"

    return re.sub(r"```mermaid\n(.*?)```", replace_block, md_text, flags=re.DOTALL)


# ── Markdown processing ────────────────────────────────────

def process_markdown(md_text: str) -> str:
    """Strip header, fix heading levels, remove manual TOC."""
    lines = md_text.split("\n")

    # Find content start
    start_idx = 0
    for i, line in enumerate(lines):
        if line.startswith("## 摘要"):
            start_idx = i
            break

    content = "\n".join(lines[start_idx:])

    # Remove manual TOC
    content = re.sub(
        r"## 目录\n\n(- \[.*?\]\(.*?\)\n)+\n---",
        "",
        content, flags=re.MULTILINE,
    )

    # Promote heading levels: ## → #, ### → ##, #### → ###, ##### → ####
    # This makes "第 X 章" become top-level sections (numbered 1, 2, 3…)
    new_lines = []
    for line in content.split("\n"):
        if line.startswith("#"):
            m = re.match(r"^(#+)", line)
            if m and len(m.group(1)) >= 2:
                line = line[1:]  # remove one #
        new_lines.append(line)
    content = "\n".join(new_lines)

    # Fix image path whitespace
    content = re.sub(r"!\[\]\(\s+", "![](", content)

    return content


# ── Pandoc metadata ────────────────────────────────────────

def create_pandoc_metadata() -> str:
    return r"""---
title: "多智能体协作与演化技术调研报告"
subtitle: "运维多智能体协作技术研究项目"
author:
  - 中山大学
date: "2026 年 4 月"
documentclass: article
papersize: a4
fontsize: 11pt
classoption:
  - UTF8
geometry:
  - top=25mm
  - bottom=25mm
  - left=25mm
  - right=25mm
header-includes:
  - \usepackage{xeCJK}
  - \setCJKmainfont{Songti SC}
  - \setCJKsansfont{Heiti SC}
  - \setCJKmonofont{Heiti SC}
  - \usepackage{longtable}
  - \usepackage{booktabs}
  - \usepackage{graphicx}
  - \usepackage{float}
  - \let\origfigure\figure
  - \let\endorigfigure\endfigure
  - \renewenvironment{figure}[1][htbp]{\origfigure[H]}{\endorigfigure}
  - \usepackage{hyperref}
  - \hypersetup{colorlinks=true, linkcolor=blue, urlcolor=blue, citecolor=blue}
  - \setlength{\parskip}{0.5em}
  - \setlength{\parindent}{2em}
  - \usepackage{enumitem}
  - \setlist{nosep}
toc: true
toc-depth: 3
numbersections: false
---

"""


# ── PDF build ──────────────────────────────────────────────

def build_pdf(md_path: Path) -> bool:
    cmd = [
        "pandoc", str(md_path),
        "-o", str(OUTPUT_PDF),
        "--pdf-engine=xelatex",
        "--resource-path", str(REPORT_DIR),
        "--wrap=auto",
    ]
    print("  Running: pandoc → xelatex → PDF ...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        print(f"  ERROR:\n{result.stderr[-1500:]}")
        if "ctex" in result.stderr or "fontspec" in result.stderr:
            print("  Retrying with fallback...")
            return build_pdf_fallback(md_path)
        return False
    return OUTPUT_PDF.exists()


def build_pdf_fallback(md_path: Path) -> bool:
    cmd = [
        "pandoc", str(md_path),
        "-o", str(OUTPUT_PDF),
        "--pdf-engine=xelatex",
        "--resource-path", str(REPORT_DIR),
        "-V", "CJKmainfont=Songti SC",
        "-V", "mainfont=Songti SC",
        "-V", "sansfont=Heiti SC",
        "-V", "monofont=Menlo",
        "-V", "geometry:margin=25mm",
        "--toc", "--toc-depth=3", "-N",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(f"  Fallback failed:\n{result.stderr[-1000:]}")
        return False
    return OUTPUT_PDF.exists()


# ── Main ───────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  导出 PDF: 多智能体协作与演化技术调研报告")
    print("=" * 60)

    if not REPORT_MD.exists():
        print(f"ERROR: {REPORT_MD} not found"); sys.exit(1)

    # 1. Read
    print("\n[1/4] 读取 Markdown...")
    md_text = REPORT_MD.read_text(encoding="utf-8")
    print(f"  {len(md_text)} chars, {md_text.count(chr(10))} lines")

    # 2. Render Mermaid
    print("[2/4] 渲染 Mermaid 图表...")
    md_text = render_mermaid_blocks(md_text)

    # 3. Process
    print("[3/4] 处理 Markdown（调整标题层级）...")
    metadata = create_pandoc_metadata()
    content = process_markdown(md_text)
    processed = metadata + content

    processed_path = REPORT_DIR / "_processed_report.md"
    processed_path.write_text(processed, encoding="utf-8")

    # 4. PDF
    print("[4/4] 生成 PDF...")
    ok = build_pdf(processed_path)
    processed_path.unlink(missing_ok=True)

    if ok:
        mb = OUTPUT_PDF.stat().st_size / (1024 * 1024)
        print(f"\n{'='*60}")
        print(f"  PDF 导出成功!")
        print(f"  文件: {OUTPUT_PDF}")
        print(f"  大小: {mb:.1f} MB")
        print(f"{'='*60}")
    else:
        print("\nERROR: PDF 导出失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
