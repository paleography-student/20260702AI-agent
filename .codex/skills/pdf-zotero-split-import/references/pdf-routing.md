# PDF Routing And Page Boundary Rules

Use this reference when PDF type, directory pages, OCR fallback, or page offsets are uncertain.

## Type Routing Priority

1. User prompt:
   - Mentions `会议论文集`, `会议`, `proceedings`, `conference`: route to `conference_proceedings`.
   - Mentions `集刊`, `辑刊`, `论文集刊`, `collected journal`: route to `collected_journal`.
   - Mentions `书籍`, `图书`, `book`, or asks to import the whole PDF: route to `book`.
2. PDF evidence:
   - Proceedings: conference name, meeting date/place, "proceedings", "论文集", "研讨会".
   - Collected journal: title resembles a serial collected volume, copyright page, editor list, publisher, ISBN, TOC of independent articles.
   - Book: copyright page plus continuous chapters rather than independent article-author pairs.
3. If two routes remain plausible and would change Zotero item type or split behavior, ask the user.

## TOC Detection

- Inspect PDF outlines/bookmarks first. A usable outline has multiple entries with distinct target pages and article-like titles.
- If no usable outline exists, inspect the first 10 PDF pages for `目录`, `目次`, `Contents`, dotted leaders, and lines ending in printed page numbers.
- For collected journals/books, also inspect the first 5 pages and last 2 pages for copyright data.
- Use Codex PDF plugin rendering/inspection when layout matters or text extraction scrambles columns.
- Use MinerU OCR when pages are scanned, image-based, sparse in extracted text, or multi-column TOC parsing fails.

## Page Offset Calculation

PDF page numbers are 1-based for user-facing manifests, while Python PDF libraries use 0-based indices internally.

1. Extract the first article's printed page number from the TOC.
2. Locate the first article's actual PDF page by searching the article title in page text or by visual inspection.
3. Calculate `offset = actual_pdf_page - printed_toc_page`.
4. For each article, set `actual_pdf_start = printed_toc_page + offset`.
5. Set each article's end page to the page before the next article's actual start. The final article ends at the last page or the last article boundary verified by the TOC/bookmark structure.

If the first article title cannot be located reliably, ask the user to confirm the first article's actual PDF page before splitting.

## OCR Fallback

- Use `mineru-open-api flash-extract <pdf> --pages 1-10 --language ch` for small front matter and TOC page ranges.
- Use `mineru-open-api extract <pdf> -f md,json --pages <range> --model pipeline --ocr --language ch -o <dir>` when a token is configured and more reliable OCR/layout output is needed.
- Record MinerU use and page ranges in the final report because document content may be sent to MinerU's service.

## Filename And Temp Directory Rules

- Split PDF filename format: `标题+作者.pdf`.
- Remove Windows-illegal characters: `< > : " / \ | ? *`, ASCII control characters, trailing spaces, and trailing dots.
- Avoid reserved device names such as `CON`, `PRN`, `AUX`, `NUL`, `COM1`, and `LPT1`.
- Keep the full path at or below 256 characters. If needed, truncate the title, keep the first author, and append an 8-character hash.
- Output directory priority:
  1. Existing `A:\temppdf`
  2. `<project>\temppdf`
  3. `<home>\.codex\temppdf`
