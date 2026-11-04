"""Read selected OMNeT++ vector blocks directly, without large CSV-R copies."""
from dataclasses import dataclass, field
import math
from pathlib import Path
import shlex


@dataclass
class Vector:
    id: str
    module: str
    metric: str
    columns: str
    blocks: list = field(default_factory=list)


class Vectors:
    def __init__(self, path):
        self.path = Path(path)
        self.items = {}
        index = self.path.with_suffix('.vci')
        if not index.is_file():
            raise FileNotFoundError(f'Index missing; run opp_scavetool index {self.path}')
        file_record = None
        with index.open() as source:
            for line in source:
                if line.startswith('file '):
                    file_record = line.split()
                elif line.startswith('vector '):
                    a = shlex.split(line)
                    self.items[a[1]] = Vector(a[1], a[2], a[3].split(':')[0], a[4])
                elif line and line[0].isdigit():
                    a = line.split()
                    if a[0] not in self.items or len(a) < 3:
                        raise ValueError(f'Invalid vector index row: {index}')
                    self.items[a[0]].blocks.append((int(a[1]), int(a[2])))
        info = self.path.stat()
        if (file_record is None or int(file_record[1]) != info.st_size or
                int(file_record[2]) != int(info.st_mtime)):
            raise ValueError(f'Stale vector index; run opp_scavetool index {self.path}')

    def samples(self, vector):
        if 'T' not in vector.columns or 'V' not in vector.columns:
            raise ValueError(f'Unsupported vector columns {vector.columns}')
        ti, vi = 1 + vector.columns.index('T'), 1 + vector.columns.index('V')
        previous = -math.inf
        with self.path.open('rb') as source:
            for offset, size in vector.blocks:
                source.seek(offset)
                end = offset + size
                while source.tell() < end:
                    line = source.readline()
                    if not line:
                        raise ValueError(f'Truncated vector block: {self.path}')
                    a = line.split()
                    if not a or a[0].decode() != vector.id or len(a) != 1 + len(vector.columns):
                        raise ValueError(f'Invalid indexed vector block: {self.path}')
                    t, value = float(a[ti]), float(a[vi])
                    if not math.isfinite(t) or t < previous:
                        raise ValueError(f'Unordered vector {vector.module}/{vector.metric}')
                    previous = t
                    yield t, value

    def find(self, module, metric):
        return [v for v in self.items.values() if v.module == module and v.metric == metric]


def seconds(samples, start, end):
    """Equal-time bins keep high-ACK-rate paths from dominating mean RTT."""
    bins = {}
    for t, value in samples:
        if t >= end:
            break
        if start <= t and math.isfinite(value) and value > 0:
            second = math.floor(t)
            total, count = bins.get(second, (0., 0))
            bins[second] = total + value, count + 1
    return {second: total/count for second, (total, count) in bins.items()}


def interval_integral(samples, start, end, rate=False):
    """Return recorded area, coverage and bounds without extrapolating rates."""
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError('Invalid measurement interval')
    previous = None
    area = coverage = 0.
    covered_start = covered_end = None
    for t, value in samples:
        if previous is not None:
            left, right = max(previous[0], start), min(t, end)
            dt = max(0., right - left)
            if dt:
                used = value if rate else previous[1]
                if not math.isfinite(used):
                    raise ValueError('Non-finite value in measurement interval')
                area += dt * used
                coverage += dt
                covered_start = left if covered_start is None else covered_start
                covered_end = right
        previous = t, value
        if t >= end:
            break
    if previous is not None and not rate and previous[0] < end:
        dt = max(0., end - max(start, previous[0]))
        area += dt * previous[1]
        coverage += dt
        if dt:
            covered_start = max(start,previous[0]) if covered_start is None else covered_start
            covered_end = end
    bounds = (covered_start,covered_end) if coverage else None
    return area, coverage, bounds


def interval_mean(samples, start, end, rate=False):
    """Strict full-window mean; rates describe the preceding interval."""
    area, coverage, _ = interval_integral(samples,start,end,rate)
    if coverage < end - start - 1e-6:
        raise ValueError(f'Vector does not cover the complete measurement interval '
                         f'({coverage:g}/{end-start:g}s covered in {start:g}–{end:g}s)')
    return area / (end - start)
