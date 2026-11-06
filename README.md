# Get Files

**Public preview 0.1.0** — Windows only. Apache-2.0. Published by Brightline IP.

Get Files downloads URLs, PDFs and local files into a **local, hash-verified library**, keeps each original with
its provenance, extracts the text (flagging scanned PDF pages for Windows OCR), tags it, and lets you search it by
keyword. Ask for a source once; later, retrieve only the few page-addressable passages a task needs instead of
re-downloading or pasting whole documents.

It is built for IP practitioners and researchers who collect decisions, statutes, office actions, articles and
other public sources, and who want an AI assistant (through MCP) or a script to fetch and recall them reliably.
All data stays on your computer. See [DISCLAIMER.md](DISCLAIMER.md) — outputs are not legal advice and must be
verified.

## What it does

- Fetch one URL or a batch of up to 100; **download-only**, **corpus** (extract + index) or **both** modes.
- Request an actual PDF (`--pdf`), including an unambiguous PDF link behind a landing page.
- Save each original once under its SHA-256, verify an expected hash when you know it, and record every attempt.
- Extract text from PDF, HTML, Word, Excel, PowerPoint, EPUB and plain text with bounded, resource-limited workers.
- Flag image-only PDF pages and run bounded **Windows OCR** on request; OCR output is marked `needs_review`.
- Tag documents by subject and search them with SQLite FTS5 keyword ranking (not semantic search).
- Expose everything through a **CLI**, a **loopback REST API**, a **Node client** and an **MCP server**.

## Requirements

- Windows 10 or 11 (the OCR engine and DPAPI-protected backups are Windows features; other systems are not supported).
- Python 3.12 and Node.js 24 or newer (with npm).
- PowerShell 5.1 or later, a Windows OCR language pack (English is installed by default) for OCR.

## Install (easiest)

1. Download the zip from the release page and check its SHA-256 against the value in the release notes:
   `Get-FileHash .\get-files-0.1.0.zip -Algorithm SHA256`
2. Right-click the zip, **Extract All**, open the folder and double-click **Setup.cmd**. The script is not code-signed,
   so Windows SmartScreen may warn you; choose More info, Run anyway only if the checksum matched.
3. Setup checks for Python and Node.js and offers to install anything missing through Windows Package Manager. It
   installs nothing without your yes, and it needs no Docker and no API key.

It installs to `%LOCALAPPDATA%\Brightline\GetFiles`, starts the service, and adds **Start Get Files**, **Stop Get Files** and
**Uninstall Get Files** to the Start Menu. To connect an AI assistant, merge the `MCP-setup.json` file Setup writes into your
client's configuration. Running Setup again repairs or updates the installation and keeps your library.
`Setup.cmd -DryRun` shows what it would do without changing anything.

The service does not start with Windows; use **Start Get Files** after a restart (AI assistants start it on demand).

## Install from source

```powershell
git clone <this repository> get-files
cd get-files
.\install.ps1          # creates .venv from requirements.lock, runs npm ci, creates the data folder and a
                       # user-only service token, then prints the MCP registration snippet
```

`install.ps1` needs network access to PyPI and the npm registry. It installs no service or scheduled task. To
remove it, run `.\uninstall.ps1` (your library is kept unless you pass `-RemoveData`).

## Quick start

```powershell
.\get-files.ps1 'https://example.org/some-decision.pdf' --pdf --collection research --tag patent-law
.\get-files.ps1 --jobs JOB_ID --wait 20 --output 'My PDFs'      # copies originals into <export root>\My PDFs
.\corpus.ps1 search 'terminal disclaimer' --collection research --limit 5
.\corpus.ps1 status --collection research
```

The first command starts the local service automatically (`.\start.ps1 -Background` starts it by hand). Results
carry the document hash, page locator, chunk hash, source URL and timestamps. `ready` means text was extracted; it
is not verified source identity, current law, or proof of a proposition. More in
[docs/GET_FILES.md](docs/GET_FILES.md) and [docs/SUBJECT_TAGS.md](docs/SUBJECT_TAGS.md).

## MCP client registration

Add this to your MCP client's configuration (use the exact snippet `install.ps1` prints; paths below are examples):

```json
{
  "mcpServers": {
    "get_files": {
      "command": "C:\\Program Files\\nodejs\\node.exe",
      "args": ["C:\\path\\to\\get-files\\integrations\\mcp\\server.mjs"],
      "env": { "GET_FILES_DATA": "C:\\path\\to\\get-files\\data" }
    }
  }
}
```

Tools: `get_files`, `get_file_jobs`, `search_corpus`, `list_corpus_tags`, `read_source`, `continue_pdf_ocr`,
`complete_pdf_ocr`. Retrieved text is untrusted content; the tools tell the model not to follow instructions in it.

## Configuration

Environment variables (see [.env.example](.env.example); nothing is read from a `.env` file automatically):

| Variable | Default | Purpose |
|---|---|---|
| `GET_FILES_DATA` | `<install dir>\data` | Library, database, service token, logs |
| `GET_FILES_EXPORT_ROOT` | `Documents\Get Files` | Exports (`--output`, `output_dir`) are confined to this folder |
| `GET_FILES_URL` | `http://127.0.0.1:8766` | Service address used by the Node client / MCP server |
| `GET_FILES_TOKEN_FILE` | `<data>\service-token.txt` | Bearer token file |
| `GET_FILES_AUTOSTART` | `true` | `false` stops the client from launching the service |
| `GET_FILES_PYTHON` | `.venv\Scripts\python.exe` | Interpreter for `start.ps1` / `corpus.ps1` |
| `GET_FILES_ALLOW_DOCKER_HOST` | unset | `1` also accepts the `Host: host.docker.internal:<port>` header, so a client in Docker Desktop (for example the Research Verification Workbench container) can call the service. The token is still required |

