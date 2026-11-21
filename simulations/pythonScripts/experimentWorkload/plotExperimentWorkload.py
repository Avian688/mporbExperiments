#!/usr/bin/env python3
"""Plot short-flow completion time and long-flow goodput from aggregate workload JSON."""

import argparse
import json
import math
from pathlib import Path

from runExperimentWorkload import (
    DURATION, DEFAULT_OFFERED_MBPS, DEFAULT_WORKLOAD_SEED, PROTOCOLS, RESULTS,
    SUBFLOW_COUNTS, case_name, leo, scenario_folder,
)


PLOTS = leo.SIM_DIR / 'plots/experimentWorkload'
SHORT_MAX = 100_000
LONG_MIN = 1_000_000
MARKERS = ('o', 's', '^', 'D', 'v')


def read_results(folder, seed):
    results = {}
    schedule_hashes = set()
    missing = []
    for controller in PROTOCOLS:
        ks = (1,) if controller == 'OrbSingle' else SUBFLOW_COUNTS
        for k in ks:
            path = folder / f'{case_name(controller, k, seed)}.json'
            if not path.is_file():
                missing.append(path.stem)
                continue
            with path.open(encoding='utf-8') as handle:
                data = json.load(handle)
            if data.get('schema_version') != 1:
                raise ValueError(f'Unsupported workload result schema in {path}')
            case = data['case']
            if (case['controller'], case['requested_subflows']) != (controller, k):
                raise ValueError(f'Controller or subflow count does not match the file name in {path}')
            if schedule_hash := case['workload'].get('schedule_sha256'):
                schedule_hashes.add(schedule_hash)
            results[controller, k] = data['size_classes']
    if not results:
        raise ValueError(f'No workload results for seed {seed} in {folder}. Run runExperimentWorkload.py first.')
    if len(schedule_hashes) > 1:
        raise ValueError(f'The selected results use different arrival schedules in {folder}')
    if missing:
        print(f'Missing {len(missing)} of {len(missing) + len(results)} cases. Plots use the available results.')
    return results


def flow_means(results, metric, select_class, name):
    values = {}
    for case, size_classes in results.items():
        groups = [size_class for size_class in size_classes if select_class(size_class)]
        # Combine disjoint size classes by flow count, not by class count.
        distributions = [group[metric] for group in groups if group[metric]['count']]
        count = sum(distribution['count'] for distribution in distributions)
        if count:
            values[case] = math.fsum(d['mean'] * d['count'] for d in distributions) / count
        offered = sum(group['offered'] for group in groups)
        unfinished = sum(group['unfinished'] for group in groups)
        if unfinished:
            controller, k = case
            print(f'{controller} K={k}: {unfinished}/{offered} {name} flows unfinished at cutoff')
    return values


def draw(plt, path, values, title, ylabel, scale=1):
    fig, ax = plt.subplots(figsize=(7.2, 4.4), layout='constrained')
    ax.set(title=title, xlabel='Requested subflows (K)', ylabel=ylabel, xticks=SUBFLOW_COUNTS)
    ax.grid(axis='y', alpha=0.25)
    for index, (controller, (_, _, label)) in enumerate(PROTOCOLS.items()):
        if controller == 'OrbSingle':
            if (controller, 1) in values:
                ax.axhline(values[controller, 1] * scale, color='0.3', linestyle='--', label=label)
            continue
        points = [values.get((controller, k), math.nan) * scale for k in SUBFLOW_COUNTS]
        if any(math.isfinite(point) for point in points):
            ax.plot(SUBFLOW_COUNTS, points, label=label, color=f'C{index}', marker=MARKERS[index])
    if values:
        ax.set_ylim(bottom=0)
        ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.19), ncol=3, frameon=False)
    else:
        ax.text(0.5, 0.5, 'No completed flows in this size range', ha='center', transform=ax.transAxes)
        ax.set_yticks([])
    fig.savefig(path)
    plt.close(fig)
    print(f'Saved {path}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sim-time', type=float, default=DURATION)
    parser.add_argument('--offered-mbps', type=float, default=DEFAULT_OFFERED_MBPS)
    parser.add_argument('--workload-seed', type=int, default=DEFAULT_WORKLOAD_SEED)
    parser.add_argument('--results-dir', type=Path, help='Override the directory containing the aggregate JSON')
    parser.add_argument('--output-dir', type=Path, help='Override the PDF output directory')
    args = parser.parse_args()

    scenario = scenario_folder(args.sim_time, args.offered_mbps)
    results = read_results(args.results_dir or RESULTS / scenario, args.workload_seed)
    short = flow_means(results, 'fct_seconds',
                       lambda row: row['max_inclusive_bytes'] == SHORT_MAX, 'short')
    long = flow_means(results, 'goodput_mbps',
                      lambda row: row['min_exclusive_bytes'] >= LONG_MIN, 'long')

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    output = args.output_dir or PLOTS / scenario / f'seed{args.workload_seed}'
    output.mkdir(parents=True, exist_ok=True)
    draw(plt, output / 'short_flow_fct.pdf', short, 'Short-flow completion time (≤100 kB)', 'Mean FCT (ms)', 1000)
    draw(plt, output / 'long_flow_goodput.pdf', long, 'Long-flow goodput (>1 MB)', 'Mean goodput (Mb/s)')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(str(error))
