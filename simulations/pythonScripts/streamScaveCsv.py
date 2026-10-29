"""Bounded-memory CSV-R extraction; numeric text is copied without float reformatting.

CSV-R stores an entire vector in a field. Neither pandas row chunks nor csv.reader
bound memory for that format. Scan fields in fixed blocks, skip unneeded data,
and spool only the selected row's time/value fields to temporary files.
"""
from collections import OrderedDict
from contextlib import ExitStack
import math
import re
import tempfile
from pathlib import Path

BLOCK_BYTES = 1024 * 1024
_SPECIAL = re.compile(rb'[,"\r\n]')
_THREAD = re.compile(r'\.thread_\d+')


def field_parts(handle):
    """Yield (bytes, end_field, end_row), including quoted multiline/escaped fields."""
    quoted = pending_quote = closed = False
    at_start = True
    skip_lf = False
    row_open = False
    while chunk := handle.read(BLOCK_BYTES):
        pos = 0
        while pos < len(chunk):
            if skip_lf:
                skip_lf = False
                if chunk[pos:pos+1] == b'\n':
                    pos += 1
                    continue
            if pending_quote:
                pending_quote = False
                if chunk[pos:pos+1] == b'"':
                    yield b'"', False, False
                    pos += 1
                    continue
                quoted = False
                closed = True
            if quoted:
                end = chunk.find(b'"', pos)
                if end < 0:
                    yield chunk[pos:], False, False
                    break
                if end > pos:
                    yield chunk[pos:end], False, False
                pos = end + 1
                pending_quote = True
                continue
            match = _SPECIAL.search(chunk, pos)
            end = match.start() if match else len(chunk)
            if end > pos:
                if closed:
                    raise ValueError('Characters after a closing CSV quote')
                yield chunk[pos:end], False, False
                at_start = False
                row_open = True
            if not match:
                break
            char = chunk[end:end+1]
            pos = end + 1
            if char == b'"':
                if not at_start or closed:
                    raise ValueError('Quote inside an unquoted CSV field')
                quoted = True
                at_start = False
                row_open = True
            else:
                end_row = char != b','
                yield b'', True, end_row
                at_start = True
                closed = False
                row_open = not end_row
                skip_lf = char == b'\r'
    if pending_quote:
        quoted = False  # closing quote exactly at EOF
    if quoted:
        raise ValueError('Unterminated CSV quoted field')
    if row_open:
        yield b'', True, True


def vector_records(path, accept):
    """Yield selected metadata and disk-backed raw vector fields, closed on advance.

accept receives a metadata dict; scalar/long records have no vector field files.
No unselected vector fields are retained when metadata precedes them (CSV-R).
"""
    with Path(path).open('rb', buffering=BLOCK_BYTES) as source, ExitStack() as files:
        header = None
        header_fields = []
        column = 0
        text = bytearray()
        row = {}
        spools = {}
        for data, end_field, end_row in field_parts(source):
            key = header[column] if header is not None and column < len(header) else None
            if header is None or key in ('module', 'name', 'type', 'time', 'value'):
                text.extend(data)
                if len(text) > BLOCK_BYTES:
                    raise ValueError('CSV metadata field exceeds 1 MiB')
            elif key in ('vectime', 'vecvalue'):
                # Nonstandard column ordering is supported by spooling until the
                # selection metadata is known. Standard exports skip immediately.
                known = all(k in row for k in ('module', 'name')) and ('type' not in header or 'type' in row)
                if not known or accept(row):
                    if key not in spools:
                        spools[key] = files.enter_context(tempfile.TemporaryFile())
                    spools[key].write(data)
            if end_field:
                if header is None:
                    header_fields.append(text.decode('utf-8-sig'))
                elif key in ('module', 'name', 'type', 'time', 'value'):
                    row[key] = text.decode('utf-8')
                text.clear()
                column += 1
            if end_row:
                if header is None:
                    header = header_fields
                    if not {'module', 'name'}.issubset(header):
                        raise ValueError('CSV needs module and name columns')
                    if not ({'vectime', 'vecvalue'}.issubset(header) or {'time', 'value'}.issubset(header)):
                        raise ValueError('Expected vectime/vecvalue or time/value columns')
                elif column == 1 and not any(row.values()) and not spools:
                    pass
                else:
                    if column != len(header):
                        raise ValueError(f'CSV row has {column} fields; expected {len(header)}')
                    if accept(row):
                        for file in spools.values():
                            file.seek(0)
                        yield row, spools, 'vectime' in header and 'vecvalue' in header
                files.close()
                spools = {}
                row = {}
                column = 0


