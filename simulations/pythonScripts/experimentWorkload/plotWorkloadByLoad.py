#!/usr/bin/env python3
"""Plot mean completion time versus offered load and goodput versus session rank."""

import argparse
from collections import defaultdict
import csv
import gzip
import math
from statistics import mean

from plotExperimentWorkload import (
    DURATION, DEFAULT_LAYOUT, DEFAULT_WORKLOAD_SEED, LARGE_MIN, LAYOUT_NAMES, LONG_MIN,
    MARKERS, PLOT_PROTOCOLS, SHORT_MAX, SUBFLOW_COUNTS, get_layout, read_cases,
    save_figure, saved_seeds, scenario_folder,
)


SIZE_PANELS = ((0, SHORT_MAX, 'Up to 100 kB'), (SHORT_MAX, LONG_MIN, 'Over 100 kB to 1 MB'))
STYLES = {
    controller: dict(color=f'C{index}', marker=marker)
    for index, (controller, marker) in enumerate(zip(
        (name for name in PLOT_PROTOCOLS if name != 'OrbSingle'), (*MARKERS, 'X', 'P')))
}
STYLES['OrbSingle'] = dict(color='0.3', marker='d', linestyle='--')


def size_class(data, lower, upper):
    return next(group for group in data['size_classes']
                if (group['min_exclusive_bytes'], group['max_inclusive_bytes']) == (lower, upper))


def diagnostic_rank(path):
    opener = gzip.open if path.suffix == '.gz' else open
    goodputs = []
    with opener(path, 'rt', encoding='utf-8', newline='') as handle:
        for row in csv.DictReader(handle):
            size = int(row['offered_bytes'])
            if not LONG_MIN < size <= LARGE_MIN or not row['completion_s']:
                continue
            fct = float(row['fct_s']) if row.get('fct_s') else float(row['completion_s']) - float(row['arrival_s'])
            if fct > 0:
                goodputs.append(size * 8e-6 / fct)
    return sorted(goodputs)


