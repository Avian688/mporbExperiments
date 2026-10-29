"""Replot the joint responsiveness CDF and RTT ratios from extracted results."""
import argparse
import json
import math

from experiments import PROTOCOLS, directory
from analyze import plot


def main(default_experiments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiments', nargs='+', type=int, choices=range(1, 5),
                        default=default_experiments or [1, 2, 3, 4])
    parser.add_argument('--protocols', nargs='+', choices=tuple(PROTOCOLS), default=list(PROTOCOLS))
    parser.add_argument('--runs', type=int, choices=range(1, 6), default=5)
    parser.add_argument('--configs', nargs='+', help='Exact configuration names')
    parser.add_argument('--rtts', nargs='+', type=int)
    parser.add_argument('--sim-time', type=float, help='Select a duration; otherwise infer it from extracted summaries')
    args = parser.parse_args()
    if args.sim_time is not None and (not math.isfinite(args.sim_time) or not 11 < args.sim_time <= 300):
        parser.error('--sim-time must be finite and between 11 and 300 seconds (exclusive of 11)')
    configs = []
    for number in args.experiments:
        matrix = json.loads((directory(number) / 'matrix.json').read_text())
        configs.extend(c for c in matrix if c['protocol'] in args.protocols and c['run'] <= args.runs
                       and (not args.rtts or c['rtt_ms'] is None or c['rtt_ms'] in args.rtts))
    if args.configs:
        unknown = set(args.configs) - {c['config'] for c in configs}
        if unknown:
            parser.error(f'Unknown selected configurations: {sorted(unknown)}')
        configs = [c for c in configs if c['config'] in args.configs]
    selected = []
    for config in configs:
        summary_path = directory(config['experiment']) / 'csvs/extracted' / config['config'] / 'summary.json'
        if not summary_path.is_file():
            continue
        summary = json.loads(summary_path.read_text())
        if args.sim_time is not None and summary['sim_time'] != args.sim_time:
            continue
        selected.append(dict(config, sim_time=summary['sim_time'], measurement_end=summary['sim_time'] - 1))
    plot(selected)


def run(default_experiments=None):
    try:
        main(default_experiments)
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error))


if __name__ == '__main__':
    run()
