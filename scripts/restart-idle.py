"""Explicitly approved local restart; refuse active/queued/retry work.

No raw credentials, source data, environment dump, database mutation or replay.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.request import build_opener, ProxyHandler, Request

root=Path(__file__).resolve().parents[1]
shell=str(Path(os.environ.get('SYSTEMROOT',r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe')
data_dir=Path(os.environ.get('GET_FILES_DATA') or root/'data').resolve()
if "'" in str(data_dir):raise RuntimeError('Unsupported character in data directory path')


def health():
    # Queue and process details are returned only to authenticated callers.
    token_file=Path(os.environ.get('GET_FILES_TOKEN_FILE') or data_dir/'service-token.txt')
    request=Request('http://127.0.0.1:8766/health',headers={'Authorization':'Bearer '+token_file.read_text(encoding='utf-8-sig').strip()})
    with build_opener(ProxyHandler({})).open(request,timeout=3) as r:
        return json.load(r)


def main():
    if sys.argv[1:]!=['--approved']:raise RuntimeError('Explicit maintenance approval required')
    before=health()
    if before.get('service')!='Get Files' or before.get('pending_jobs')!=0 or type(before.get('process_id')) is not int:
        raise RuntimeError('Service identity invalid or queue not idle')
    state=json.loads((data_dir/'service-state.json').read_text(encoding='utf-8-sig'))
    if state.get('pid')!=before['process_id']:raise RuntimeError('Service state does not match the running process')
    # Hold a write reservation so no other worker can claim/enqueue between the
    # idle check and stop. No database content is changed by this transaction.
    db=sqlite3.connect(data_dir/'corpus.sqlite3',timeout=5)
    try:
        db.execute('BEGIN IMMEDIATE')
        active=db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','working','retry_wait')").fetchone()[0]
        if active:raise RuntimeError('Queue became active; restart cancelled')
        counts=dict(db.execute('SELECT status,count(*) FROM jobs GROUP BY status'))
        pid=before['process_id']
        script=("$ErrorActionPreference='Stop'; $p=Get-CimInstance Win32_Process -Filter 'ProcessId="+str(pid)+"'; "
            "if (-not $p -or $p.Name -ne 'python.exe' -or $p.CommandLine -notmatch '\"?-m\"?\\s+\"?corpus\"?(\\s|$)' "
            "-or $p.CommandLine -notlike '*"+str(data_dir)+"*') { throw 'Process identity mismatch' }; "
            "Stop-Process -Id "+str(pid)+" -ErrorAction Stop")
        subprocess.run([shell,'-NoProfile','-NonInteractive','-Command',script],check=True,timeout=15,capture_output=True)
    finally:
        db.rollback();db.close()
    # Background launchers must not inherit captured pipes and keep communicate()
    # waiting for the lifetime of the newly started service.
    result=subprocess.run([shell,'-NoProfile','-NonInteractive','-File',str(root/'start.ps1'),'-Background'],timeout=45,
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL)
    if result.returncode:raise RuntimeError('Restart failed; inspect Get Files local service logs')
    after=health()
    if after.get('service')!='Get Files' or not after.get('worker_alive') or after.get('process_id')==before['process_id']:
        raise RuntimeError('Restart health check failed')
    print(json.dumps({'before_pid':before['process_id'],'after':after,'pre_restart_job_counts':counts,'reprocessed_existing_documents':False}))


if __name__=='__main__':
    try:main()
    except Exception as error:
        # Subprocess exceptions may contain commands or captured diagnostics.
        print(json.dumps({'status':'restart_failed_or_refused','error_type':type(error).__name__}));sys.exit(1)
