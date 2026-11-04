"""Endpoint vector selection, CSV extraction and reusable plotting data."""
import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re

import leo
from analyzeExperimentLeo import sender_ranks

RATES = {'goodput','throughput','retransmissionRate'}
COUNTERS = {'numRtos', 'metaReinjectedBytes'}
DATA_SUBFLOWS = {'rtt', 'cwnd', 'U', 'mbytesInFlight'}
RECEIVER_METRICS = {'goodput', 'throughput', 'holBlockedBytes'}


def terminal_folder(pair, receiver=False):
    side = 'destination' if receiver else 'source'
    city = re.sub(r'[^A-Za-z0-9]+', '_', pair[side+'_city'])
    return f'userTerminal{pair[side]}_{city}'


def sample_series(samples, horizon, bins=3000):
    """Keep first/last/min/max per bin, with bounded memory and original times."""
    if bins < 1 or not math.isfinite(horizon) or horizon <= 0:
        raise ValueError('Invalid plotting horizon or bin count')
    points, bucket = [], []
    current = None
    count = 0

    def flush():
        if bucket:
            chosen = {bucket[0], bucket[-1], min(bucket, key=lambda p:p[2]),
                      max(bucket, key=lambda p:p[2])}
            points.extend((t, v) for _, t, v in sorted(chosen))

    for index, (time, value) in enumerate(samples):
        if time > horizon:
            break
        if time < 0 or not math.isfinite(value):
            continue
        count += 1
        key = min(bins - 1, int(time / horizon * bins))
        if current != key:
            flush()
            bucket.clear()
            current = key
        point = index, time, value
        if not bucket:
            bucket = [point]
        else:
            bucket = [bucket[0], min((*bucket, point), key=lambda p:p[2]),
                      max((*bucket, point), key=lambda p:p[2]), point]
    flush()
    return points, count


def prepare_curve(vectors, vector, metric, horizon, bins=3000):
    points, count = sample_series(vectors.samples(vector), horizon, bins)
    return finish_curve(points,count,metric,horizon,not vector.blocks)


def finish_curve(points, count, metric, horizon, empty):
    status = 'recorded' if points else 'no_samples'
    if metric in COUNTERS:
        if empty:
            # These registered cumulative counters start at zero and emit when
            # incremented. An absent recording is handled separately, never here.
            points, status = [(0., 0.), (horizon, 0.)], 'zero_events'
        elif points and points[0][0] > 0:
            points.insert(0, (0., 0.))
    # Measured RTT is a set of observations, not a state held across an outage.
    # Periodic rates also must not extend beyond their last actual interval.
    if points and metric != 'rtt' and metric not in RATES and points[-1][0] < horizon:
        points.append((horizon, points[-1][1]))
    return points, count, status


def applicable(config, metric):
    return not ((metric == 'U' and config['protocol'] == 'CubicUncoupled') or
                (metric in ('holBlockedBytes', 'metaReinjectedBytes') and config['protocol'] == 'OrbSingle'))


def pair_vectors(vectors, config, pair, metric):
    """Return (vector, label, rank) without inferring passive-side path identity."""
    if not applicable(config, metric):
        return []
    receiver = metric in RECEIVER_METRICS
    terminal = pair['destination'] if receiver else pair['source']
    host = rf'\.userTerminal\[{terminal}\]'
    pattern = re.compile(host + (r'\.app\[0\]\.thread_\d+$' if metric == 'goodput'
                                else r'\.tcp\.conn-\d+$'))
    found = [v for v in vectors.items.values() if v.metric == metric and pattern.search(v.module)]
    found.sort(key=lambda v:int(v.module.rsplit('_' if metric == 'goodput' else '-', 1)[1]))
    if receiver:
        return [(v, 'Application goodput' if metric == 'goodput' and len(found) == 1 else
                    ('Receiver ' + v.module.rsplit('.',1)[-1]), None) for v in found]
    ranks = sender_ranks(vectors, pair['source'], config['k'], config['protocol'] == 'OrbSingle')
    by_module = {v.module:rank for rank,v in ranks.items()}
    if metric in DATA_SUBFLOWS:
        found = [v for v in found if v.module in by_module]
    curves = []
    for vector in found:
        rank = by_module.get(vector.module)
        label = (f'Path {rank} ({vector.module.rsplit(".",1)[-1]})' if rank is not None else
                 'Meta/control ' + vector.module.rsplit('.',1)[-1])
        curves.append((vector,label,rank))
    return curves


