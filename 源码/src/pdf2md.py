#!/usr/bin/env python3
"""
pdf2md.py -- 将 PDF 转换为 Markdown，仅保留文字。

策略:
  - 使用 pymupdf4llm 提取全部文字（保证不丢字）
  - 后处理：合并逐行为段落、去掉图片标记和OCR块、清理格式
"""

import os
import re
import time
from multiprocessing import Pool
from pathlib import Path

import fitz
import pymupdf4llm


# ============================================================
# 正则
# ============================================================
IMAGE_RE = re.compile(r"!\[.*?\]\(.*?\)")
HTML_IMG_RE = re.compile(r"<img[^>]*/?>", re.IGNORECASE)
PICTURE_BLOCK_RE = re.compile(
    r'\*\*----- Start of picture text -----\*\*.*?'
    r'\*\*----- End of picture text -----\*\*',
    re.DOTALL
)
PICTURE_PLACEHOLDER_RE = re.compile(
    r'\*\*==>\s*picture\s*\[\d+\s*x\s*\d+\]\s*intentionally omitted\s*<==\*\*'
)
SENTENCE_END_RE = re.compile(r'[.!?:]["\')]?$')
LIGATURE_RE = re.compile(r'([a-z])- ([a-z])')


def process_markdown_text(md_text):
    """
    后处理 pymupdf4llm 输出：
    1. 去掉图片占位符
    2. 去掉图片 OCR 文字块
    3. 合并碎行为自然段落
    4. 清理残留 artifact
    """
    md_text = IMAGE_RE.sub("", md_text)
    md_text = HTML_IMG_RE.sub("", md_text)
    md_text = PICTURE_BLOCK_RE.sub("", md_text)
    md_text = PICTURE_PLACEHOLDER_RE.sub("", md_text)

    pages = md_text.split('\n\n\n')
    processed_pages = []

    for page_text in pages:
        if not page_text.strip():
            continue

        lines = page_text.strip().split('\n')
        cleaned_lines = _clean_page_lines(lines)
        paragraphs = _merge_lines_to_paragraphs(cleaned_lines)

        if paragraphs:
            processed_pages.append('\n\n'.join(paragraphs))

    result = '\n\n'.join(processed_pages)
    result = _global_cleanup(result)
    return result


def _clean_page_lines(lines):
    cleaned = []
    for line in lines:
        stripped = line.strip()
        stripped = re.sub(r'!\[.*?\]\(.*?\)', '', stripped)

        if not stripped:
            cleaned.append('')
            continue
        if stripped in ('---', '***', '==>', '-->'):
            continue
        if re.match(r'^\d{1,4}$', stripped):
            continue
        if stripped == '**' or stripped == '****':
            continue
        if len(stripped) < 5 and re.match(r'^[\W_]+$', stripped):
            continue
        if re.match(r'^\*{0,2}[A-Z ]{5,} \| VOL \d+ \|', stripped):
            continue
        if re.match(r'^\*{0,2}(www\.|http|DOI:|Published|Downloaded)', stripped, re.IGNORECASE):
            continue
        if re.match(r'^\*{0,2}Page \d+ of \d+\*{0,2}$', stripped, re.IGNORECASE):
            continue

        cleaned.append(stripped)

    return cleaned


def _merge_lines_to_paragraphs(lines):
    if not lines:
        return []

    paragraphs = []
    current_para = []

    for line in lines:
        stripped = line.strip()

        if not stripped:
            if current_para:
                para = _finalize_paragraph(current_para)
                if para:
                    paragraphs.append(para)
                current_para = []
            continue

        if _is_heading_line(stripped, current_para):
            if current_para:
                para = _finalize_paragraph(current_para)
                if para:
                    paragraphs.append(para)
                current_para = []
            paragraphs.append(stripped)
            continue

        if current_para and _should_start_new_paragraph(stripped, current_para[-1]):
            para = _finalize_paragraph(current_para)
            if para:
                paragraphs.append(para)
            current_para = []

        current_para.append(stripped)

    if current_para:
        para = _finalize_paragraph(current_para)
        if para:
            paragraphs.append(para)

    return paragraphs


def _is_heading_line(line, current_para):
    stripped = line.strip().rstrip('*').strip()

    if line.strip().startswith('#'):
        return True

    if not current_para:
        return False

    words = stripped.split()
    if len(words) > 20:
        return False
    if len(stripped) > 150:
        return False

    heading_patterns = [
        r'^(Abstract|Introduction|Results?|Discussion|Methods?|Materials?|'
        r'Conclusions?|References?|Acknowledgments?|Supplementary|Appendix|'
        r'Background|Summary|Findings|Data|Experimental)',
        r'^(Figure|Fig\.|Table)\s*\d+',
        r'^[IVX]+\.\s',
        r'^\d+\.\s+\w',
    ]
    for pat in heading_patterns:
        if re.match(pat, stripped, re.IGNORECASE):
            return True

    return False


def _should_start_new_paragraph(current_line, prev_line):
    if SENTENCE_END_RE.search(prev_line.strip()):
        return True
    if current_line.startswith('  ') and not prev_line.startswith('  '):
        return True
    if re.match(r'^[•\-\*]\s', current_line.strip()):
        return True
    return False


