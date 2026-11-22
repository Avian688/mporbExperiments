#!/usr/bin/env python3
"""Plot completion time and goodput from aggregate workload JSON."""

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
LARGE_MIN = 10_000_000
MARKERS = ('o', 's', '^', 'D', 'v')
CASES = tuple((controller, k) for controller in PROTOCOLS
              for k in ((1,) if controller == 'OrbSingle' else SUBFLOW_COUNTS))


def read_results(folder, seed):
    results = {}
    schedule_hashes = set()
    scenario_values = {'offered_mbps': set(), 'duration_s': set()}
    for controller, k in CASES:
        path = folder / f'{case_name(controller, k, seed)}.json'
        if not path.is_file():
            continue
        with path.open(encoding='utf-8') as handle:
            data = json.load(handle)
        if data.get('schema_version') != 1:
            raise ValueError(f'Unsupported workload result schema in {path}')
        case = data['case']
        workload = case['workload']
        if (case['controller'], case['requested_subflows'], workload.get('seed', seed)) != (controller, k, seed):
            raise ValueError(f'Workload case does not match the file name in {path}')
        if schedule_hash := workload.get('schedule_sha256'):
            schedule_hashes.add(schedule_hash)
        for key, values in scenario_values.items():
            if workload.get(key) is not None:
                values.add(workload[key])
        results[controller, k] = data['size_classes']
    if not results:
        raise ValueError(f'No workload results for seed {seed} in {folder}. Run runExperimentWorkload.py first.')
    if len(schedule_hashes) > 1:
        raise ValueError(f'The selected results use different arrival schedules in {folder}')
    if any(len(values) > 1 for values in scenario_values.values()):
        raise ValueError(f'Seed {seed} uses different offered loads or durations in {folder}')
    if len(results) < len(CASES):
        print(f'Seed {seed}: missing {len(CASES) - len(results)} of {len(CASES)} cases.')
    return results, {key: next(iter(values), None) for key, values in scenario_values.items()}


def flow_means(results, metric, select_class, name, seed):
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
            print(f'Seed {seed}, {controller} K={k}: {unfinished}/{offered} {name} flows unfinished at cutoff')
    return values


def fct_percentile(results, boundary, size, percentile):
    values = {}
    for case, size_classes in results.items():
        for size_class in size_classes:
            if size_class[boundary] == size:
                quantiles = size_class['fct_seconds']['quantiles']
                if percentile < len(quantiles):
                    values[case] = quantiles[percentile]
                break
    return values


def run_metrics(results, seed):
    return {
        'short_mean': flow_means(results, 'fct_seconds', lambda row: row['max_inclusive_bytes'] == SHORT_MAX,
                                 'short', seed),
        'long_goodput': flow_means(results, 'goodput_mbps', lambda row: row['min_exclusive_bytes'] >= LONG_MIN,
                                   'long', seed),
        'short_p95': fct_percentile(results, 'max_inclusive_bytes', SHORT_MAX, 95),
        'short_p99': fct_percentile(results, 'max_inclusive_bytes', SHORT_MAX, 99),
        'large_p95': fct_percentile(results, 'min_exclusive_bytes', LARGE_MIN, 95),
    }


def saved_seeds(folder):
    prefixes = {case_name(controller, k, 0).removesuffix('_Seed0') for controller, k in CASES}
    seeds = set()
    for path in folder.glob('WebSearch_*_Seed*.json'):
        prefix, _, seed = path.stem.rpartition('_Seed')
        if prefix in prefixes and seed.isascii() and seed.isdecimal():
            seeds.add(int(seed))
    return sorted(seeds)


def average_metrics(by_seed):
    averages, ranges = {}, {}
    for metric in by_seed[0]:
        samples = [run[metric] for run in by_seed]
        common = set.intersection(*(set(sample) for sample in samples))
        averages[metric], ranges[metric] = {}, {}
        for case in common:
            values = [sample[case] for sample in samples]
            low, high = min(values), max(values)
            averages[metric][case] = min(high, max(low, math.fsum(values) / len(values)))
            ranges[metric][case] = (low, high)
        missing = set.union(*(set(sample) for sample in samples)) - common
        if missing:
            print(f'{metric}: omitted {len(missing)} cases with missing values in the selected seeds.')
    return averages, ranges


def draw_curves(ax, values, title, ylabel, scale=1, spread=None):
    ax.set(title=title, xlabel='Requested subflows (K)', ylabel=ylabel, xticks=SUBFLOW_COUNTS)
    ax.grid(axis='y', alpha=0.25)
    for index, (controller, (_, _, label)) in enumerate(PROTOCOLS.items()):
        if controller == 'OrbSingle':
            if (controller, 1) in values:
                if spread is not None:
                    low, high = spread[controller, 1]
                    ax.axhspan(low * scale, high * scale, color='0.3', alpha=0.12)
                ax.axhline(values[controller, 1] * scale, color='0.3', linestyle='--', label=label)
            continue
        points = [values.get((controller, k), math.nan) * scale for k in SUBFLOW_COUNTS]
        if any(math.isfinite(point) for point in points):
            ax.plot(SUBFLOW_COUNTS, points, label=label, color=f'C{index}', marker=MARKERS[index])
            if spread is not None:
                ks = [k for k in SUBFLOW_COUNTS if (controller, k) in values]
                means = [values[controller, k] for k in ks]
                limits = [spread[controller, k] for k in ks]
                ax.errorbar(ks, [mean * scale for mean in means],
                            yerr=([(mean - low) * scale for mean, (low, _) in zip(means, limits)],
                                  [(high - mean) * scale for mean, (_, high) in zip(means, limits)]),
                            fmt='none', ecolor=f'C{index}', capsize=3)
    if values:
        ax.set_ylim(bottom=0)
        ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.19), ncol=3, frameon=False)
    else:
        message = ('No data for all selected seeds' if spread is not None
                   else 'No completed flows in this size range')
        ax.text(0.5, 0.5, message, ha='center', transform=ax.transAxes)
        ax.set_yticks([])