def export_vectors(config, vectors, bins=3000):
    """Stream full endpoint CSVs and compact plot CSVs to fixed file names."""
    root = leo.vector_dir(config)
    root.mkdir(parents=True,exist_ok=True)
    manifest = root/'vectors.json'
    # An interrupted export must not leave a valid-looking old manifest.
    manifest.unlink(missing_ok=True)
    records, expected = [], set()
    for vector in vectors.items.values():
        if vector.metric not in leo.USER_METRICS:
            continue
        receiver = vector.metric in RECEIVER_METRICS
        side = 'destination' if receiver else 'source'
        pair = next((p for p in leo.PAIRS if re.search(
            rf'\.userTerminal\[{p[side]}\]\.'+
            (r'app\[0\]\.thread_\d+$' if vector.metric=='goodput' else r'tcp\.conn-\d+$'),
            vector.module)),None)
        if pair is None:
            continue
        relative = Path(terminal_folder(pair,receiver))/vector.module.split(f'.userTerminal[{pair[side]}].',1)[1]
        directory = root/relative
        directory.mkdir(parents=True,exist_ok=True)
        raw_path = directory/f'{vector.metric}.csv'
        plot_path = directory/f'{vector.metric}.plot.csv'
        raw_temp = raw_path.with_suffix('.csv.tmp')
        count = 0
        with raw_temp.open('w',newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(('time',vector.metric))
            def recorded_samples():
                nonlocal count
                for time,value in vectors.samples(vector):
                    writer.writerow((time,value))
                    count += 1
                    yield time,value
            stream = recorded_samples()
            points, plotted_count = sample_series(stream,config['sim_time'],bins)
            # Retain every original sample even if it is beyond the plot horizon.
            for _ in stream:
                pass
        raw_temp.replace(raw_path)
        points, _, status = finish_curve(points,plotted_count,vector.metric,config['sim_time'],count==0)
        plot_temp = plot_path.with_suffix('.csv.tmp')
        with plot_temp.open('w',newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(('time',vector.metric))
            writer.writerows(points)
        plot_temp.replace(plot_path)
        expected.update((raw_path,plot_path))
        records.append(dict(id=vector.id,module=vector.module,metric=vector.metric,
            csv=str(raw_path.relative_to(root)),plot_csv=str(plot_path.relative_to(root)),
            raw_points=count,plot_points=len(points),status=status,
            csv_stat=leo.stat(raw_path),plot_csv_stat=leo.stat(plot_path)))
    # Only this run's owned vector directory is pruned. Removed recordings must
    # not survive as stale CSVs, and reruns never create numbered backups.
    for path in root.rglob('*.csv'):
        if path not in expected:
            path.unlink()
    for path in root.rglob('*.tmp'):
        path.unlink()
    payload = dict(version=1,config=config,source_stat=leo.stat(vectors.path),
        input_hash=leo.input_hash(config),bins=bins,vectors=records)
    temporary = manifest.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(payload,indent=2)+'\n')
    temporary.replace(manifest)
    return payload


@dataclass
class CsvVector:
    id: str
    module: str
    metric: str


class CsvVectors:
    """Read only exported CSVs; plotting never opens the native result vectors."""
    def __init__(self, root, config):
        self.root = Path(root)
        path = self.root/'vectors.json'
        try:
            self.manifest = json.loads(path.read_text())
        except FileNotFoundError as error:
            raise ValueError(f'{config["config"]}: terminal CSVs have not been extracted; '
                             'run runExperimentLeo.py --start-step 3 --skip-generate') from error
        if self.manifest.get('version')!=1 or self.manifest.get('config')!=config:
            raise ValueError(f'Stale terminal CSV manifest: {path}; re-extract step 3')
        self.metadata = {v['id']:v for v in self.manifest['vectors']}
        self.items = {key:CsvVector(key,v['module'],v['metric']) for key,v in self.metadata.items()}

    def samples(self, vector, compact=False):
        record = self.metadata[vector.id]
        key = 'plot_csv' if compact else 'csv'
        path = self.root/record[key]
        if not path.is_file() or leo.stat(path)!=record[key+'_stat']:
            raise ValueError(f'Missing or changed extracted CSV: {path}; re-extract step 3')
        with path.open(newline='') as handle:
            reader = csv.reader(handle)
            if next(reader,None)!=['time',vector.metric]:
                raise ValueError(f'Invalid extracted CSV header: {path}')
            for row in reader:
                if len(row)!=2:
                    raise ValueError(f'Invalid extracted CSV row: {path}')
                yield float(row[0]),float(row[1])

    def curve(self, vector):
        record = self.metadata[vector.id]
        points = list(self.samples(vector,compact=True))
        if len(points)!=record['plot_points']:
            raise ValueError(f'Incomplete plot CSV for {vector.module}/{vector.metric}; re-extract step 3')
        return points,record['raw_points'],record['status']
