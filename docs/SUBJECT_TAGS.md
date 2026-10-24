# Finding sources by subject

Every imported document receives `research` plus relevant subject tags. Examples include `legal-research`, `patent-law`, `double-patenting`, `space-law`, `privacy-law`, `biotechnology-research`, and `semiconductor-research`. Subject tags are independent of collections: a document can have several subjects within one collection.

Tagging runs locally on ingestion, using document titles and repeated text evidence. The current rules cover legal subjects, biotechnology, pharmaceuticals, semiconductors, artificial intelligence, and publishing resources. Unsupported subjects can be supplied by callers and added to the rules later. Automatic tags are retrieval aids, not verified statements about a source's authority, identity, or currency. Scans can receive title-based tags while retaining `needs_ocr` status.

Labels and their rule evidence are stored in SQLite; document responses expose `tags` and `tag_evidence`. Original files and normalized text are not rewritten by tagging. Caller tags survive automatic retagging and remain scoped to their collection. Deduplicated documents retain tags from subsequent imports. Hashes still identify the original source.

From this project directory:

```powershell
.\corpus.ps1 tags --collection legal
.\corpus.ps1 search --collection legal --tag double-patenting
.\corpus.ps1 search "terminal disclaimer" --collection legal --tag patent-law
.\corpus.ps1 search "tag:space-law" --collection legal
.\corpus.ps1 search --collection legal --tag patent-law --include-incomplete
```

For authenticated API callers:

- `GET /v1/tags?collection=legal` lists subjects, total documents, and ready documents.
- `GET /v1/search?collection=legal&tags=patent-law,double-patenting&q=terminal` combines subject filters with text search. All requested tags must match.
- `GET /v1/search?collection=legal&q=tag:space-law` browses matching documents without loading full text. This `tag:` syntax also works through the verified live n8n search webhook.
- Search returns at most 50 records and defaults to ready documents; use `include_incomplete=true` to include partial text and scans. Tag-only results contain document metadata; text searches return cited chunks.
- Supply `"tags": ["Legal Research", "Article Sources"]` on an intake item for custom tags, normalized to `legal-research` and `article-sources`.

`CorpusClient.search(collection, query, limit, {tags, includeIncomplete})` and `CorpusClient.tags(collection)` expose the same functionality to other projects. Public client-record imports accept `research_tags`; private notes and contact data are not mapped.

After changing `corpus/tagging.py`, run `corpus.ps1 retag --collection legal`. This reads existing normalized text and replaces only automatic labels, without re-downloading or re-extracting PDFs. The inventory report includes each document's current tags.
