"""Disposable parser process. It receives file paths, never service credentials."""
import json
import sys
import zipfile
from dataclasses import asdict
from pathlib import Path


def preflight(path):
    if path.suffix.lower() in {'.docx','.xlsx','.xlsm','.pptx','.epub'}:
        with zipfile.ZipFile(path) as archive:
            entries=archive.infolist()
            if len(entries)>10000 or sum(e.file_size for e in entries)>128*1024*1024:
                raise ValueError('Archive expansion limit exceeded')
            if any(e.file_size>32*1024*1024 or e.file_size>max(1,e.compress_size)*200 for e in entries):
                raise ValueError('Archive member expansion limit exceeded')


if __name__=='__main__':
    # The parent attaches Windows resource limits before releasing this gate.
    if sys.stdin.buffer.readline()!=b'go\n':
        raise SystemExit(2)
    if sys.platform!='win32':
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(1024**3,1024**3))
    from corpus.processing import extract_source
    source,output=map(Path,sys.argv[1:3])
    try:
        preflight(source)
        # Old long-running parents must not receive OCR text under their old
        # readiness/cache rules. Only the updated Processor opts into this path.
        options = sys.argv[3:]
        checkpoint = Path(options[2]) if len(options) == 3 and options[:2] == ['--ocr','--ocr-checkpoint'] else None
        if options not in ([], ['--ocr']) and checkpoint is None:
            raise ValueError('Invalid extraction options')
        result=extract_source(source,allow_ocr=bool(options),ocr_checkpoint=checkpoint)
        if len(result.sections)>10000 or sum(len(s.text) for s in result.sections)>8_000_000:
            raise ValueError('Extracted text limit exceeded')
        output.write_text(json.dumps(asdict(result),ensure_ascii=False),encoding='utf-8')
    except Exception:
        # Third-party exception text may contain document content or secrets.
        raise SystemExit(3)
