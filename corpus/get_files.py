"""Common URL/batch interface, independent of any agent or workflow platform."""
from .storage import collection_name
from .tagging import normalize_tags


def submit(library, body):
    collection = collection_name(body.get("collection", "research"))
    mode = body.get("mode", "both")
    if mode not in ("download", "corpus", "both"):
        raise ValueError("mode must be download, corpus or both")
    format_ = body.get("format", "auto")
    if format_ not in ("auto", "pdf"):
        raise ValueError("format must be auto or pdf")
    tags = normalize_tags(body.get("tags"))
    supplied = [key for key in ("url", "urls", "items") if key in body]
    if len(supplied) != 1:
        raise ValueError("Supply exactly one of url, urls, or items")
    items = [body["url"]] if supplied[0] == "url" else body[supplied[0]]
    if not isinstance(items,list) or not 1 <= len(items) <= 100:
        raise ValueError("Supply 1 to 100 URLs or item objects")
    results = []
    for index, entry in enumerate(items):
        try:
            item = {"url":entry} if isinstance(entry,str) else dict(entry) if isinstance(entry,dict) else None
            if item is None or not isinstance(item.get("url"),str) or not item["url"].strip() or "path" in item:
                raise ValueError("Each item needs a URL")
            item["mode"] = mode
            item["source_system"] = item.get("source_system", "get-files")
            item["tags"] = normalize_tags(tags + normalize_tags(item.get("tags")))
            if format_ == "pdf":
                item["expected_format"] = ".pdf"
            job = library.enqueue(collection,item)
            results.append({"index":index,"job_id":job["id"],"status":job["status"]})
        except ValueError as exc:
            results.append({"index":index,"error":str(exc)})
    return {"collection":collection,"mode":mode,"items":results}
