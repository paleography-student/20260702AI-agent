#!/usr/bin/env python3
"""分析会议论文集、集刊或图书 PDF，并输出供拆分和 Zotero 导入使用的 manifest。

这个脚本只做“分析”和“生成候选清单”，不会写入 Zotero，也不会修改原始 PDF。
它的目标是把容易重复、容易出错的 PDF 基础检查固定下来：
1. 读取页数、书签、前后页文本；
2. 判断 PDF 更像会议论文集、集刊还是图书；
3. 从书签或目录页中提取单篇论文候选；
4. 在文本抽取效果差时给出 MinerU OCR 的建议或结果路径。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    import pdfplumber
except Exception:  # pragma: no cover - 运行环境缺包时仍允许用 pypdf 降级
    pdfplumber = None

from pypdf import PdfReader


# 目录行常见形态：
#   文章标题........12
#   Article Title  Author Name  23
#   文章标题 / 作者 45
# 这里只做保守解析，复杂目录仍应由 Codex PDF 插件或 MinerU OCR 辅助复核。
TOC_LINE_RE = re.compile(r"^(?P<body>.+?)(?:[.\u2026·\s]{2,}|[\t ]+)(?P<page>\d{1,4})\s*$")


def read_pdf_text_with_pypdf(reader: PdfReader, page_indexes: Iterable[int]) -> dict[int, str]:
    """用 pypdf 抽取指定页文本。

    pypdf 抽取速度快，适合先做低成本筛查；但它可能破坏多栏目录的阅读顺序，
    所以后续还会用 pdfplumber 和视觉/OCR 流程补强。
    返回值用 1-based 页码作为 key，便于人工复核时直接对应 PDF 阅读器页码。
    """

    texts: dict[int, str] = {}
    page_count = len(reader.pages)
    for index in page_indexes:
        if index < 0 or index >= page_count:
            continue
        try:
            texts[index + 1] = reader.pages[index].extract_text() or ""
        except Exception as exc:
            texts[index + 1] = f"[pypdf extraction failed: {exc}]"
    return texts


def read_pdf_text_with_pdfplumber(pdf_path: Path, page_indexes: Iterable[int]) -> dict[int, str]:
    """用 pdfplumber 抽取指定页文本。

    pdfplumber 对排版信息更敏感，经常比 pypdf 更适合目录页和版权页。
    如果运行环境没有 pdfplumber，返回空字典，让调用方继续使用 pypdf 结果。
    """

    if pdfplumber is None:
        return {}

    texts: dict[int, str] = {}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for index in page_indexes:
            if index < 0 or index >= len(pdf.pages):
                continue
            try:
                texts[index + 1] = pdf.pages[index].extract_text(x_tolerance=1, y_tolerance=3) or ""
            except Exception as exc:
                texts[index + 1] = f"[pdfplumber extraction failed: {exc}]"
    return texts


def flatten_outline(reader: PdfReader) -> list[dict[str, Any]]:
    """递归展开 PDF 书签。

    pypdf 的 outline 可能是嵌套列表，也可能包含无法解析页码的对象。
    这里保留层级 level，后续由 agent 判断哪些书签是真正的论文起始页。
    """

    outline_items: list[dict[str, Any]] = []

    def walk(nodes: Iterable[Any], level: int) -> None:
        for node in nodes:
            if isinstance(node, list):
                walk(node, level + 1)
                continue
            title = getattr(node, "title", None)
            if not title:
                continue
            try:
                page_number = reader.get_destination_page_number(node) + 1
            except Exception:
                page_number = None
            outline_items.append(
                {
                    "title": str(title).strip(),
                    "pdf_page": page_number,
                    "level": level,
                }
            )

    try:
        walk(reader.outline, 0)
    except Exception:
        return []
    return outline_items


def text_density_is_sparse(text_by_page: dict[int, str]) -> bool:
    """判断文本抽取是否稀疏。

    如果前若干页平均字符数很低，通常意味着扫描件、图片型 PDF、加密字体或抽取失败；
    这种情况应触发 Codex PDF 插件视觉检查或 MinerU OCR。
    """

    usable_texts = [text.strip() for text in text_by_page.values() if text and not text.startswith("[")]
    if not usable_texts:
        return True
    average_chars = sum(len(text) for text in usable_texts) / max(len(usable_texts), 1)
    return average_chars < 80


def classify_document(type_hint: str, combined_text: str, outline_count: int) -> dict[str, Any]:
    """根据用户提示和 PDF 文本给出文档类型候选。

    这里返回候选和置信度，而不是强行得出最终答案；Zotero item type 和是否拆分都依赖
    这个判断，所以低置信度时应交给上层 agent 询问用户。
    """

    hint = type_hint.lower()
    text = combined_text.lower()
    scores = {
        "conference_proceedings": 0,
        "collected_journal": 0,
        "book": 0,
    }

    if any(word in hint for word in ["会议论文集", "会议", "proceedings", "conference"]):
        scores["conference_proceedings"] += 5
    if any(word in hint for word in ["集刊", "辑刊", "collected journal"]):
        scores["collected_journal"] += 5
    if any(word in hint for word in ["书籍", "图书", "book", "整本"]):
        scores["book"] += 5

    for word in ["会议", "研讨会", "论坛", "proceedings", "conference", "symposium"]:
        if word in text:
            scores["conference_proceedings"] += 2
    for word in ["集刊", "辑刊", "主编", "编者", "论文集"]:
        if word in text:
            scores["collected_journal"] += 2
    for word in ["isbn", "出版社", "出版", "版次", "印刷", "cip"]:
        if word in text:
            scores["book"] += 1
            scores["collected_journal"] += 1

    if outline_count >= 5:
        scores["conference_proceedings"] += 1
        scores["collected_journal"] += 1

    best_type = max(scores, key=scores.get)
    best_score = scores[best_type]
    second_score = sorted(scores.values(), reverse=True)[1]
    confidence = "high" if best_score >= second_score + 3 and best_score >= 4 else "medium" if best_score >= 3 else "low"
    return {"type": best_type, "confidence": confidence, "scores": scores}


def detect_toc_pages(text_by_page: dict[int, str]) -> list[int]:
    """在前若干页中寻找目录页。

    目录页常见信号包括“目录/目次/Contents”和多行以页码结尾的条目。
    这里只返回候选页，复杂跨页目录仍需要 agent 用 PDF 插件或 OCR 复核。
    """

    toc_pages: list[int] = []
    for page, text in text_by_page.items():
        normalized = text.strip()
        if not normalized:
            continue
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        page_line_hits = sum(1 for line in lines if TOC_LINE_RE.match(line))
        has_heading = any(keyword in normalized for keyword in ["目录", "目次", "Contents", "CONTENTS"])
        if has_heading or page_line_hits >= 3:
            toc_pages.append(page)
    return toc_pages


def split_title_and_authors(raw_body: str) -> tuple[str, list[str], str]:
    """从目录条目正文中尽量拆出标题和作者。

    不同论文集的目录差异很大。为了避免误拆中文姓名，本函数只在有明确分隔符时拆分；
    否则把整段作为标题，并把作者留空，后续由 agent 复核或从正文首页补抽。
    """

    body = re.sub(r"[.\u2026·]+", " ", raw_body).strip()
    body = re.sub(r"\s+", " ", body)
    separators = [" / ", " ／ ", "\t", "  "]
    for sep in separators:
        if sep in raw_body:
            left, right = raw_body.rsplit(sep, 1)
            title = left.strip(" .\u2026·\t")
            authors_raw = right.strip(" .\u2026·\t")
            authors = [part.strip() for part in re.split(r"[;；、,，]+", authors_raw) if part.strip()]
            return title or body, authors, authors_raw
    return body, [], ""


def parse_toc_entries(text_by_page: dict[int, str], toc_pages: list[int]) -> list[dict[str, Any]]:
    """从目录候选页中解析论文条目候选。

    输出中的 `printed_start_page` 是目录上印刷的页码，不一定等于 PDF 实际页码。
    后续要用第一页论文的实际 PDF 页计算 offset。
    """

    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for page in toc_pages:
        text = text_by_page.get(page, "")
        for line in text.splitlines():
            clean_line = line.strip()
            match = TOC_LINE_RE.match(clean_line)
            if not match:
                continue
            title, authors, authors_raw = split_title_and_authors(match.group("body"))
            printed_page = int(match.group("page"))
            key = (title, printed_page)
            if not title or key in seen:
                continue
            seen.add(key)
            entries.append(
                {
                    "title": title,
                    "authors": authors,
                    "authors_raw": authors_raw,
                    "toc_page": page,
                    "printed_start_page": printed_page,
                    "pdf_start_page": None,
                    "pdf_end_page": None,
                    "source": "toc",
                    "needs_review": not authors,
                }
            )
    return entries


def outline_to_article_candidates(outline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把书签转换为论文候选。

    只选择带页码的叶子/低层级条目。目录、前言、版权页等明显非论文标题会被过滤。
    """

    blocked_words = ["目录", "目次", "前言", "序", "版权", "contents", "preface", "copyright"]
    candidates: list[dict[str, Any]] = []
    for item in outline:
        title = item.get("title", "").strip()
        page = item.get("pdf_page")
        if not title or not page:
            continue
        if any(word.lower() in title.lower() for word in blocked_words):
            continue
        candidates.append(
            {
                "title": title,
                "authors": [],
                "authors_raw": "",
                "toc_page": None,
                "printed_start_page": None,
                "pdf_start_page": page,
                "pdf_end_page": None,
                "source": "outline",
                "needs_review": True,
            }
        )
    return candidates


