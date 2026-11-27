"""Connection-level plots and CSV summaries for the MPOrb evaluation."""

import csv
from collections import defaultdict
from statistics import mean

import numpy as np

from evaluation import PROTOCOLS, SWEEP_LABELS
from experiments import SIM_DIR
from plotStyle import plot_style, save_figure


def jain(rates):
    squares = np.sum(rates ** 2, axis=-1)
    return np.divide(np.sum(rates, axis=-1) ** 2, rates.shape[-1] * squares,
                     out=np.full(np.shape(squares), np.nan), where=squares > 0)


def integral(start, end, rates, left, right):
    width = np.maximum(0, np.minimum(end, right) - np.maximum(start, left))
    return np.sum(rates * width[:, None], axis=0) / (right - left)


def read_run(root, spec):
    path = root / 'results' / f'{spec["config"]}.csv'
    if not path.is_file():
        raise FileNotFoundError(f'Missing {path}; run that case before plotting')
    counters = np.loadtxt(path, delimiter=',', skiprows=1, ndmin=2)
    columns = 2 + spec['foreground'] + (len(spec['paths'][0]) if spec['subflow_metrics'] else 0)
    if not len(counters) or counters.shape[1] != columns or not np.all(np.isfinite(counters)):
        raise ValueError(f'Invalid endpoint counter fields in {path}')
    start, end = counters[:, 0], counters[:, 1]
    if (start[0] != 0 or not np.isclose(end[-1], spec['duration']) or np.any(end <= start)
            or not np.allclose(end[:-1], start[1:])):
        raise ValueError(f'Incomplete or invalid endpoint bins in {path}')
    rates = counters[:, 2:] * 8e-6 / (end - start)[:, None]
    apps = rates[:, :spec['foreground']]
    ranks = rates[:, spec['foreground']:]
    return start, end, apps, ranks


def one_second_rates(start, end, apps):
    # Recorder bins start at 0 and divide a whole second. Keep complete seconds.
    per_second = np.zeros((int(end[-1]), apps.shape[1]))
    seconds = np.floor(start).astype(int)
    valid = seconds < len(per_second)
    np.add.at(per_second, seconds[valid], apps[valid] * (end[valid] - start[valid])[:, None])
    return per_second


def summarize(spec, trace):
    start, end, apps, ranks = trace
    number = spec['experiment']
    left, right = 10, spec['duration'] - 1
    if number == '6':
        left, right = spec['join'] + 10, spec['leave']
    if right <= left:
        raise ValueError('Plotting requires a measurement interval after the warm-up')
    rates = integral(start, end, apps, left, right)
    seconds = one_second_rates(start, end, apps)[int(np.ceil(left)):int(right)]
    fairness = jain(seconds)
    row = dict(experiment=number, controller=spec['controller'], value=spec['value'], trial=spec['trial'],
               aggregate_mbps=float(rates.sum()), a_mbps=float(rates[0]),
               others_mbps=float(np.mean(rates[1:])) if len(rates) > 1 else np.nan,
               b_mbps=float(rates[1]) if len(rates) > 1 else np.nan,
               c_mbps=float(rates[2]) if len(rates) > 2 else np.nan,
               mean_second_jain=finite_mean(fairness), allocation_jain=np.nan,
               min_max_ratio=float(rates.min() / rates.max()) if rates.max() > 0 else np.nan,
               a_rank1_mbps=np.nan, a_rank2_mbps=np.nan, before_release_mbps=np.nan,
               after_release_mbps=np.nan, recovery_s=np.nan, a_before_join_mbps=np.nan,
               a_after_leave_mbps=np.nan)
    if spec['ideal']:
        normalized = seconds / np.asarray(spec['ideal'])
        row['allocation_jain'] = finite_mean(jain(normalized))
    if ranks.shape[1] >= 2:
        row['a_rank1_mbps'], row['a_rank2_mbps'] = integral(start, end, ranks, left, right)[:2]
    if number == '6':
        row['a_before_join_mbps'] = integral(start, end, apps, 10, spec['join'])[0]
        row['a_after_leave_mbps'] = integral(start, end, apps, spec['leave'] + 10, spec['duration'])[0]
    if number == 'A':
        release = spec['release']
        if release + 5 <= spec['duration']:
            row['before_release_mbps'] = integral(start, end, ranks, release - 10, release)[0]
            row['after_release_mbps'] = integral(start, end, ranks, release, release + 5)[0]
            recovered = np.convolve(ranks[:, 0], np.ones(10) / 10, mode='valid')
            hits = np.flatnonzero((start[:len(recovered)] >= release) & (recovered >= 90))
            if len(hits):
                row['recovery_s'] = end[hits[0] + 9] - release
    return row


