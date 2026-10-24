# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes (public preview) |

## Reporting a vulnerability

Please use GitHub's private security advisory feature on this repository ("Security" tab, "Report a
vulnerability"). Do not open a public issue for a vulnerability. Include the version, your Windows and Python
versions, and the smallest reproduction you can share without sensitive documents.

## What the software trusts

- **The local user.** The REST API binds to loopback only (127.0.0.1 / ::1); non-loopback binds are refused.
  Every `/v1/*` call needs the bearer token in `data/service-token.txt`, which `install.ps1` creates with access
  limited to the current user. Anyone who can read that file can use the service. `start.ps1` also creates the token owner-only if
  `install.ps1` was not run. The unauthenticated `/health` route reports only liveness; the REST API rejects requests whose
  `Host`, `Origin` or `Sec-Fetch-Site` header is not a same-origin loopback request (DNS-rebinding defence).
- **Supply chain.** Installs are version-pinned (`requirements.lock`, `package-lock.json`) but not hash-pinned. NAT64
  address ranges are refused as fetch destinations.
- **The URLs you submit.** Fetches accept only public http(s) URLs: private, loopback and link-local
  destinations (including after redirects) are rejected, connections are pinned to the vetted address, URLs
  with embedded credentials or known signature/token query parameters are refused, and the service token is
  never sent to a source site.
- **Nothing downloaded.** Retrieved files are untrusted. Originals are stored, never executed. Parsers run in a
  bounded worker (time, memory, page and size limits); these limits are not a filesystem or network sandbox.
  Extracted text returned through MCP or the API is untrusted content, never instructions.
- **Local file import** is limited to configured import roots (default `intake/` beside the install) and refuses
  known credential and configuration paths.

## Data statement

All data (originals, extracted text, the SQLite index, the service token, logs) stays in the data directory on
your machine (`data/` by default, or `GET_FILES_DATA`). Get Files makes no outbound request other than the
source fetches you ask for. Encrypted backups use Windows DPAPI (current user) and cannot be recovered without
the same Windows profile.

## Out of scope

The optional mutual-TLS "spoke" in `extras/n8n/` is disabled by default and is an advanced, unsupported extra. Its generated configuration listens on loopback
(`127.0.0.1:8767`) and its private keys are restricted to the current user; widening the listen address is a
deliberate, firewalled choice.
A compromised local user account, a malicious local administrator, and malicious documents that defeat the
parser libraries themselves (report those to the library maintainers) are outside the threat model.
