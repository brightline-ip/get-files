from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .processing import PROJECT_ROOT, Processor
from .storage import Library, encoded


def main():
    parser = argparse.ArgumentParser(description="Shared local source corpus")
    parser.add_argument("--data",type=Path,default=Path(os.environ.get("GET_FILES_DATA") or PROJECT_ROOT/"data"))
    parser.add_argument("--import-root",type=Path,action='append',default=None)
    sub = parser.add_subparsers(dest="command",required=True)
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--collection",required=True)
    source = enqueue.add_mutually_exclusive_group(required=True)
    source.add_argument("--path")
    source.add_argument("--url")
    enqueue.add_argument("--title")
    enqueue.add_argument("--source-system",default="manual")
    enqueue.add_argument("--expected-format")
    enqueue.add_argument("--tag",action="append",default=[])
    ingest = sub.add_parser("ingest")
    ingest.add_argument("manifest",type=Path)
    search = sub.add_parser("search")
    search.add_argument("query",nargs="?",default="")
    search.add_argument("--tag",action="append",default=[])
    search.add_argument("--collection",required=True)
    search.add_argument("--limit",type=int,default=5)
    search.add_argument("--include-incomplete",action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--collection")
    tags = sub.add_parser("tags")
    tags.add_argument("--collection",required=True)
    retag = sub.add_parser("retag")
    retag.add_argument("--collection",required=True)
    job = sub.add_parser("job")
    job.add_argument("id")
    sub.add_parser("work")
    serve = sub.add_parser("serve")
    serve.add_argument("--host",default="127.0.0.1")
    serve.add_argument("--port",type=int,default=8766)
    serve.add_argument("--token-file",type=Path)
    serve.add_argument('--spoke-config',type=Path)
    args = parser.parse_args()
    library = Library(args.data)
    if args.command == "enqueue":
        item = {k:v for k,v in {"path":args.path,"url":args.url,"title":args.title,"source_system":args.source_system,"expected_format":args.expected_format}.items() if v is not None}
        item["tags"] = args.tag
        print(encoded(library.enqueue(args.collection,item)))
    elif args.command == "ingest":
        body = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
        print(encoded([{"job_id":j["id"],"status":j["status"]} for item in body["items"] if (j:=library.enqueue(body["collection"],item))]))
    elif args.command == "search":
        print(encoded(library.search(args.collection,args.query,args.limit,args.include_incomplete,args.tag)))
    elif args.command == "tags":
        print(encoded(library.tag_inventory(args.collection)))
    elif args.command == "retag":
        with library.connect() as db:
            ids = [r[0] for r in db.execute("SELECT DISTINCT document_id FROM memberships WHERE collection=?", (args.collection,))]
        for doc_id in ids:
            library.retag(args.collection,doc_id)
        print(encoded({"documents":len(ids),"tags":library.tag_inventory(args.collection)}))
    elif args.command == "status":
        print(encoded(library.summary(args.collection)))
    elif args.command == "job":
        print(encoded(library.job(args.id)))
    elif args.command == "work":
        Processor(library,allowed_roots=args.import_root).drain()
    elif args.command == "serve":
        from .server import serve
        config=json.loads(args.spoke_config.read_text(encoding='utf-8-sig')) if args.spoke_config else None
        serve(library,(args.token_file or args.data/'service-token.txt').read_text().strip(),args.host,args.port,processor=Processor(library,allowed_roots=args.import_root),spoke_config=config)


if __name__ == "__main__":
    main()
