"""Standalone per-run plots for the recorded OrbTCP baseline and MPTCP essentials."""
import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np
from experiments import SIM_DIR, PROTOCOLS, METRICS, MSS, RUN_COUNTS, run_numbers, directory, recorded_metrics
from plotStyle import TRACE_SIZE, plot_style, save_figure

LABELS = {
    'goodput': ('Goodput (Mbps)', 1e-6),
    'cwnd': ('CWND (MSS)', 1 / MSS),
    'rtt': ('RTT (ms)', 1000), 'srtt': ('Smoothed RTT (ms)', 1000),
    'throughput': ('Throughput (Mbps)', 1e-6),
    'mbytesInFlight': ('Bytes In-Flight (MSS)', 1 / MSS),
    'retransmissionRate': ('Retransmission rate (Mbps)', 1e-6),
    'queueLength': ('Queue Length (pkts)', 1),
    'bandwidth': ('Bandwidth (Mbps)', 1e-6),
    'holBlockedBytes': ('HoL blocked bytes', 1),
    'metaReinjectedBytes': ('Cumulative meta-level reinjected bytes', 1),
}
TITLES = {'goodput': 'Goodput', 'cwnd': 'CWND', 'rtt': 'RTT', 'srtt': 'SRTT',
          'throughput': 'Throughput', 'mbytesInFlight': 'Bytes In-Flight',
          'retransmissionRate': 'Retransmission Rate', 'queueLength': 'Queue Length',
          'bandwidth': 'Bandwidth', 'holBlockedBytes': 'HoL Blocking',
          'metaReinjectedBytes': 'Reinjected Bytes'}
RATES = {'goodput', 'throughput', 'retransmissionRate'}


def read_series(path, horizon=None, bins=3000):
    """Bounded plotting arrays: retain first/last/min/max in each time bin."""
    if horizon is None:
        horizon = 0.
        with Path(path).open() as handle:
            rows = csv.reader(handle)
            next(rows, None)
            for row in rows:
                if row:
                    horizon = max(horizon, float(row[0]))
    width = max(horizon, 1e-9) / bins
    points, bucket = [], []
    current = None
    def flush():
        if bucket:
            selected = {bucket[0], bucket[-1], min(bucket, key=lambda p: p[2]), max(bucket, key=lambda p: p[2])}
            points.extend((time, value) for _, time, value in sorted(selected))
    with Path(path).open() as handle:
        rows = csv.reader(handle)
        next(rows, None)
        for index, row in enumerate(rows):
            if len(row) < 2:
                continue
            time, value = float(row[0]), float(row[1])
            if not math.isfinite(time) or not math.isfinite(value):
                continue
            key = int(time / width)
            if key != current:
                flush()
                bucket.clear()
                current = key
            # Track extrema without holding the full bin's samples.
            point = (index, time, value)
            if not bucket:
                bucket = [point]
            elif len(bucket) < 4:
                bucket.append(point)
            else:
                first, low, high, _ = bucket
                bucket = [first, min(low, point, key=lambda p: p[2]), max(high, point, key=lambda p: p[2]), point]
            if len(bucket) == 4:
                bucket = [bucket[0], min(bucket, key=lambda p: p[2]), max(bucket, key=lambda p: p[2]), bucket[-1]]
    flush()
    return np.asarray(points, dtype=float).reshape(-1, 2)


@plot_style()
def draw(paths, metric, output, title, horizon=None, capacity=None):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=TRACE_SIZE)
    curves = 0
    for path in paths:
        series = read_series(path, horizon)
        if not len(series):
            continue
        if metric not in RATES and horizon is not None and series[-1, 0] < horizon:
            series = np.vstack((series, [horizon, series[-1, 1]]))
        label = path.stem.removesuffix('__' + metric).removeprefix('twoPathDumbbell.')
        ax.step(series[:, 0], series[:, 1] * LABELS[metric][1],
                where='pre' if metric in RATES else 'post', label=label)
        curves += 1
    if capacity is not None:
        ax.step(capacity[:, 0], capacity[:, 1], where='post', color='purple',
                linestyle='--', label='Available Bandwidth')
    if curves or capacity is not None:
        ax.legend(fontsize=8, loc='upper left')
    else:
        ax.text(.5, .5, 'No recorded samples', ha='center', va='center', transform=ax.transAxes)
    ax.set(xlabel='Time (s)', ylabel=LABELS[metric][0], title=title)
    ax.set_ylim(bottom=0)
    if horizon is not None:
        ax.set_xlim(0, horizon)
    ax.grid(True)
    fig.tight_layout(rect=[0, 0, 1, 1], pad=1.)
    save_figure(fig, output)
    plt.close(fig)
    return curves