def finite_mean(values):
    valid = [value for value in values if np.isfinite(value)]
    return float(mean(valid)) if valid else np.nan


CURVES = {
    'A': (('before_release_mbps', 'Affected subflow before release', 'TCP receive rate (Mb/s)'),
          ('after_release_mbps', 'First 5 s after release', 'TCP receive rate (Mb/s)'),
          ('recovery_s', 'Time to sustain 90 Mb/s for 1 s', 'Recovery time (s)')),
    '1': (('a_mbps', 'Connection A', 'Goodput (Mb/s)'),
          ('a_rank2_mbps', 'A on the shared link', 'TCP receive rate (Mb/s)'),
          ('mean_second_jain', 'Connection fairness', 'Mean per-second Jain index')),
    '2': (('a_mbps', 'Connection A (K=1)', 'Goodput (Mb/s)'),
          ('b_mbps', 'Connection B', 'Goodput (Mb/s)'),
          ('c_mbps', 'Connection C', 'Goodput (Mb/s)'),
          ('allocation_jain', 'Fairness relative to feasible allocation', 'Mean normalized Jain index')),
    '3': (('min_max_ratio', 'Connection goodput ratio', 'Smaller / larger goodput'),
          ('aggregate_mbps', 'Aggregate goodput', 'Goodput (Mb/s)')),
    '4': (('min_max_ratio', 'Connection goodput ratio', 'Smaller / larger goodput'),
          ('mean_second_jain', 'Fine-grained fairness', 'Mean per-second Jain index')),
    '5': (('mean_second_jain', 'Connection fairness', 'Mean per-second Jain index'),
          ('aggregate_mbps', 'Aggregate goodput', 'Goodput (Mb/s)')),
    '6': (('mean_second_jain', 'Fairness with added connections', 'Mean per-second Jain index'),
          ('a_mbps', 'Connection A with added connections', 'Goodput (Mb/s)')),
}


def sweep_plot(plt, output, number, groups, specs):
    metrics = CURVES['1' if number == 'B' else number]
    if number == '2':
        fig, panels = plt.subplots(2, 2, figsize=(8.2, 6.8), layout='constrained')
        axes = panels.flat
    else:
        fig, axes = plt.subplots(1, len(metrics), figsize=(4.1 * len(metrics), 3.4), layout='constrained')
    values = sorted({value for _, value in groups})
    for ax, (metric, title, ylabel) in zip(axes, metrics):
        for controller, (_, _, name, colour) in PROTOCOLS.items():
            rates = [(value, [row[metric] for row in groups[controller, value] if np.isfinite(row[metric])])
                     for value in values]
            if not any(series for _, series in rates):
                continue
            average = [finite_mean(series) for _, series in rates]
            spread = [(avg - min(series), max(series) - avg) if series else (0, 0)
                      for avg, (_, series) in zip(average, rates)]
            ax.errorbar(values, average, yerr=np.maximum(0, np.asarray(spread).T),
                        color=colour, marker='o', capsize=2, label=name)
        if metric in ('a_mbps', 'b_mbps', 'c_mbps') and number in ('1', '2', 'B'):
            ideal = [next(spec['ideal'] for spec in specs if spec['value'] == value) for value in values]
            index = ('a_mbps', 'b_mbps', 'c_mbps').index(metric)
            ax.plot(values, [point[index] for point in ideal], '--',
                    color='0.3', label='Feasible max-min')
        if metric in ('min_max_ratio', 'mean_second_jain', 'allocation_jain'):
            ax.set_ylim(0, 1.04)
        elif metric == 'recovery_s':
            ax.set_ylim(bottom=0)
        ax.set(xlabel=SWEEP_LABELS['1' if number == 'B' else number], ylabel=ylabel, title=title, xticks=values)
        ax.grid(alpha=.2)
        if ax.lines:
            ax.legend(loc='best', fontsize='small')
        else:
            ax.text(.5, .5, 'No runs reached the target', ha='center', transform=ax.transAxes)
    save_figure(fig, output / 'summary', aggregate=True)
    plt.close(fig)


