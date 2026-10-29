"""Keep CPU parallelism where RAM permits; no fixed small extraction-worker cap."""
import os
import platform
import re
import subprocess
from pathlib import Path

GiB = 1024 ** 3
# Streaming workers hold bounded text/token buffers. Reserve far more than their
# measured footprint for imports, file buffering and interpreter variation.
WORKER_RESERVE = 256 * 1024 ** 2


def available_memory():
    """Return currently available/reclaimable RAM, never swap/compressed pages."""
    try:
        if platform.system() == 'Darwin':
            result = subprocess.check_output(['/usr/bin/vm_stat'], text=True, timeout=5)
            page = int(re.search(r'page size of (\d+) bytes', result)[1])
            counts = {name: int(value) for name, value in re.findall(r'^([^:]+):\s+(\d+)\.', result, re.M)}
            # Inactive pages may include anonymous memory: count only free and
            # file-backed pages, with an additional safety reserve below.
            return page * (counts.get('Pages free', 0) + counts.get('File-backed pages', 0))
        if platform.system() == 'Linux':
            fields = dict(re.findall(r'^(\w+):\s+(\d+) kB', Path('/proc/meminfo').read_text(), re.M))
            return int(fields['MemAvailable']) * 1024
        return os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def extraction_workers(cores, jobs):
    if cores < 1:
        raise ValueError('--cores must be positive')
    available = available_memory()
    override = os.environ.get('EXPERIMENT_EXTRACT_MEMORY_GIB')
    if override:
        budget = float(override) * GiB
        if not (0 < budget < float('inf')):
            raise ValueError('EXPERIMENT_EXTRACT_MEMORY_GIB must be finite and positive')
    elif available is not None:
        budget = max(0, min(available * .7, available - GiB))
    else:
        # Never quietly fall back to dozens of jobs when memory cannot be read.
        budget = WORKER_RESERVE
        print('RAM availability unavailable; using one extraction worker. '
              'Set EXPERIMENT_EXTRACT_MEMORY_GIB to an explicit budget if needed.')
    if available is not None:
        budget = min(budget, max(0, min(available * .7, available - GiB)))
    workers = min(cores, jobs, int(budget // WORKER_RESERVE))
    if workers < 1:
        raise RuntimeError('Insufficient available RAM to start extraction safely; free memory and retry step 3.')
    print(f'Extraction: {workers} workers (CPU limit {cores}; RAM budget {budget/GiB:.1f} GiB; '
          f'{WORKER_RESERVE//(1024**2)} MiB reserved per worker).')
    return workers
