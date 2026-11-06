"""Bounded, local Windows OCR fallback. Canonical source for the corpus snapshot.

No model, shell command string, cloud service, source overwrite or PDF rewrite.
OCR text is a fallible derivative, not a claim of lossless image recognition.

Pinned configuration (see docs/PDF_OCR.md, "Pinned configuration"): every run
records OCR_CONFIG, the adapter and helper hashes, the Windows build that supplied
the recognizer, and a config_sha256 over all of them. A page's result is only
reused from a checkpoint written under the same config_sha256. Every page of the
document gets an explicit status record; nothing is left implicit or partial.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager

MAX_OCR_PAGES = 8               # pages per helper process (the helper enforces 1-8)
MAX_PAGES_PER_CALL = 64         # new pages attempted per extraction call
TIME_BUDGET_SECONDS = 75.0      # no new helper batch starts after this; worker deadline is 120 s
HELPER_DEADLINE_SECONDS = 105.0 # a running batch stops here, so the worker returns what it has instead of being killed
_ocr_started = None
RENDER_DPI = 200                # fixed render scale; 300 dpi measured no gain for this engine
MAX_RENDER_PIXELS = 2600        # longest rendered side; the helper rejects larger output
OCR_VERSION = 'windows-ocr-empty-pages-v2'
CHECKPOINT_VERSION = 'windows-ocr-checkpoint-v2'
HELPER = Path(__file__).with_name('windows_pdf_ocr.ps1')
OCR_CONFIG = {'engine': 'Windows.Media.Ocr', 'language': 'en-US', 'render_dpi': RENDER_DPI,
              'max_render_pixels': MAX_RENDER_PIXELS, 'pages_per_helper_call': MAX_OCR_PAGES,
              'max_pages_per_call': MAX_PAGES_PER_CALL, 'time_budget_seconds': TIME_BUDGET_SECONDS,
              'policy': 'empty-native-text-pages-only', 'version': OCR_VERSION}
PAGE_STATUSES = ('native', 'recognized', 'blank', 'failed', 'pending')


class OcrCheckpointPathError(ValueError):
    """Checkpoint path cannot be used (too long for SQLite's journal, or unsafe)."""


def ocr_config():
    """Deterministic description of the run configuration, hashed for provenance."""
    config = dict(OCR_CONFIG)
    config['adapter_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    config['helper_sha256'] = hashlib.sha256(HELPER.read_bytes()).hexdigest()
    # The Windows recognizer has no version API; the OS build is the closest pin.
    config['engine_build'] = platform.version() if os.name == 'nt' else 'unavailable'
    config['config_sha256'] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return config


def _windows_job(process):
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong), ('flags', w.DWORD),
                    ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t), ('active', w.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]; kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended(); limits.basic.flags = 0x100 | 0x2000; limits.process_memory = 512 * 1024**2
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, int(process._handle)):
        if handle: kernel.CloseHandle(handle)
        raise RuntimeError('OCR resource limits unavailable')
    return lambda: kernel.CloseHandle(handle)


def _valid_rows(rows, pages):
    if not isinstance(rows, list) or len(rows) != len(pages) or any(not isinstance(r, dict) for r in rows):
        return False
    if any(type(r.get('page')) is not int for r in rows) or {r['page'] for r in rows} != set(pages):
        return False
    return not any(r.get('status') not in {'recognized', 'failed'} or not isinstance(r.get('text'), str)
                   or len(r['text']) > 200000 for r in rows)


def helper_timeout(now=time.monotonic):
    if _ocr_started is None:
        return 60
    return max(5, min(60, HELPER_DEADLINE_SECONDS - (now() - _ocr_started)))