def session_ranks(data, folder):
    group = size_class(data, LONG_MIN, LARGE_MIN)
    values = group.get('session_goodput_mbps')
    if values is None:
        for name in ('flow_outcomes.csv.gz', 'flow_outcomes.csv'):
            path = folder / name
            if path.is_file():
                values = diagnostic_rank(path)
                break
    if values is not None:
        return range(1, len(values) + 1), values, False

    distribution = group['goodput_mbps']
    count = distribution['count']
    if count == 0:
        return (), (), False
    # The recorder uses the nearest-rank percentile, including the minimum at P0.
    points = {max(1, (percentile * count + 99) // 100): value
              for percentile, value in enumerate(distribution['quantiles'])}
    return tuple(points), tuple(points.values()), True


def draw_fct(plt, path, data, seeds, loads, label):
    fig, axes = plt.subplots(1, 2, figsize=(8.8, 3.8))
    fig.subplots_adjust(left=.09, right=.98, bottom=.27, top=.79, wspace=.36)
    fig.suptitle(f'Mean completion time by offered load\n{label}')
    handles = {}
    for ax, (lower, upper, title) in zip(axes, SIZE_PANELS):
        ax.set(title=title, xlabel='Offered load (Mb/s)', ylabel='Mean FCT (ms)', xticks=loads)
        ax.grid(axis='y', alpha=.25)
        for controller, points in data.items():
            values = []
            for load in loads:
                groups = [size_class(points[load][seed][0], lower, upper) for seed in seeds] if load in points else []
                if groups and all(group['fct_seconds']['count'] for group in groups):
                    values.append(1000 * mean(group['fct_seconds']['mean'] for group in groups))
                else:
                    values.append(math.nan)
            if any(math.isfinite(value) for value in values):
                handles[controller], = ax.plot(loads, values, label=PLOT_PROTOCOLS[controller][2],
                                               **STYLES[controller])
        ax.set_ylim(bottom=0)
    if not handles:
        plt.close(fig)
        return
    fig.legend(handles.values(), [PLOT_PROTOCOLS[name][2] for name in handles],
               loc='lower center', ncol=min(3, len(handles)), frameon=False)
    save_figure(plt, fig, path)


def draw_ranks(plt, path, data, load, seed, k):
    fig, ax = plt.subplots(figsize=(7.2, 4.4), layout='constrained')
    ax.set(title=f'Session goodput (1–10 MB)\n{load:g} Mb/s offered · K={k} · Seed {seed}',
           xlabel='Rank of transport session (lowest goodput first)', ylabel='Goodput (Mb/s)')
    ax.grid(axis='y', alpha=.25)
    has_sampled = False
    for controller, points in data.items():
        if load not in points:
            continue
        ranks, values, sampled = session_ranks(*points[load][seed])
        if not values:
            continue
        has_sampled |= sampled
        style = STYLES[controller].copy()
        style.pop('marker')
        if sampled:
            style.update(marker='.', markersize=2)
        ax.plot(ranks, values, label=PLOT_PROTOCOLS[controller][2], **style)
    if not ax.lines:
        plt.close(fig)
        return
    if has_sampled:
        ax.set_xlabel('Rank of transport session (lowest first; dots are saved percentiles)')
    ax.set_ylim(bottom=0)
    ax.legend(loc='lower right', fontsize='small')
    save_figure(plt, fig, path)


def collect(layout, args):
    parent = (layout.results / scenario_folder(args.sim_time, 1)).parent
    loads = args.loads
    if loads is None:
        loads = []
        for path in parent.glob('*Mbps'):
            if not path.is_dir():
                continue
            try:
                load = float(path.name.removesuffix('Mbps'))
            except ValueError:
                continue
            if math.isfinite(load) and load > 0:
                loads.append(load)
    by_duration = defaultdict(dict)
    for load in sorted(set(loads)):
        folder = layout.results / scenario_folder(args.sim_time, load)
        if args.diagnostic:
            folder /= 'diagnostics'
            if args.diagnostic == 'window-updates':
                folder /= 'window_updates'
        saved = saved_seeds(folder, diagnostic=bool(args.diagnostic))
        if not saved and args.loads is not None:
            print(f'No saved cases at {load:g} Mb/s in {folder}')
        for seed in saved:
            if args.workload_seed is not None and seed != args.workload_seed:
                continue
            cases, metadata = read_cases(folder, seed, layout.name, diagnostic=bool(args.diagnostic))
            if metadata['offered_mbps'] is not None and metadata['offered_mbps'] != load:
                raise ValueError(f'Offered load in results does not match {folder}')
            duration = metadata['duration_s'] or args.sim_time
            by_duration[duration][load, seed] = cases
    return by_duration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--layout', choices=LAYOUT_NAMES, default=DEFAULT_LAYOUT)
    parser.add_argument('--sim-time', type=float, default=DURATION, help='Source scenario duration (default: 600 s)')
    parser.add_argument('--loads', type=float, nargs='+', help='Offered loads in Mb/s (default: all saved loads)')
    parser.add_argument('--ks', type=int, choices=SUBFLOW_COUNTS, nargs='+', default=SUBFLOW_COUNTS)
    parser.add_argument('--protocols', choices=PLOT_PROTOCOLS, nargs='+', default=PLOT_PROTOCOLS)
    parser.add_argument('--workload-seed', type=int, help='Use this seed for both plots')
    parser.add_argument('--diagnostic', nargs='?', const='standard', choices=('standard', 'window-updates'),
                        help='Read diagnostic results, optionally from the window-updates runs')
    args = parser.parse_args()
    if args.loads and any(not math.isfinite(load) or load <= 0 for load in args.loads):
        parser.error('--loads requires finite positive rates')

    layout = get_layout(args.layout)
    by_duration = collect(layout, args)
    if not by_duration:
        raise ValueError(f'No matching saved results for {layout.name} at {args.sim_time:g}s')

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    output = layout.plots / 'workload_by_load' / (args.diagnostic or 'full')
    for duration, results in sorted(by_duration.items()):
        for k in sorted(set(args.ks)):
            data = {}
            for controller in args.protocols:
                case = controller, 1 if controller == 'OrbSingle' else k
                points = defaultdict(dict)
                for (load, seed), cases in results.items():
                    if case in cases:
                        points[load][seed] = cases[case]
                if points:
                    data[controller] = points
            if not data or (k != 1 and set(data) == {'OrbSingle'}):
                continue
            loads = sorted({load for points in data.values() for load in points})
            common = set.intersection(*(set(samples) for points in data.values() for samples in points.values()))
            directory = output / f'{duration:g}s' / f'K{k}'
            directory.mkdir(parents=True, exist_ok=True)
            if common:
                seeds = sorted(common)
                seed_label = 'Seed' if len(seeds) == 1 else 'Seeds'
                label = f'K={k} · {duration:g} s · Completed downloads · {seed_label} {", ".join(map(str, seeds))}'
                draw_fct(plt, directory / 'avg_fct_by_load.pdf', data, seeds, loads, label)
            else:
                print(f'{duration:g}s K={k}: no common seed across loads for the mean FCT plot.')
            for load in loads:
                matched = common or set.intersection(*(set(points[load]) for points in data.values() if load in points))
                if not matched:
                    print(f'{duration:g}s K={k} at {load:g} Mb/s: no common seed for the session rank plot.')
                    continue
                seed = DEFAULT_WORKLOAD_SEED if DEFAULT_WORKLOAD_SEED in matched else min(matched)
                draw_ranks(plt, directory / f'goodput_by_session_rank_{load:g}Mbps_seed{seed}.pdf',
                           data, load, seed, k)
            if len(loads) == 1:
                print(f'{duration:g}s K={k}: one saved load ({loads[0]:g} Mb/s).')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(str(error))
