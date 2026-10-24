"""Explicit offline backup/restore command; never includes service credentials."""
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from corpus.backup import create_backup,restore_backup
from corpus.protected_key import load_key

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('operation',choices=['backup','restore'])
parser.add_argument('archive',type=Path)
parser.add_argument('--data',type=Path,default=ROOT/'data')
parser.add_argument('--destination',type=Path)
parser.add_argument('--key-file',type=Path,default=ROOT/'security'/'backup-key.dpapi')
args=parser.parse_args()
try:
    key=load_key(args.key_file,create=args.operation=='backup')
    if args.operation=='backup':result=create_backup(args.data,args.archive,key)
    else:
        if args.destination is None:raise ValueError('A new restore destination is required')
        result=restore_backup(args.archive,args.destination,key)
    print(json.dumps(result))
except Exception as exc:
    print('Backup operation failed ('+type(exc).__name__+'); existing archives and credentials were not replaced.',file=sys.stderr)
    raise SystemExit(1)