def locate_first_article_page(reader: PdfReader, first_title: str, max_pages: int = 40) -> int | None:
    """在 PDF 前若干页中搜索第一篇论文标题，估算实际 PDF 起始页。

    该函数只用于辅助计算目录页码偏移。如果标题过短或抽取文本不稳定，可能找不到，
    此时 manifest 会保留空值，要求上层 agent 或用户确认。
    """

    normalized_title = re.sub(r"\s+", "", first_title)
    if len(normalized_title) < 4:
        return None
    for index in range(min(len(reader.pages), max_pages)):
        try:
            page_text = reader.pages[index].extract_text() or ""
        except Exception:
            continue
        normalized_page_text = re.sub(r"\s+", "", page_text)
        if normalized_title[:20] in normalized_page_text:
            return index + 1
    return None


def apply_page_offset(reader: PdfReader, entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int | None]:
    """根据第一篇文章计算印刷页码到 PDF 页码的偏移量。

    页码规则：manifest 和用户报告都使用 1-based PDF 页码；真正写 PDF 时再转成 0-based。
    """

    toc_entries = [entry for entry in entries if entry.get("printed_start_page")]
    if not toc_entries:
        return entries, None

    first_entry = toc_entries[0]
    actual_first_page = locate_first_article_page(reader, first_entry["title"])
    if actual_first_page is None:
        return entries, None

    offset = actual_first_page - int(first_entry["printed_start_page"])
    for entry in toc_entries:
        entry["pdf_start_page"] = int(entry["printed_start_page"]) + offset

    starts = [entry["pdf_start_page"] for entry in toc_entries if entry.get("pdf_start_page")]
    for index, entry in enumerate(toc_entries):
        if index + 1 < len(starts):
            entry["pdf_end_page"] = starts[index + 1] - 1
        else:
            entry["pdf_end_page"] = len(reader.pages)
    return entries, offset