def tokens(file):
    """Bounded numeric-token batches; supports whitespace and legacy comma arrays."""
    tail = b''
    while chunk := file.read(BLOCK_BYTES):
        data = tail + chunk.replace(b',', b' ')
        end = max(data.rfind(c) for c in (b' ', b'\n', b'\r', b'\t'))
        if end < 0:
            tail = data
            if len(tail) > BLOCK_BYTES:
                raise ValueError('Vector numeric token exceeds 1 MiB')
            continue
        yield from data[:end].split()
        tail = data[end+1:]
    if tail.strip():
        yield tail.strip()


def copy_vector(times, values, output, metric):
    """Keep original numeric precision; buffer writes, never a full vector."""
    output.write(b'time,' + metric.encode('utf-8') + b'\n')
    batch = []
    count = 0
    for time, value in zip(tokens(times), tokens(values)):
        batch.append(time + b',' + value + b'\n')
        if len(batch) == 8192:
            output.writelines(batch)
            count += len(batch)
            batch.clear()
    output.writelines(batch)
    return count + len(batch)


def extract_vectors(csv_path, out_root, metrics):
    """Match existing paths/last-vector-wins semantics; return number written."""
    out_root = Path(out_root)
    metrics = set(metrics)
    available = set()
    def accept(row):
        vector = row.get('type', 'vector').strip().lower() == 'vector'
        metric = row.get('name', '').split(':', 1)[0].strip()
        if vector and len(available) < 60:
            available.add(metric)
        return vector and metric in metrics
    written = 0
    # Long-form records are interleaved. Disk partitions preserve group order
    # and legacy cleaned-thread collision behaviour without retaining samples.
    groups = {}
    handles = OrderedDict()
    out_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.extract-', dir=out_root) as temp:
        try:
            for row, spools, wide in vector_records(csv_path, accept):
                metric = row['name'].split(':', 1)[0].strip()
                target = out_root / _THREAD.sub('', row['module']) / (metric + '.csv')
                if wide:
                    if 'vectime' not in spools or 'vecvalue' not in spools:
                        continue
                    stage = Path(temp) / 'vector.csv'
                    with stage.open('wb', buffering=BLOCK_BYTES) as output:
                        count = copy_vector(spools['vectime'], spools['vecvalue'], output, metric)
                    if count:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        stage.replace(target)
                        written += 1
                else:
                    key = (row['module'], row['name'])
                    if key not in groups:
                        groups[key] = (Path(temp) / f'group-{len(groups)}.csv', target)
                    try:
                        time, value = float(row['time']), float(row['value'])
                    except (ValueError, KeyError):
                        continue
                    if not math.isfinite(time) or not math.isfinite(value):
                        continue
                    stage, _ = groups[key]
                    if key not in handles:
                        if len(handles) >= 64:
                            _, old = handles.popitem(last=False)
                            old.close()
                        new = not stage.exists()
                        handles[key] = stage.open('a', buffering=64*1024)
                        if new:
                            handles[key].write(f'time,{metric}\n')
                    handles.move_to_end(key)
                    handles[key].write(f'{time},{value}\n')
        finally:
            for file in handles.values():
                file.close()
        for stage, target in groups.values():
            if not stage.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            stage.replace(target)
            written += 1
    print(f'wrote {written} vector CSV files under {out_root}')
    if not written:
        print('available vector names:', ', '.join(sorted(available)))
    return written
