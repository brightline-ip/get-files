# Optional: n8n integration (advanced, disabled by default)

Get Files runs completely without n8n. This folder holds an optional workflow generator for people who want an
n8n instance to submit and search through a **restricted mutual-TLS interface** instead of the master API.

## What is here

- `n8n-workflow.mjs`, `n8n-restricted-workflow.mjs` — generate the twelve-node n8n workflow definition.
- `workflow.json` — the generated workflow, for import into n8n. `workflow.test.mjs` checks the two match
  (`node --test extras/n8n/workflow.test.mjs`).
- `compose.override.example.yaml` — example Docker Compose override that allowlists `host.docker.internal` for
  n8n's SSRF protection. Adapt it to your own compose file.

The server side lives in `corpus/spoke.py`, `corpus/access.py` and `corpus/tls_material.py`. It is imported only
when a spoke configuration is supplied, so by default none of it runs and nothing listens on a second port.

## Enabling it (at your own risk)

1. Generate client and server identities into an empty directory:
   `.venv\Scripts\python.exe -m corpus.tls_material <empty-directory>` (see the module for options).
2. Copy the resulting `spoke.json` to `security\spoke.json`, or pass `start.ps1 -SpokeConfig <file>`.
   `start.ps1` loads `security\spoke.json` automatically when it exists; delete it to disable the spoke.
   The generated `spoke.json` listens on `127.0.0.1:8767`. A containerised n8n reaches the host through
   `host.docker.internal`, which needs a non-loopback address: edit `host` in `spoke.json` deliberately (for
   example to your Docker host-gateway address, never a public interface) and firewall the port. The generated
   private keys (`*-key.pem`) are restricted to the current user; keep them that way.
3. In n8n, create an HTTP SSL credential from the generated client certificate and a header-auth credential for
   the webhooks, then import `workflow.json` and wire the credentials. The spoke issues certificate-bound,
   single-use grants (60 seconds) and restricts each client certificate to explicitly permitted collections and
   operations. Never give n8n the master service token.

This extra is unsupported in the 0.1.0 preview: the installer and live-verification scripts that were used
internally depend on a private n8n deployment and are not included.
