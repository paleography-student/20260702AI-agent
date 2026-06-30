#!/usr/bin/env python3
"""根据 manifest 将论文集或集刊 PDF 拆分成单篇 PDF。

本脚本只负责文件拆分和安全命名，不负责写入 Zotero。Zotero 写入由 agent 使用
`mcp__zotero_mcp` 工具完成。这样可以把可重复、可测试的 PDF 操作和需要交互/确认
的 Zotero 操作分开。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter


# Windows 文件名不能包含这些字符；ASCII 0-31 控制字符也不能出现。
INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Windows 设备保留名，即使带扩展名也不适合作为文件名主体。
RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def resolve_output_dir(project_dir: Path | None, explicit_output: Path | None) -> Path:
    """按用户要求解析临时拆分目录。

    优先级：
    1. 显式 --output-dir；
    2. 已存在的 A:\\temppdf；
    3. 项目目录下 temppdf；
    4. 用户主目录 .codex\\temppdf。
    """

    if explicit_output:
        explicit_output.mkdir(parents=True, exist_ok=True)
        return explicit_output

    preferred = Path("A:/temppdf")
    if preferred.exists() and preferred.is_dir():
        return preferred

    if project_dir and project_dir.exists():
        fallback = project_dir / "temppdf"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    fallback = Path.home() / ".codex" / "temppdf"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def sanitize_component(value: str, default: str) -> str:
    """清理 Windows 非法文件名字符。

    标题和作者来自 OCR/目录页，可能包含斜杠、冒号、引号、换行或控制字符。
    这些字符会导致 Windows 创建文件失败，所以统一替换为空格并压缩空白。
    """

    cleaned = INVALID_FILENAME_CHARS_RE.sub(" ", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = default
    if cleaned.upper() in RESERVED_WINDOWS_NAMES:
        cleaned = f"{cleaned}_"
    return cleaned


def first_author_text(article: dict[str, Any]) -> str:
    """取得用于文件名的作者文本。

    manifest 中 authors 可能是列表，也可能只有 authors_raw。文件名只需要可读、
    稳定，不需要承载完整作者列表；路径过长时会优先保留首位作者。
    """

    authors = article.get("authors") or []
    if isinstance(authors, list) and authors:
        return str(authors[0])
    raw = article.get("authors_raw") or ""
    if raw:
        parts = [part.strip() for part in re.split(r"[;；、,，]+", str(raw)) if part.strip()]
        if parts:
            return parts[0]
        return str(raw).strip()
    return "未知作者"


def build_safe_pdf_path(output_dir: Path, article: dict[str, Any], index: int, max_total_length: int) -> Path:
    """生成 `标题+作者.pdf` 格式的安全路径，并确保完整路径不超过限制。

    Windows 传统路径长度限制是 260 左右。用户要求不超过 256 字符，因此这里以完整
    字符串长度计算；如果超长，保留标题前段、首位作者和 8 位哈希，保证可追溯。
    """

    title = sanitize_component(str(article.get("title") or f"未命名论文{index:03d}"), f"未命名论文{index:03d}")
    author = sanitize_component(first_author_text(article), "未知作者")
    base = f"{title}+{author}"
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    candidate = output_dir / f"{base}.pdf"

    if len(str(candidate)) <= max_total_length:
        return candidate

    # 为目录、扩展名和哈希预留空间。路径预算不足时继续收缩标题。
    fixed = f"+{author}_{digest}.pdf"
    available = max_total_length - len(str(output_dir)) - 1 - len(fixed)
    if available < 12:
        author = author[:8].strip(" .") or "作者"
        fixed = f"+{author}_{digest}.pdf"
        available = max_total_length - len(str(output_dir)) - 1 - len(fixed)
    short_title = title[: max(8, available)].strip(" .") or f"论文{index:03d}"
    return output_dir / f"{short_title}{fixed}"


def normalize_page_range(article: dict[str, Any], next_article: dict[str, Any] | None, page_count: int) -> tuple[int, int]:
    """把 manifest 中的 1-based 页码转换成有效的 1-based 起止页。

    pypdf 写出时才转 0-based。这里先保留 1-based，便于错误信息和人工核查。
    如果当前条目没有 end page，但下一条有 start page，则默认结束于下一条前一页。
    """

    start = article.get("pdf_start_page") or article.get("start_page")
    end = article.get("pdf_end_page") or article.get("end_page")
    if start is None:
        raise ValueError(f"Missing start page for article: {article.get('title')}")
    start_int = int(start)

    if end is None and next_article:
        next_start = next_article.get("pdf_start_page") or next_article.get("start_page")
        if next_start is not None:
            end = int(next_start) - 1
    end_int = int(end) if end is not None else page_count

    if start_int < 1 or end_int < start_int or end_int > page_count:
        raise ValueError(
            f"Invalid page range {start_int}-{end_int} for {article.get('title')} in a {page_count}-page PDF"
        )
    return start_int, end_int


def split_article(reader: PdfReader, start_page: int, end_page: int, output_path: Path) -> None:
    """写出单篇论文 PDF。

    输入页码是 1-based；pypdf 的 pages 列表是 0-based，所以循环时要减 1。
    """

    writer = PdfWriter()
    for one_based_page in range(start_page, end_page + 1):
        writer.add_page(reader.pages[one_based_page - 1])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as file:
        writer.write(file)


def split_from_manifest(manifest_path: Path, output_dir: Path, max_path_length: int) -> dict[str, Any]:
    """读取 manifest 并拆分所有可拆分文章。"""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pdf_path = Path(manifest["pdf_path"]).expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"Source PDF not found: {pdf_path}")

    reader = PdfReader(str(pdf_path))
    page_count = len(reader.pages)
    articles = manifest.get("articles") or []
    if not articles:
        raise ValueError("Manifest has no articles to split")

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    used_paths: set[str] = set()

    for index, article in enumerate(articles, start=1):
        try:
            next_article = articles[index] if index < len(articles) else None
            start_page, end_page = normalize_page_range(article, next_article, page_count)
            output_path = build_safe_pdf_path(output_dir, article, index, max_path_length)

            # 防止不同条目清理后文件名相同。重复时追加序号，同时继续保持路径长度限制。
            if str(output_path).lower() in used_paths:
                stem = output_path.stem
                suffix = f"_{index:03d}.pdf"
                allowed = max_path_length - len(str(output_dir)) - 1 - len(suffix)
                output_path = output_dir / f"{stem[:max(8, allowed)].strip(' .')}{suffix}"
            used_paths.add(str(output_path).lower())

            split_article(reader, start_page, end_page, output_path)
            results.append(
                {
                    "title": article.get("title"),
                    "authors": article.get("authors"),
                    "start_page": start_page,
                    "end_page": end_page,
                    "file_path": str(output_path.resolve()),
                    "path_length": len(str(output_path.resolve())),
                }
            )
        except Exception as exc:
            failures.append({"title": article.get("title"), "error": str(exc)})

    return {
        "source_pdf": str(pdf_path),
        "output_dir": str(output_dir.resolve()),
        "created": results,
        "failed": failures,
        "created_count": len(results),
        "failed_count": len(failures),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Split a PDF into article PDFs from a manifest.")
    parser.add_argument("manifest", help="analyze_pdf.py 生成或人工复核后的 manifest JSON")
    parser.add_argument("--output-dir", help="拆分 PDF 输出目录；不提供时按 A:\\temppdf / 项目 temppdf / .codex temppdf 规则选择")
    parser.add_argument("--project-dir", help="项目目录；不提供时使用当前工作目录")
    parser.add_argument("--max-path-length", type=int, default=256, help="完整路径最大长度，默认 256")
    parser.add_argument("--result-json", help="拆分结果 JSON 输出路径；不提供时输出到 stdout")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).expanduser().resolve()
    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    project_dir = Path(args.project_dir).expanduser().resolve() if args.project_dir else Path.cwd()
    explicit_output = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
    output_dir = resolve_output_dir(project_dir, explicit_output)

    result = split_from_manifest(manifest_path, output_dir, args.max_path_length)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.result_json:
        result_path = Path(args.result_json).expanduser().resolve()
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(payload, encoding="utf-8")
    else:
        print(payload)

    return 1 if result["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