Command-line options: `start.ps1 -Port -DataPath -Background`; `python -m corpus --data DIR --import-root DIR serve`.
Local-file intake only reads inside import roots (default `intake\` beside the install).

## REST API

Loopback only, `127.0.0.1:8766`. Every `/v1/*` route needs `Authorization: Bearer <service token>`; `/health`
returns only `ok`, `service` and `worker_alive` without a token (process, queue and disk details need the token).
Requests must carry a loopback `Host` header; cross-origin and cross-site browser requests are refused.

| Route | Purpose |
| --- | --- |
| `POST /v1/ingest` | Queue up to 100 items; each gets a job ID or input error |
| `POST /v1/get-files` | Single/batch URLs with download, corpus or both modes, optional PDF requirement |
| `GET /v1/jobs/{id}` | Status, original request and attempt history |
| `GET /v1/jobs/{id}/original` | Download the preserved original, even if extraction failed |
| `POST /v1/jobs/{id}/retry` with `{}` | Requeue a failed/access/resolution job |
| `GET /v1/search?collection=research&q=...&limit=5` | Ranked chunks; collection required |
| `GET /v1/documents/{hash}?collection=...` | Manifest and provenance |
| `GET /v1/documents/{hash}/chunks?collection=...&offset=0&limit=20` | Bounded chunk retrieval |
| `GET /v1/documents/{hash}/original` / `/normalized` | Archived original / normalized text |
| `GET /v1/tags`, `GET /v1/status?collection=...` | Subject tags; queue and library counts |

Repeated requests reuse a deterministic idempotency key; use a new key to intentionally re-check a changing
source. A changed original gets a new document ID and earlier versions stay archived.

## Data locations

Everything is under the data directory (`data\` by default): `corpus.sqlite3`, `files\` (originals),
`documents\` (normalized text), `service-token.txt`, `service-state.json`, logs. Keep the directory together when
you back it up. `scripts\backup-corpus.py` makes a coherent encrypted backup (key held with Windows DPAPI for the
current user). Exports go to the export root only.

## Security model

- Loopback-only API; non-loopback binds are refused. Host, Origin and Sec-Fetch-Site checks block DNS-rebinding and
  cross-site browser requests. The token file is created with current-user-only access (by `install.ps1` or, if you
  skip it, by `start.ps1`). `install.ps1` restricts the data directory itself only when it creates it or it is empty.
- Python packages are version-pinned in `requirements.lock` but not hash-pinned; npm installs use the lockfile with
  `--ignore-scripts`. Review dependencies if your threat model includes a compromised package index.
- Fetches reject private/loopback/link-local destinations (also after redirects), pin connections to vetted
  addresses, refuse URLs with embedded credentials or signature parameters, and never send your token to a site.
- Parsers run in a bounded worker (120 s, 1 GiB, 2,000 PDF pages, 8 M characters). This is a resource limit,
  not a sandbox: treat downloaded files as untrusted.
- No LLM controls downloading or extraction. See [SECURITY.md](SECURITY.md).

## Responsible use

- **Respect the sources.** You are responsible for following each site's terms of service, `robots.txt`, rate limits
  and access rules. The tool honors HTTP 429/Retry-After with backoff, but it does not read `robots.txt` for you
  and it does not bypass logins, paywalls or bot challenges (those become `needs_access`).
- **Copyright stays your responsibility.** Downloaded material remains the property of its owners. Store and use it
  only as the law and your licenses allow, and do not redistribute it from this library without permission.
- **Data stays local.** Nothing is uploaded anywhere by Get Files. If you connect an AI assistant over MCP, the
  passages returned to it are seen by that assistant's provider — consider confidentiality before using
  privileged or client material.

## Limitations

- Windows only. OCR needs the Windows OCR engine; it is bounded per call and its accuracy is not verified.
- Keyword (FTS5) search only. Not a citation verifier or legal-currency service.
- No login, paywall or CAPTCHA handling; title-only citation resolution is not provided.
- Optional n8n/mutual-TLS interface exists as an unsupported extra in [extras/n8n](extras/n8n/README.md) and is off
  by default.
- Clean-machine installation has not been independently verified for this preview.

## Tests

From the repository root (the Python suite needs the `.venv` created by `install.ps1`; all scratch data lives
under the system temp folder and is removed afterwards):

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t .
node --test tests/get-files-client.test.mjs extras/n8n/workflow.test.mjs
```

One timing-dependent 66-page Windows OCR test is skipped unless `GET_FILES_SLOW_OCR_TESTS=1`; the vendored-adapter
comparison is skipped unless `GET_FILES_CONTEXT_FORGE_DIR` points at a Context Forge checkout.

## Uninstall

`.\uninstall.ps1` removes the service process, `.venv` and `integrations\mcp\node_modules`, keeping your library.
Add `-RemoveData` to delete the library as well (irreversible); remove the MCP entry from your client by hand.

## Licence and disclaimer

Apache-2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)). Read the [DISCLAIMER](DISCLAIMER.md) before relying on any output.