def trace_plots(plt, output, number, traces):
    if number not in ('A', '4', '5', '6'):
        return
    for value, cases in sorted(traces.items()):
        fig, axes = plt.subplots(2, 1, figsize=(8, 5.4), sharex=True, layout='constrained')
        for controller, runs in cases.items():
            _, _, label, colour = PROTOCOLS[controller]
            for pos, ax in enumerate(axes):
                xs, ys = [], []
                for spec, (start, end, apps, ranks) in runs:
                    if number == 'A':
                        x, y = end - spec['release'], ranks[:, pos]
                    else:
                        seconds = one_second_rates(start, end, apps)
                        x = np.arange(len(seconds))
                        if number == '6':
                            # Present the newcomers' common start at t=0 across trials.
                            x = x + .5 - spec['join']
                            active = (x >= 0) & (x < spec['leave'] - spec['join'])
                            y = finite_phase_fairness(seconds, active) if pos else seconds[:, :2].mean(axis=1)
                        else:
                            y = jain(seconds) if pos else seconds.sum(axis=1)
                    xs.append(x)
                    ys.append(y)
                grid = xs[0]
                aligned = [np.interp(grid, x, y, left=np.nan, right=np.nan) for x, y in zip(xs, ys)]
                series = np.array([finite_mean(column) for column in np.asarray(aligned).T])
                ax.plot(grid, series, color=colour, label=label)
        upper, lower = axes
        if number == 'A':
            upper.set_ylabel('Affected rank (Mb/s)')
            lower.set_ylabel('Other rank (Mb/s)')
            lower.set_xlabel('Seconds from background removal')
            lower.set_xlim(-20, min(60, runs[0][0]['duration'] - runs[0][0]['release']))
            for ax in axes:
                ax.axvline(0, color='0.5', linestyle=':')
        else:
            upper.set_ylabel('Incumbent goodput (Mb/s)' if number == '6' else 'Aggregate goodput (Mb/s)')
            lower.set_ylabel('Connection Jain index')
            lower.set_ylim(0, 1.04)
            lower.set_xlabel('Seconds from newcomers joining' if number == '6' else 'Simulation time (s)')
            if number == '6':
                for ax in axes:
                    ax.axvspan(0, 100, color='0.5', alpha=.12)
        upper.legend(ncol=3, fontsize='small')
        for ax in axes:
            ax.grid(alpha=.2)
        save_figure(fig, output / f'trace_{value}', aggregate=True)
        plt.close(fig)


def finite_phase_fairness(seconds, active):
    return np.where(active, jain(seconds), jain(seconds[:, :2]))


def plot(root, specs):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    number = specs[0]['experiment']
    output = SIM_DIR / 'plots' / 'evaluation' / root.parent.name / root.name
    output.mkdir(parents=True, exist_ok=True)
    groups = defaultdict(list)
    traces = defaultdict(lambda: defaultdict(list))
    rows = []
    for spec in specs:
        trace = read_run(root, spec)
        row = summarize(spec, trace)
        rows.append(row)
        groups[spec['controller'], spec['value']].append(row)
        if number in ('A', '4', '5', '6'):
            traces[spec['value']][spec['controller']].append((spec, trace))
    with (output / 'runs.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with plot_style(aggregate=True):
        sweep_plot(plt, output, number, groups, specs)
        trace_plots(plt, output, number, traces)
    print(f'Plots and summaries: {output}', flush=True)
