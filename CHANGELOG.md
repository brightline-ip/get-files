# Changelog

## 0.1.0 — 2026-10-23

First public preview.

- Hash-verified download of URLs, PDFs and local files into a local library with provenance; download-only,
  corpus or both modes.
- Text extraction (PDF, HTML, Office, EPUB, text) with bounded workers; Windows OCR flagging and continuation
  for scanned PDF pages.
- Subject tags and FTS5 keyword search over page-addressable chunks.
- Loopback REST API, CLI, Node client and MCP server (`get_files`, `get_file_jobs`, `search_corpus`,
  `list_corpus_tags`, `read_source`, `continue_pdf_ocr`, `complete_pdf_ocr`).
- `install.ps1` / `uninstall.ps1`; configurable data directory (`GET_FILES_DATA`) and export root
  (`GET_FILES_EXPORT_ROOT`).
- Optional n8n workflow generator and mutual-TLS spoke, disabled by default (`extras/n8n/`).

## 0.1.0 (security review fixes) — 2026-10-24

- `install.ps1` no longer rewrites the ACL of an existing non-empty data directory (warns and skips).
- Removed an unused internal helper; neutral default collection names.
- REST API: Host/Origin/Sec-Fetch-Site checks; unauthenticated `/health` trimmed to liveness.
- SSRF filter rejects NAT64 ranges. `start.ps1` token created owner-only. mTLS spoke defaults to loopback and its
  private keys are user-only. Node client uses the absolute Windows PowerShell path. Pip installs documented as
  version-pinned, not hash-pinned.
