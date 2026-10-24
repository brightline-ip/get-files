"""Explicit offline rotation invalidates old copies without disclosing either token."""
import base64
import os
import secrets
from pathlib import Path

from .processing import WorkerLock


def rotate_local_token(data):
    data=Path(data).resolve();target=data/'service-token.txt'
    if not target.is_file() or target.is_symlink():raise ValueError('Existing local token file is required')
    with WorkerLock(data):
        pending=data/('token-pending-'+secrets.token_hex(8))
        try:
            with pending.open('xb') as stream:
                stream.write(base64.b64encode(secrets.token_bytes(48)));stream.flush();os.fsync(stream.fileno())
            os.replace(pending,target)
        finally:pending.unlink(missing_ok=True)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data',type=Path,default=Path(__file__).resolve().parents[1]/'data');parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if args.apply:
        try:rotate_local_token(args.data)
        except Exception as exc:print('Token rotation failed ('+type(exc).__name__+'); value was not displayed.');raise SystemExit(1)
        print('Local token rotated while the worker was stopped; old copies are invalid.')
    else:print('Preview: rotate only the local corpus token after stopping the worker. Requires explicit rotation approval.')
