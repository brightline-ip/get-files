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

## 0.1.0 (reliability) — 2026-11-05

- Extraction time limits count running time, not time the computer was asleep.
- OCR can continue for documents saved before OCR existed.

## 0.1.0 (installer) — 2026-11-06

- `Setup.cmd`: one-click Windows installer with Start Menu shortcuts and a clean uninstaller.
- On-demand start honours a non-default port. Python packages install from wheels only.

## 0.1.0 (pre-release review fixes) — 2026-11-06

- A file briefly locked by antivirus or a sync client no longer quarantines a document, and one unavailable document no longer fails a whole search. Errors now carry the reason.
- A temporary file error during saving retries and resumes, instead of failing the job for good or leaving hidden copies. Orphan working folders are removed at startup.
- Downloads are no longer cut off after 45 seconds: that limit now covers connecting, and the 180-second budget and a 30-second stall limit cover the body.
- OCR works with any English Windows language pack, not only en-US, and a slow batch stops before the worker limit so finished pages are kept.
- Downloaded programs and scripts are saved with a neutral `.bin` name. 6to4 and Teredo addresses are refused. URLs with spaces work.
- Keep-data uninstall keeps the backup key. A service started with no console writes `data\service.log`. Stop recognizes a service started on demand.

## 0.1.0 (fact-check) — 2026-11-06

- Corrected claims in the README and disclaimer, and the advice given when a document fails its integrity check.
- Setup creates the `intake` folder. The download request now identifies itself as `GetFiles/0.1`.
- Setup and the README give commands that run under default Windows script settings, and an exact import command.