def extract_metadata_candidates(text: str) -> dict[str, Any]:
    """从封面、版权页、目录附近文本中抽取书目信息候选。

    这里不追求一次性完美解析，只提取高价值字段和原始证据，便于 Zotero 导入前复核。
    """

    isbn_match = re.search(r"ISBN(?:\s|:|：)*([0-9Xx\-\s]{10,20})", text)
    year_match = re.search(r"(19|20)\d{2}", text)
    publisher_match = re.search(r"([\u4e00-\u9fa5A-Za-z0-9（）()·\s]{2,40}出版社)", text)
    place_match = re.search(r"(北京|上海|天津|重庆|南京|杭州|广州|成都|武汉|西安|长沙|济南|郑州|沈阳|长春|哈尔滨)", text)

    return {
        "title": None,
        "conference_name": None,
        "conference_date": None,
        "conference_place": place_match.group(1) if place_match else None,
        "editors": [],
        "publisher": publisher_match.group(1).strip() if publisher_match else None,
        "publication_place": place_match.group(1) if place_match else None,
        "publication_date": year_match.group(0) if year_match else None,
        "isbn": isbn_match.group(1).replace(" ", "") if isbn_match else None,
        "evidence_excerpt": text[:1500],
    }


def run_mineru_if_needed(pdf_path: Path, pages: str, output_root: Path | None, mode: str) -> dict[str, Any]:
    """按需调用 MinerU OCR，并返回执行信息。

    mode 为 auto 时仅在文本稀疏时由调用方调用本函数；mode 为 always 时强制调用。
    这里只调用 CLI，不解析 MinerU 输出内容，因为不同版本输出文件名可能不同。
    """

    mineru = shutil.which("mineru-open-api")
    if not mineru:
        return {"used": False, "reason": "mineru-open-api not found"}

    digest = hashlib.md5(str(pdf_path).encode("utf-8")).hexdigest()[:6]
    target_dir = output_root or Path.home() / "MinerU-Skill" / f"{pdf_path.stem}_{digest}"
    target_dir.mkdir(parents=True, exist_ok=True)

    command = [
        mineru,
        "flash-extract",
        str(pdf_path),
        "--pages",
        pages,
        "--language",
        "ch",
        "-o",
        str(target_dir),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, timeout=900)
    return {
        "used": completed.returncode == 0,
        "mode": "flash-extract",
        "pages": pages,
        "output_dir": str(target_dir),
        "returncode": completed.returncode,
        "stderr": completed.stderr[-2000:],
        "stdout": completed.stdout[-2000:],
    }


