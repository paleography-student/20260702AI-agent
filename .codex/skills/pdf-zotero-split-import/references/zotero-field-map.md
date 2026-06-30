# Zotero Field Map

Use this reference before creating Zotero items through `mcp__zotero_mcp.write_item`.

## Common Write Sequence

1. Call `get_collections(recursive=true)` and choose the collection key.
2. Create the parent metadata item with `write_item(action="create")`.
3. Import the PDF attachment with `write_item(action="import", parentItemKey=<itemKey>, filePath=<pdf>)`.
4. Add the created item to the collection with `add_items_to_collection`.
5. Verify with `get_item_details(mode="standard")`.

## Conference Proceedings

- `itemType`: `conferencePaper`
- Per-paper fields:
  - `title`: paper title
  - `creators`: paper authors as `creatorType="author"`
  - `pages`: page range when known
- Shared fields:
  - `proceedingsTitle` or `bookTitle`: proceedings/conference volume title
  - `date`: conference year or full date
  - `place`: conference location
  - `conferenceName`: conference name when supported; otherwise also record it in `extra`
- Default collection: `会议论文集/<year>`

## Collected Journals

- `itemType`: `journalArticle`
- Per-paper fields:
  - `title`: article title
  - `creators`: article authors as `creatorType="author"`
  - `pages`: page range when known
- Shared fields:
  - `publicationTitle`: collected volume/book title
  - `publisher`: publisher
  - `place`: publication place
  - `date`: publication date
  - `ISBN`: ISBN when available
  - `creators`: include volume editors as `creatorType="editor"` only if Zotero accepts them for the item type; otherwise write editors to `extra`
- Default collection: `集刊出版物`

## Books

- `itemType`: `book`
- Fields:
  - `title`: book title
  - `creators`: authors as `creatorType="author"` or editors as `creatorType="editor"`
  - `publisher`: publisher
  - `place`: publication place
  - `date`: publication date
  - `ISBN`: ISBN
  - `series`, `edition`, `numPages`: fill when reliably available
- Collection: choose the best existing collection by title/topic. Ask the user if confidence is low.

## Creator Parsing

- Prefer explicit author/editor separators from the PDF: `,`, `;`, `、`, `，`, `；`, line breaks, or clear spacing.
- For Chinese names without reliable separators, do not over-split. Keep the raw string as a single `name` creator or ask the user if accuracy matters.
- For organizations, use `{ "creatorType": "...", "name": "..." }`.

## Extra Field

Use `extra` for fields that Zotero MCP or Zotero item type may not directly accept:

```text
会议名称: ...
会议地点: ...
会议时间: ...
集刊编者: ...
元数据来源: ...
OCR: MinerU
```