def save_figure(plt, fig, path):
    fig.savefig(path)
    plt.close(fig)
    print(f'Saved {path}')


def draw(plt, path, values, title, ylabel, scale=1, spread=None):
    fig, ax = plt.subplots(figsize=(7.2, 4.4), layout='constrained')
    draw_curves(ax, values, title, ylabel, scale, spread)
    save_figure(plt, fig, path)


def draw_short_tail(plt, path, metrics, workload_label, spreads):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6), layout='constrained')
    fig.suptitle(f'Short-flow completion time (≤100 kB)\n{workload_label}')
    for ax, percentile in zip(axes, (95, 99)):
        metric = f'short_p{percentile}'
        draw_curves(ax, metrics[metric], f'P{percentile} FCT', 'FCT (ms)', 1000, spreads.get(metric))
    save_figure(plt, fig, path)


def draw_workload(plt, output, metrics, workload_label, spreads=None):
    output.mkdir(parents=True, exist_ok=True)
    spreads = spreads or {}
    subtitle = f'\n{workload_label}' if spreads else ''
    draw(plt, output / 'short_flow_fct.pdf', metrics['short_mean'],
         f'Short-flow completion time (≤100 kB){subtitle}', 'Mean FCT (ms)', 1000, spreads.get('short_mean'))
    draw(plt, output / 'long_flow_goodput.pdf', metrics['long_goodput'],
         f'Long-flow goodput (>1 MB){subtitle}', 'Mean goodput (Mb/s)', spread=spreads.get('long_goodput'))
    draw_short_tail(plt, output / 'short_flow_fct_tail.pdf', metrics, workload_label, spreads)
    draw(plt, output / 'large_flow_fct_p95.pdf', metrics['large_p95'],
         f'Large-flow completion time (>10 MB)\n{workload_label}', 'P95 FCT (s)', spread=spreads.get('large_p95'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sim-time', type=float, default=DURATION)
    parser.add_argument('--offered-mbps', type=float, default=DEFAULT_OFFERED_MBPS)
    parser.add_argument('--workload-seed', type=int,
                        help='Seed for the individual plots (default: seed 1 if available)')
    parser.add_argument('--average-seeds', type=int, nargs='+',
                        help='Seeds to average (default: all saved seeds for this scenario)')
    parser.add_argument('--results-dir', type=Path, help='Override the directory containing the aggregate JSON')
    parser.add_argument('--output-dir', type=Path, help='Override the PDF output directory')
    args = parser.parse_args()

    scenario = scenario_folder(args.sim_time, args.offered_mbps)
    folder = args.results_dir or RESULTS / scenario
    available = saved_seeds(folder)
    seeds = sorted(set(args.average_seeds)) if args.average_seeds is not None else available
    if args.average_seeds is not None and (len(seeds) < 2 or seeds[0] < 0):
        parser.error('--average-seeds requires at least two distinct nonnegative seed IDs')
    individual_seed = args.workload_seed
    if individual_seed is None:
        individual_seed = DEFAULT_WORKLOAD_SEED
        if available and individual_seed not in available:
            individual_seed = available[0]
    individual_results, individual_scenario = read_results(folder, individual_seed)
    individual_metrics = run_metrics(individual_results, individual_seed)
    offered = individual_scenario['offered_mbps']
    if offered is None:
        offered = args.offered_mbps

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    output = args.output_dir or PLOTS / scenario / f'seed{individual_seed}'
    draw_workload(plt, output, individual_metrics,
                  f'{offered:g} Mb/s offered load · Workload seed {individual_seed}')

    if len(seeds) < 2:
        print('Averaged plots require at least two saved workload seeds.')
        return
    scenarios = {'offered_mbps': set(), 'duration_s': set()}
    by_seed = []
    for seed in seeds:
        if seed == individual_seed:
            metrics, seed_scenario = individual_metrics, individual_scenario
        else:
            results, seed_scenario = read_results(folder, seed)
            metrics = run_metrics(results, seed)
        by_seed.append(metrics)
        for key, value in seed_scenario.items():
            if value is not None:
                scenarios[key].add(value)
    if any(len(values) > 1 for values in scenarios.values()):
        raise ValueError('Averaged plots require the same offered load and duration across seeds')

    averages, spreads = average_metrics(by_seed)
    mean_output = args.output_dir / 'averaged' if args.output_dir else PLOTS / scenario / 'averaged'
    offered = next(iter(scenarios['offered_mbps']), args.offered_mbps)
    seed_label = ', '.join(map(str, seeds))
    draw_workload(plt, mean_output, averages,
                  f'{offered:g} Mb/s offered load · Seeds {seed_label} (mean and min–max)', spreads)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(str(error))
