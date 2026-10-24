"""Bound parser wall time and memory; this is not a filesystem/network sandbox."""
import sys
from pathlib import Path

from .acquisition import AcquisitionError
from .vendor.worker_runtime import WorkerRuntimeError, read_extraction_output, run_extraction_worker


def bounded_extract(path,timeout=120,allow_ocr=False,ocr_checkpoint=None):
    from .processing import Extraction, Section, PROJECT_ROOT
    output=Path(path).parent/'extraction-result.json'
    options = ['--ocr'] if allow_ocr else []
    if ocr_checkpoint is not None:
        if not allow_ocr: raise ValueError('Checkpoint requires explicit OCR opt-in')
        options += ['--ocr-checkpoint', str(ocr_checkpoint)]
    try:
        output.unlink(missing_ok=True)
        runtime=run_extraction_worker(Path(sys.executable),[str(path),str(output),*options],PROJECT_ROOT,
                                      Path(path).parent,timeout)
        result=read_extraction_output(output,64*1024*1024)
        result['metadata']['worker_runtime']=runtime.metadata()
        return Extraction([Section(**s) for s in result['sections']],result['metadata'],result['warnings'])
    except WorkerRuntimeError as exc:
        if exc.code=='timeout':
            raise AcquisitionError('extraction_timeout','Parser exceeded its time limit') from None
        if exc.code=='attach_failed':
            raise AcquisitionError('extraction_limits','Could not attach parser resource limits') from None
        if exc.code=='output_oversize':
            raise AcquisitionError('extraction_limit','Parser output exceeds the size limit') from None
        raise AcquisitionError('extraction_failed','Parser rejected the file or exceeded a resource limit; original retained when requested') from None
    finally:
        output.unlink(missing_ok=True)