def _finalize_paragraph(lines):
    if not lines:
        return ""

    text_lines = []
    for line in lines:
        s = line.strip()
        s = re.sub(r'\*{2}(.+?)\*{2}', r'\1', s)
        s = re.sub(r'\b_(.+?)_\b', r'\1', s)
        s = re.sub(r'~~(.+?)~~', r'\1', s)
        s = re.sub(r'^-\s*\d{1,4}\s{2,}', '', s)
        if s:
            text_lines.append(s)

    if not text_lines:
        return ""

    paragraph = " ".join(text_lines)
    paragraph = re.sub(r'([a-z]{3,})-\s+([a-z]{3,})', r'\1\2', paragraph)
    paragraph = LIGATURE_RE.sub(r'\1\2', paragraph)
    paragraph = re.sub(r'\s+', ' ', paragraph)

    stripped = paragraph.strip()
    if len(stripped) < 120 and stripped[0].isupper() and len(stripped.split()) <= 15:
        if not stripped.endswith(('.', '?', '!')):
            return f"## {stripped}"

    return paragraph.strip()


def _global_cleanup(text):
    text = re.sub(r'\n{4,}', '\n\n\n', text)
    text = re.sub(r'##\s*\n', '\n', text)
    text = re.sub(r'<br\s*/?>', '', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)

    lines = text.split('\n')
    cleaned = []
    for line in lines:
        s = line.strip()
        if not s:
            cleaned.append(line)
            continue
        if s in ('**', '--', '—', '>', '<', '==>'):
            continue
        if len(s) < 4 and re.match(r'^[\W\d]+$', s):
            continue
        if re.match(r'^[A-Z ]{6,} \| VOL \d+ \|', s):
            continue
        if re.match(r'^(www\.|http|DOI:)', s, re.IGNORECASE):
            continue
        if re.match(r'^\d{1,4} [A-Z ]+ \| VOL \d+ \| \d{1,2} [A-Z]+ \d{4}$', s):
            continue
        if s.lower() in ('comment', 'news', 'letter', 'review', 'article',
                          'editorial', 'perspective', 'research', 'report',
                          'correspondence', 'news feature', 'news & views'):
            continue
        cleaned.append(line)
    return '\n'.join(cleaned)


# ============================================================
# 单个文件转换
# ============================================================
def convert_single_pdf(pdf_path: Path, output_path: Path) -> tuple:
    """转换单个PDF为MD。返回 (success: bool, message: str, md_chars: int)。"""
    doc = None
    try:
        doc = fitz.open(str(pdf_path))

        if len(doc) == 0:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("", encoding="utf-8")
            return (True, "Empty PDF", 0)

        if doc.is_encrypted:
            if not doc.authenticate(""):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text("", encoding="utf-8")
                return (True, "Encrypted PDF (skipped)", 0)

        md_text = pymupdf4llm.to_markdown(
            doc, write_images=False, page_chunks=False
        )
        md_text = process_markdown_text(md_text)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(md_text, encoding="utf-8")

        return (True, f"OK ({len(md_text)} chars)", len(md_text))

    except fitz.FileDataError:
        return (False, "Corrupted PDF", 0)
    except fitz.EmptyFileError:
        return (False, "Empty file", 0)
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}", 0)
    finally:
        if doc is not None:
            doc.close()


def _worker_convert(args):
    pdf_path_str, output_path_str = args
    success, msg, chars = convert_single_pdf(Path(pdf_path_str), Path(output_path_str))
    return (pdf_path_str, success, msg, chars)


# ============================================================
# 批量转换入口（供 pipeline 调用）
# ============================================================
def convert_all(source_dir: Path, output_dir: Path, workers: int,
                filenames: set | None = None) -> list[dict]:
    """
    批量转换 PDF → MD。
    filenames: 只处理指定文件名的PDF，None=全部。
    返回 [{pdf_filename, pdf_path, success, message, md_chars, md_path}, ...]
    """
    pdf_paths = list(source_dir.rglob("*.pdf"))
    if filenames:
        pdf_paths = [p for p in pdf_paths if p.name in filenames]
    if not pdf_paths:
        print("未找到 PDF 文件。")
        return []

    tasks = []
    task_meta = []
    for pdf_path in pdf_paths:
        rel = pdf_path.relative_to(source_dir)
        tasks.append((str(pdf_path), str(output_dir / rel.with_suffix(".md"))))
        task_meta.append({
            "pdf_filename": pdf_path.name,
            "pdf_path": str(pdf_path),
            "md_path": str(output_dir / rel.with_suffix(".md")),
        })

    print(f"PDF 数量: {len(tasks)} → 输出: {output_dir} (并行: {workers})")

    for d in {str(Path(out).parent) for _, out in tasks}:
        os.makedirs(d, exist_ok=True)

    start = time.time()
    ok = fail = 0
    results = []

    meta_map = {m["pdf_filename"]: m for m in task_meta}

    with Pool(processes=workers) as pool:
        for pdf_path_str, is_ok, msg, chars in pool.imap_unordered(
            _worker_convert, tasks, chunksize=10
        ):
            pdf_filename = Path(pdf_path_str).name
            matched_meta = meta_map.get(pdf_filename, {})

            if is_ok:
                ok += 1
            else:
                fail += 1

            results.append({
                "pdf_filename": pdf_filename,
                "pdf_path": pdf_path_str,
                "success": is_ok,
                "message": msg,
                "md_chars": chars,
                "md_path": matched_meta.get("md_path", ""),
            })

            done = ok + fail
            print(f"\r  进度: {done}/{len(tasks)} (成功:{ok} 失败:{fail})",
                  end="", flush=True)

    print(f"\n\n耗时: {time.time() - start:.1f}s  成功: {ok}  失败: {fail}")
    return results
