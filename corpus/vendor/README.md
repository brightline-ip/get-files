# Vendored extraction adapter

These files are reviewed snapshots from **Context Forge**, a text-extraction and bounded Windows OCR component
also published by Brightline IP under Apache-2.0 (see the repository NOTICE).

`context_forge_extractors.py` is a reviewed snapshot of the Context Forge extractor; only its InputError import
points to the local equivalent `extraction_errors.py`. The snapshot has no runtime imports from other projects.

`pdf_ocr.py` and `windows_pdf_ocr.ps1` are a separate reviewed snapshot of the Context Forge bounded Windows OCR
adapter (Windows OCR engine, current-user only, no network). The corpus-specific PDF extractor calls that
adapter, not the older vendored `_pdf` function. Per-document OCR metadata records both adapter hashes.

Updates are explicit and reviewed; nothing here is synchronized automatically. A test compares the two OCR
files byte for byte against a Context Forge checkout when `GET_FILES_CONTEXT_FORGE_DIR` points at its
`context_forge` package directory, and is skipped otherwise.