def plot_metrics(configs, metrics):
    figures = 0
    for config in configs:
        root = directory(config['experiment'])
        out = root / 'csvs/extracted' / config['config']
        summary_path = out / 'summary.json'
        if not summary_path.is_file():
            continue
        summary = json.loads(summary_path.read_text())
        raw = root / 'csvs/raw' / f'{config["config"]}.csv'
        if any(summary.get(key) != value for key, value in config.items()) or not raw.is_file() or \
            summary['raw_stat'] != [raw.stat().st_size, raw.stat().st_mtime_ns]:
            raise ValueError(f'Stale extraction for {config["config"]}; rerun extraction')
        for metric in metrics:
            if metric not in recorded_metrics(config['experiment']):
                continue
            paths = sorted(out.glob(f'*__{metric}.csv'))
            output = SIM_DIR / 'plots' / f'experiment{config["experiment"]}' / 'individual' / config['config'] / metric
            title = f'{TITLES[metric]} - {PROTOCOLS[config["protocol"]][2]}'
            if config['rtt_ms'] is not None:
                title += f', R={config["rtt_ms"]} ms'
            if config['layout'] is not None:
                title += f' ({config["layout"].lower()})'
            capacity = None
            if metric == 'goodput' and config['experiment'] <= 2:
                from analyze import available_capacity
                records = json.loads((root / 'scenarios' / f'run{config["run"]}.json').read_text())
                events = sorted({0, config['sim_time'], *(r[k] for r in records for k in ('time', 'up_time')
                    if r[k] < config['sim_time'])})
                times = np.asarray(events)
                capacity = np.column_stack((times, available_capacity(records, times).sum(axis=0)))
            draw(paths, metric, output, title, config['sim_time'], capacity)
            figures += 1
    if not figures:
        raise ValueError('No selected extracted runs to plot; run extraction first')
    print(f'Wrote {figures} individual metric figures (PNG/PDF)')


def main(default_experiment=None, default_metric=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='*', type=Path, help='Optional extracted vector CSVs to plot directly')
    parser.add_argument('--experiment', type=int, choices=range(1, 5), default=default_experiment)
    parser.add_argument('--metrics', nargs='+', choices=('all', *METRICS), default=[default_metric or 'all'])
    parser.add_argument('--protocols', nargs='+', choices=tuple(PROTOCOLS), default=list(PROTOCOLS))
    parser.add_argument('--runs', type=int, choices=range(1, max(RUN_COUNTS.values()) + 1),
                        help='Limit seeds to 1..N; defaults to 50 trials for experiments 1/2 and five seeds for 3/4')
    parser.add_argument('--configs', nargs='+')
    parser.add_argument('--rtts', nargs='+', type=int)
    parser.add_argument('--sim-time', type=float, help='Expected duration; otherwise read it from each extracted summary')
    parser.add_argument('--output', type=Path, help='Output basename when plotting CSV files directly')
    args = parser.parse_args()
    metrics = list(METRICS) if 'all' in args.metrics else args.metrics
    if args.files:
        if len(metrics) != 1:
            parser.error('Direct CSV plotting requires one metric, e.g. --metrics cwnd')
        metric = metrics[0]
        draw(args.files, metric, args.output or Path(metric), TITLES[metric], args.sim_time)
        return
    if args.experiment is None:
        parser.error('--experiment is required when no CSV files are supplied')
    configs = json.loads((directory(args.experiment) / 'matrix.json').read_text())
    configs = [c for c in configs if c['protocol'] in args.protocols and c['run'] in run_numbers(args.experiment, args.runs)
               and (not args.rtts or c['rtt_ms'] is None or c['rtt_ms'] in args.rtts)]
    if args.configs:
        unknown = set(args.configs) - {c['config'] for c in configs}
        if unknown:
            parser.error(f'Unknown configurations: {sorted(unknown)}')
        configs = [c for c in configs if c['config'] in args.configs]
    selected = []
    for config in configs:
        summary = directory(args.experiment) / 'csvs/extracted' / config['config'] / 'summary.json'
        if summary.is_file():
            horizon = args.sim_time if args.sim_time is not None else json.loads(summary.read_text())['sim_time']
            config.update(sim_time=horizon, measurement_end=horizon - 1)
            selected.append(config)
    plot_metrics(selected, metrics)


def run(default_experiment=None, default_metric=None):
    try:
        main(default_experiment, default_metric)
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error))


if __name__ == '__main__':
    run()
