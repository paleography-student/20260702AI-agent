---
name: pdf-zotero-split-import
description: Split conference proceedings or collected-journal PDFs into individual paper PDFs and import them into Zotero with populated metadata and attachments; import ordinary book PDFs as whole-book Zotero records. Use when the user specifies one or more PDF files and asks to split proceedings/collections into Zotero, import a proceedings PDF, import a collected volume, or add a book PDF to Zotero with metadata.
---

# PDF Zotero Split Import

## Core Workflow

Use this skill when the user provides one or more PDF files and asks to import them into Zotero after splitting or classification.

1. Classify each PDF as `conference_proceedings`, `collected_journal`, or `book`.
   - Prefer explicit user prompt wording.
   - Otherwise inspect the cover, first 5 pages, first 10 pages, and last 2 pages.
   - If classification is low confidence and affects Zotero item type, ask the user before writing.
2. Analyze structure and metadata.
   - Use the Codex PDF plugin workflow for visual/page-aware directory-page and copyright-page analysis.
   - Run `scripts/analyze_pdf.py` to collect text, outlines, likely table-of-contents pages, metadata candidates, and article candidates.
   - If text extraction is sparse, the PDF is image-based, or the directory page is unreliable, use MinerU OCR. Prefer `mineru-open-api flash-extract` for small page ranges; use `mineru-open-api extract` when a token is available and the file/page range exceeds flash limits.
3. Split only proceedings and collected journals.
   - Prefer existing PDF bookmarks/outlines when they map cleanly to article starts.
   - If no usable bookmarks exist, derive article starts from the table of contents.
   - Calculate page offset from the first article's printed TOC page number and its actual PDF page number before splitting.
   - Run `scripts/split_pdf.py <manifest.json>` to create single-paper PDFs.
4. Import into Zotero through Zotero MCP or Zotero's local API, never through the browser Zotero Connector.
   - Preferred path: use `mcp__zotero_mcp` tools when responsive.
   - If the tool wrapper times out but Zotero is otherwise running, call the Zotero MCP Streamable HTTP service directly at `http://127.0.0.1:23121/mcp` with MCP JSON-RPC (`initialize`, `notifications/initialized`, then `tools/call`).
   - Use `http://127.0.0.1:23119/api` only for read-only verification and schema/item inspection; the local API is read-only and cannot create Zotero items.
   - Do not use `/connector/import`, `/connector/saveItems`, browser Connector endpoints, or any workflow that depends on Zotero's currently selected UI collection.
   - First call `get_collections(recursive=true)` or direct MCP `get_collections` to verify Zotero MCP is available and to inspect target collections.
   - Create the metadata item, import the PDF attachment, then add the item to the target collection by collection key.
   - After writing, verify every created item through `23119` local API or Zotero MCP: item type, title, creators, `collections`, and attachment child count.

Read `references/pdf-routing.md` when page routing, TOC parsing, OCR fallback, or offset decisions are non-trivial.
Read `references/zotero-field-map.md` before creating Zotero items.

## Branches

### Conference Proceedings

- Extract shared metadata from page 1 or page 2: conference/proceedings name, meeting date, location, and year.
- Extract per-paper metadata from bookmarks or TOC pages: title, authors, printed start page, and actual PDF start page.
- Default target collection: `会议论文集/<year>`. If the year is missing after inspecting metadata and file name, ask the user.
- Zotero item type: `conferencePaper`.
- Fill shared fields for proceedings title/date/place and per-paper fields for title/authors.

### Collected Journals

- Locate the copyright page from the first 5 pages; if not found, inspect the last 2 pages.
- Extract book/volume title, editors, publisher, publication place, publication date, and ISBN.
- If the PDF lacks enough bibliographic data, use public copyright/CIP/ISBN sources and record the source in the final report.
- Split into individual papers using the same bookmark/TOC workflow as proceedings.
- Default target collection: `集刊出版物`, unless the user specifies another collection.
- Zotero item type: `journalArticle`.

### Books

- Locate the copyright page from the first 5 pages; if not found, inspect the last 2 pages.
- Extract title, authors/editors, publisher, place, date, ISBN, and any edition/series details.
- Do not split the PDF.
- Choose the best existing Zotero collection from the collection tree based on title/topic. If confidence is low, ask the user.
- Zotero item type: `book`.

## Script Usage

Generate a manifest:

```bash
python scripts/analyze_pdf.py "input.pdf" --output "manifest.json"
```

Split article PDFs from a reviewed or generated manifest:

```bash
python scripts/split_pdf.py "manifest.json"
```

The split script writes temporary PDFs to `A:\temppdf` when that folder exists. If it does not exist, it creates `temppdf` under the current project folder; if no project folder is available, it creates `.codex\temppdf` under the user home directory.

Single-paper PDFs must be named as `标题+作者.pdf`. The script sanitizes Windows-illegal characters and truncates names so the full path stays within 256 characters.

## Zotero Write Rules

- User requests such as "拆分后导入 Zotero" or "导入 Zotero" are sufficient confirmation to write Zotero items.
- Do not write if the PDF type, page boundaries, or target collection is materially uncertain; ask first.
- Never continue silently after Zotero MCP failures. Report the exact gate: MCP unavailable, collection missing, item creation failed, attachment import failed, or verification failed.
- Do not use the browser Zotero Connector for this skill. Connector imports depend on Zotero's currently selected collection and can silently write to the wrong place.
- Do not rely on collection names alone. Before writing, resolve and record the exact target collection key and full path with `get_collection_details`.
- When multiple sibling collections share a parent, such as `会议论文集 > 2025` and `会议论文集 > 2026`, verify the year collection key immediately before `add_items_to_collection`. A wrong sibling key is a hard stop; fix it before continuing.
- Add items to collections by explicit collection key only. Never describe this as "selected collection" unless the user explicitly chose a Zotero UI target and the workflow is intentionally UI-driven.
- Before creating each item, search by exact or contained title to avoid duplicate records after a timeout or interrupted run.
- For batch imports, keep an incremental JSON log with title, itemKey, attachmentKey, target collection key, and status. Write the log after each item so the run is resumable.
- If one item is accidentally added to a wrong collection, immediately remove it from the wrong collection and add it to the correct one, then verify via local API before processing more records.
- If `mcp__zotero_mcp` wrapper calls time out, check `http://127.0.0.1:23121/mcp`. If it responds, use direct MCP JSON-RPC rather than falling back to browser Connector.
- Use `http://127.0.0.1:23119/api/users/0/items/<itemKey>` and `/children` as the final verification source when available. Confirm:
  - `itemType` is the expected Zotero type (`conferencePaper`, `journalArticle`, or `book`);
  - the target collection key appears in the item's `collections`;
  - at least one attachment child exists after PDF import.
- Treat MCP summary fields such as attachment `size` as non-authoritative. If an attachment child exists and the local API reports it as an attachment, do not fail solely because a summary size displays as `0`.
- If a public metadata page is blocked by robots.txt or otherwise inaccessible, do not treat it as required evidence. Prefer PDF cover, TOC, copyright page, and page-render verification, and record any inaccessible public source as unverified or secondary.
- Include in the final report: processed PDFs, created items, target collection, successful attachments, skipped/failed records, OCR use, public metadata sources, and records needing manual review.

## Python Script Standard

All Python scripts in this skill must include detailed Chinese comments explaining non-obvious decisions, especially PDF page-number conversion, OCR fallback, filename sanitization, and Zotero manifest fields.