def build_manifest(pdf_path: Path, type_hint: str, mineru_mode: str, mineru_output: Path | None) -> dict[str, Any]:
    """构建单个 PDF 的分析 manifest。"""

    reader = PdfReader(str(pdf_path))
    page_count = len(reader.pages)
    front_indexes = list(range(min(10, page_count)))
    copyright_indexes = list(range(min(5, page_count))) + list(range(max(page_count - 2, 0), page_count))

    pypdf_front = read_pdf_text_with_pypdf(reader, front_indexes)
    plumber_front = read_pdf_text_with_pdfplumber(pdf_path, front_indexes)
    pypdf_copyright = read_pdf_text_with_pypdf(reader, copyright_indexes)
    plumber_copyright = read_pdf_text_with_pdfplumber(pdf_path, copyright_indexes)

    # 优先使用 pdfplumber 的目录/版权页文本；缺页时回退到 pypdf。
    front_text = {page: plumber_front.get(page) or pypdf_front.get(page, "") for page in pypdf_front}
    copyright_text = {page: plumber_copyright.get(page) or pypdf_copyright.get(page, "") for page in pypdf_copyright}
    combined_text = "\n".join(front_text.values()) + "\n" + "\n".join(copyright_text.values())

    outline = flatten_outline(reader)
    classification = classify_document(type_hint, combined_text, len(outline))
    toc_pages = detect_toc_pages(front_text)
    toc_entries = parse_toc_entries(front_text, toc_pages)
    outline_entries = outline_to_article_candidates(outline)

    articles = outline_entries if outline_entries else toc_entries
    page_offset = None
    if not outline_entries and toc_entries:
        articles, page_offset = apply_page_offset(reader, toc_entries)

    sparse = text_density_is_sparse(front_text)
    mineru_result = {"used": False, "reason": "not needed"}
    if mineru_mode == "always" or (mineru_mode == "auto" and sparse):
        mineru_result = run_mineru_if_needed(pdf_path, "1-10", mineru_output, mineru_mode)

    return {
        "schema_version": "1.0",
        "pdf_path": str(pdf_path.resolve()),
        "page_count": page_count,
        "document_type": classification,
        "text_extraction": {
            "sparse": sparse,
            "front_pages_sampled": list(front_text.keys()),
            "copyright_pages_sampled": sorted(copyright_text.keys()),
            "mineru": mineru_result,
        },
        "outline": outline,
        "toc_pages": toc_pages,
        "page_offset": page_offset,
        "metadata": extract_metadata_candidates(combined_text),
        "articles": articles,
        "review_required": classification["confidence"] == "low" or sparse or any(article.get("needs_review") for article in articles),
        "notes": [
            "PDF page numbers in this manifest are 1-based.",
            "Review document_type, article boundaries, and authors before Zotero writes when confidence is low.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze a PDF and generate a Zotero split/import manifest.")
    parser.add_argument("pdf", help="要分析的 PDF 文件路径")
    parser.add_argument("--output", "-o", help="manifest JSON 输出路径；不提供时输出到 stdout")
    parser.add_argument("--type-hint", default="", help="来自用户提示词的类型线索，例如 会议论文集、集刊、书籍")
    parser.add_argument(
        "--use-mineru",
        choices=["auto", "always", "never"],
        default="auto",
        help="OCR 策略：auto 在文本稀疏时调用 MinerU，always 强制调用，never 禁用",
    )
    parser.add_argument("--mineru-output", help="MinerU 输出目录")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 2
    if pdf_path.suffix.lower() != ".pdf":
        print(f"Input is not a PDF: {pdf_path}", file=sys.stderr)
        return 2

    manifest = build_manifest(
        pdf_path=pdf_path,
        type_hint=args.type_hint,
        mineru_mode=args.use_mineru,
        mineru_output=Path(args.mineru_output).expanduser().resolve() if args.mineru_output else None,
    )
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
