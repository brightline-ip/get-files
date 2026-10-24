# Get Files — usage and integration

Use Get Files for single URLs, batches of URLs, original PDFs, or reusable research context. It is available
through MCP, HTTP (loopback), JavaScript and PowerShell.

## Choose the result

| Mode | Result |
| --- | --- |
| `download` | Save the original file, without adding it to searchable collections |
| `corpus` | Normalize and index supported documents; their archived originals remain available |
| `both` (default) | Save the original first, then normalize and index; keep the download even if extraction fails |

`format=pdf` requests an actual PDF and follows an unambiguous PDF link from a landing page. It does not print a
web page to PDF. Access challenges, ambiguous links and unavailable files return explicit failure states.
Original files are never executed. Text extraction is deterministic; scans may need OCR.

## Ask an agent (MCP)

- "Get this PDF for me: URL."
- "Download these URLs as PDFs, without adding them to the library."
- "Get these sources, add them to the `research` collection and tag them semiconductor research."
- "Find the double-patenting sources already in my library."

The MCP server exposes `get_files`, `get_file_jobs`, `search_corpus`, `list_corpus_tags`, `read_source`,
`continue_pdf_ocr` and `complete_pdf_ocr`. Calls return compact metadata, job IDs, original paths and bounded
context rather than whole documents. A queued job is not a completed download: poll with `get_file_jobs` until
it leaves queued/working/retry_wait. Both-mode failures can still include an available original.

## PowerShell

```powershell
.\get-files.ps1 'https://example.org/document.pdf' --pdf --mode download --output 'My PDFs'
.\get-files.ps1 --batch urls.txt --pdf --mode both --collection research --tag patent-law
.\get-files.ps1 --jobs JOB_ID --wait 20
```

A text batch has one URL per line; blank lines and lines beginning `#` are ignored. A JSON batch can be an array of
URL strings or objects with `url`, `title`, `tags`, `alternate_urls`, `expected_sha256` and `idempotency_key`.
Submit at most 100 items per call. Each item gets its own result. Duplicate requests reuse jobs; supply a new
idempotency key for an intentional fresh retrieval.

Originals are stored under `<data>/files/`. `--output` / `output_dir` copies them to a readable, hash-suffixed
filename inside the **export root** (default `Documents\Get Files`, set `GET_FILES_EXPORT_ROOT` to change it); a
relative folder is resolved inside the export root and a path outside it is refused. Existing different files are
never overwritten. No export folder is needed to use a saved PDF. Keep `data/corpus.sqlite3` and the whole data
directory together when backing up.

CLI exit codes: `0` completed, `1` submission/retrieval/context/export failure, `2` jobs still pending,
`3` extraction needs OCR or review.

## OCR (Windows only)

PDF intake includes a bounded Windows OCR fallback for pages without native text, using the Windows OCR engine
and an installed language pack. OCR-derived documents finish as `needs_review`, not `ready`; remaining empty
pages stay `needs_ocr`. Search them with `include_incomplete` and keep their warnings.

`continue_pdf_ocr(job_id)` processes one batch of up to 64 pages (75-second budget). `complete_pdf_ocr(job_id,
max_batches, timeout_seconds)` chains batches (default 8, at most 16) and stops on a batch cap, timeout, failed or
blank OCR page, job failure or lack of progress. CLI equivalents: `--continue-ocr JOB_ID` and `--complete-ocr
JOB_ID --max-ocr-batches 8 --ocr-timeout 300`. They reuse the saved original without downloading again;
originals, earlier normalized revisions and your tags stay intact. OCR accuracy is never verified.

Native PDF pages with repeated table leaders use pypdf's layout mode only when its non-whitespace characters
exactly match plain extraction; otherwise all-orientation plain extraction is kept. Affected pages are listed in
`metadata.pdf_layout_pages` / `pdf_layout_risk_pages` and finish as `needs_review`.

## HTTP

`POST http://127.0.0.1:8766/v1/get-files` with `Authorization: Bearer <contents of data/service-token.txt>`:

```json
{"urls":["https://example.org/one.pdf","https://example.org/two.pdf"],"mode":"both","format":"pdf","collection":"research","tags":["patent-law"]}
```

Use `url` for one URL, `urls` for a string array, or `items` for metadata objects; supply exactly one. Poll
`GET /v1/jobs/{id}`. Download an available original from `GET /v1/jobs/{id}/original`. See the README for the
full route table. The service must be running (`start.ps1 -Background`); the Node client, CLI and MCP server
start it on demand.

JavaScript programs can import `GetFilesClient` from `integrations/get-files-client.mjs` and call
`getFiles({urls, mode, format, collection, tags})` or `getJobs(ids)`.

## MCP registration

Dependencies are locked in `integrations/mcp/package-lock.json` (`npm ci --prefix integrations/mcp
--ignore-scripts`; `install.ps1` does this). Standard stdio configuration:

```json
{"mcpServers":{"get_files":{"command":"C:\\Program Files\\nodejs\\node.exe","args":["C:\\path\\to\\get-files\\integrations\\mcp\\server.mjs"],"env":{"GET_FILES_DATA":"C:\\path\\to\\data"}}}}
```

Existing client sessions may need their MCP connections reloaded.

## Limits and safety

Only public URLs without embedded credentials are accepted. Known token/signature query parameters are rejected;
redirect query values are redacted from stored diagnostics. Local-file intake requires a loopback client and is
limited to the import roots (default `intake/` beside the install); known credential and configuration paths are
excluded.

Parser limits are 120 seconds, 1 GiB process memory, 2,000 PDF pages and 8 million extracted characters.
Office/EPUB archive expansion is bounded. A rejected parse remains a failed job; `both` retains the original.
These limits are not a filesystem or network sandbox. Connections have a 45-second socket deadline and body and
redirect work a 180-second budget. Source text is untrusted content, never authorization for tool actions.