def recognize_pages(source, pages):
    """Only called inside the existing bounded extraction worker."""
    if os.name != 'nt':
        raise RuntimeError('Local OCR backend unavailable on this operating system')
    if not pages or len(pages) > MAX_OCR_PAGES or any(type(n) is not int or n < 1 or n > 2000 for n in pages):
        raise ValueError('Invalid OCR page selection')
    source = Path(source).resolve(strict=True)
    parent = source.parent
    scratch = parent / ('ocr-' + uuid.uuid4().hex)
    scratch.mkdir()  # Inherit workspace ACLs; no inaccessible Windows 0700 temp dir.
    process = None; close_job = lambda: None
    try:
        output = scratch / 'result.json'
        executable = Path(os.environ.get('SYSTEMROOT', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        environment = {k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','TEMP','TMP','LANG'}}
        process = subprocess.Popen([str(executable), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(HELPER),
            '-InputPdf', str(source), '-OutputJson', str(output), '-PagesCsv', ','.join(map(str, pages))],
            env=environment, cwd=scratch, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        close_job = _windows_job(process)
        process.communicate(b'go\n', timeout=helper_timeout())
        if process.returncode or not output.is_file() or output.stat().st_size > 12 * 1024**2:
            raise RuntimeError('OCR did not return a bounded result')
        payload = json.loads(output.read_text(encoding='utf-8'))
        if payload.get('engine') != 'Windows.Media.Ocr' or not str(payload.get('language', '')).lower().startswith('en'):
            raise ValueError('Unexpected OCR engine or language')
        if payload.get('render_dpi') != RENDER_DPI or payload.get('max_render_pixels') != MAX_RENDER_PIXELS:
            raise ValueError('OCR helper render configuration does not match the pinned configuration')
        rows = payload.get('pages')
        if not _valid_rows(rows, pages):
            raise ValueError('Invalid OCR response')
        for row in rows:
            row['render'] = {'dpi': RENDER_DPI, 'width': row.pop('width', None), 'height': row.pop('height', None)}
        return rows
    finally:
        if process is not None:
            if process.poll() is None: process.kill()
            process.communicate()
        close_job()
        if scratch.exists():
            if scratch.resolve().parent != parent or scratch.is_symlink():
                raise RuntimeError('Refusing OCR scratch cleanup outside its source directory')
            shutil.rmtree(scratch)


@contextmanager
def _checkpoint(source, extraction, checkpoint, config):
    """Trusted service-selected path, never a path taken from document contents.

    SQLite owns the interprocess lock and rolls back an interrupted batch. Earlier
    committed batches survive parser termination. Original bytes bind every row.
    A checkpoint written under another config_sha256 (adapter, helper or Windows
    build changed) is discarded and rebuilt; a changed source or malformed
    checkpoint fails closed.
    """
    if checkpoint is None:
        yield None, {}
        return
    checkpoint = Path(checkpoint)
    if checkpoint.is_symlink() or checkpoint.parent.is_symlink():
        raise OcrCheckpointPathError('Unsafe OCR checkpoint path')
    if os.name == 'nt' and len(str(checkpoint)) + len('-journal') > 259 and not str(checkpoint).startswith('\\\\?\\'):
        raise OcrCheckpointPathError('OCR checkpoint path exceeds the Windows path limit; use a shorter data directory')
    digest = hashlib.sha256()
    with Path(source).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    identity = {'source_sha256': digest.hexdigest(), 'version': CHECKPOINT_VERSION,
                'config_sha256': config['config_sha256'],
                'native': hashlib.sha256(json.dumps([(s.locator, s.text) for s in extraction.sections],
                    ensure_ascii=False).encode()).hexdigest()}
    if checkpoint.exists() and checkpoint.stat().st_size > 64 * 1024**2:
        raise ValueError('OCR checkpoint exceeds size limit')
    db = sqlite3.connect(checkpoint, timeout=1)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS progress (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        saved = db.execute('SELECT payload FROM progress WHERE id=1').fetchone()
        state = json.loads(saved[0]) if saved else {'identity': identity, 'rows': []}
        if not isinstance(state, dict) or not isinstance(state.get('identity'), dict):
            raise ValueError('Invalid OCR checkpoint')
        found = state['identity']
        bound = ('source_sha256', 'version', 'native')
        if {k: found.get(k) for k in bound} != {k: identity[k] for k in bound}:
            raise ValueError('OCR checkpoint identity mismatch')
        if found.get('config_sha256') != identity['config_sha256']:
            state = {'identity': identity, 'rows': [], 'reset_from_config_sha256': found.get('config_sha256')}
        selected = {n for n,s in enumerate(extraction.sections, 1) if not s.text.strip()}
        rows = state.get('rows')
        if not isinstance(rows, list) or len(rows) > len(selected):
            raise ValueError('Invalid OCR checkpoint')
        if any(not isinstance(r, dict) or type(r.get('page')) is not int or r['page'] not in selected
               or r.get('status') not in {'recognized','failed'} or not isinstance(r.get('text'), str)
               or len(r['text']) > 200000 for r in rows):
            raise ValueError('Invalid OCR checkpoint rows')
        if len({r['page'] for r in rows}) != len(rows) or sum(len(r['text']) for r in rows) > 8_000_000:
            raise ValueError('Invalid OCR checkpoint coverage')
        yield db, state
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        if len(payload.encode()) > 48 * 1024**2:
            raise ValueError('OCR checkpoint exceeds size limit')
        db.execute('INSERT OR REPLACE INTO progress VALUES(1,?)', (payload,))
        db.commit()
    finally:
        db.close()


def apply_pdf_ocr(source, extraction, backend=None, checkpoint=None):
    config = ocr_config()
    with _checkpoint(source, extraction, checkpoint, config) as (_, state):
        return _apply_pdf_ocr(source, extraction, backend, state, config)


def _text_shape(text):
    """Cheap, deterministic shape metrics. Not a recognition confidence: the Windows
    engine exposes none, so a page can look well-formed and still be wrong."""
    tokens = re.findall(r'\S+', text)
    alpha = sum(1 for t in tokens if re.fullmatch(r"[A-Za-z][A-Za-z'\-]*[.,;:)]?", t))
    return {'words': len(tokens), 'alpha_word_ratio': round(alpha / len(tokens), 3) if tokens else None}


def _page_record(number, status, text, row=None):
    record = {'page': number, 'status': status, 'chars': len(text), 'confidence': None, 'confidence_available': False}
    if status in ('recognized', 'blank'):
        record['text_sha256'] = hashlib.sha256(text.encode('utf-8')).hexdigest()
        record['shape'] = _text_shape(text)
    if row and isinstance(row.get('render'), dict):
        record['render'] = row['render']
    return record


def _apply_pdf_ocr(source, extraction, backend, state, config):
    """Fill only empty page text; always retain a visible OCR review warning."""
    selected = [(n, s) for n,s in enumerate(extraction.sections, 1) if not s.text.strip()]
    selected_numbers = {n for n,_ in selected}
    metadata = {'version': OCR_VERSION, 'engine': 'Windows.Media.Ocr', 'language': 'en-US',
                'policy': 'empty-native-text-pages-only', 'config': config, 'config_sha256': config['config_sha256'],
                'requested_pages': [n for n,_ in selected],
                'attempted_pages': [], 'recognized_pages': [], 'blank_pages': [], 'failed_pages': [], 'pending_pages': [],
                'unresolved_pages': [], 'current_batch_pages': [], 'status': 'not_needed',
                'recognition_verified': False, 'native_text_preserved': True, 'continuation_available': False,
                'adapter_sha256': config['adapter_sha256'], 'helper_sha256': config['helper_sha256'],
                'pages': [_page_record(n, 'native', s.text) for n, s in enumerate(extraction.sections, 1) if n not in selected_numbers]}
    extraction.metadata['ocr'] = metadata
    if not selected:
        metadata['summary'] = 'OCR not needed: every page has native text.'
        return extraction
    saved = {r['page']:r for r in state.get('rows', [])}
    if state.get('reset_from_config_sha256'):
        extraction.warnings.append('OCR checkpoint from an earlier configuration was discarded; pages are re-recognized under the pinned configuration.')
    todo = [n for n,_ in selected if n not in saved][:MAX_PAGES_PER_CALL]
    current = list(todo)
    metadata['status'] = 'incomplete'
    backend = backend or recognize_pages
    global _ocr_started
    started = _ocr_started = time.monotonic()
    attempted_now = []
    try:
        while todo and (not attempted_now or time.monotonic() - started < TIME_BUDGET_SECONDS):
            chunk, todo = todo[:MAX_OCR_PAGES], todo[MAX_OCR_PAGES:]
            rows = backend(source, chunk)
            # Validate injected and native backend responses before changing any section.
            if not _valid_rows(rows, chunk):
                raise ValueError('Invalid OCR result')
            if sum(len(r['text']) for r in [*saved.values(), *rows]) + sum(len(s.text) for s in extraction.sections) > 8_000_000:
                raise ValueError('OCR text exceeds document limit')
            saved.update({r['page']: {'page': r['page'], 'status': r['status'], 'text': r['text'],
                                      **({'render': r['render']} if isinstance(r.get('render'), dict) else {})} for r in rows})
            attempted_now.extend(chunk)
    except Exception:
        failed_chunk = [n for n in current if n not in attempted_now and n not in todo]
        saved.update({n: {'page': n, 'status': 'failed', 'text': ''} for n in failed_chunk})
        attempted_now.extend(failed_chunk)
        metadata['status'] = 'unavailable_or_failed'
        extraction.warnings.append('Local PDF OCR was unavailable, timed out, or failed; native text and original preserved. No source details included in diagnostics.')
    metadata['current_batch_pages'] = attempted_now
    for number, section in selected:
        row = saved.get(number)
        if row is None:
            metadata['pages'].append(_page_record(number, 'pending', ''))
            continue
        if row['status'] == 'recognized' and row['text'].strip():
            section.text = row['text']
            metadata['recognized_pages'].append(number)
            metadata['pages'].append(_page_record(number, 'recognized', row['text'], row))
        elif row['status'] == 'recognized':
            metadata['blank_pages'].append(number)
            metadata['pages'].append(_page_record(number, 'blank', '', row))
        else:
            metadata['failed_pages'].append(number)
            metadata['pages'].append(_page_record(number, 'failed', ''))
    metadata['pages'].sort(key=lambda r: r['page'])
    metadata['attempted_pages'] = sorted(saved)
    metadata['pending_pages'] = [n for n,_ in selected if n not in saved]
    metadata['continuation_available'] = bool(metadata['pending_pages'])
    metadata['unresolved_pages'] = [n for n,s in selected if not s.text.strip()]
    if metadata['status'] != 'unavailable_or_failed':
        if metadata['pending_pages']:
            metadata['status'] = 'incomplete'
        elif metadata['blank_pages'] or metadata['failed_pages']:
            metadata['status'] = 'pages_unresolved'
        else:
            metadata['status'] = 'complete'
    if state:
        state['rows'] = [saved[n] for n in sorted(saved)]
        metadata['checkpoint_version'] = CHECKPOINT_VERSION
        metadata['checkpoint_sha256'] = hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    counts = {k: len(metadata[k]) for k in ('requested_pages', 'recognized_pages', 'blank_pages', 'failed_pages', 'pending_pages')}
    metadata['summary'] = (f"OCR {metadata['status']}: {counts['recognized_pages']} of {counts['requested_pages']} scanned pages recognized "
                           f"({counts['blank_pages']} blank, {counts['failed_pages']} failed, {counts['pending_pages']} pending); "
                           'recognition is unverified.')
    if metadata['recognized_pages']:
        extraction.warnings.append(f"Pages {_ranges(metadata['recognized_pages'])} contain OCR-derived text; verify against the original image. Recognition is not lossless or independently verified.")
    if metadata['blank_pages']:
        extraction.warnings.append(f"OCR returned no text for pages {_ranges(metadata['blank_pages'])}; they remain empty and need review.")
    if metadata['failed_pages']:
        extraction.warnings.append(f"OCR failed for pages {_ranges(metadata['failed_pages'])}; they remain empty and need review.")
    if metadata['pending_pages']:
        extraction.warnings.append(f"OCR attempted at most {MAX_PAGES_PER_CALL} new pages or {int(TIME_BUDGET_SECONDS)} seconds per call; pages {_ranges(metadata['pending_pages'])} are pending. Continue OCR before using this document.")
    # Existing empty-page warnings remain as evidence of the original extraction gap.
    return extraction


def _ranges(numbers):
    """Compact page list such as 1-8, 10, 12-14."""
    parts, start, previous = [], None, None
    for n in sorted(numbers):
        if start is None:
            start = previous = n
        elif n == previous + 1:
            previous = n
        else:
            parts.append(f'{start}-{previous}' if start != previous else str(start)); start = previous = n
    if start is not None:
        parts.append(f'{start}-{previous}' if start != previous else str(start))
    return ', '.join(parts)
