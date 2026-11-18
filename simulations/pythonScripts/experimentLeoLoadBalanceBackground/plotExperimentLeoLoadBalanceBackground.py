"""Foreground fairness and goodput for a fixed two-burst LEO replay."""

from collections import defaultdict
import math
from pathlib import Path
from statistics import mean
import sys

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / 'experimentLeo'))

from analyzeExperimentLeo import HEATMAP_LABELS, connection_goodput, jain_fairness, write_csv
from experiments import PROTOCOLS
from plotStyle import plot_style, save_figure


def window_seconds(intervals):
    seconds = []
    previous_end = -math.inf
    for start, end in intervals:
        if (not math.isfinite(start) or not math.isfinite(end) or
                int(start) != start or int(end) != end or end <= start or start < previous_end):
            raise ValueError('Plot windows must be ordered, disjoint intervals with whole-second bounds')
        seconds.extend(range(int(start), int(end)))
        previous_end = end
    return seconds


def window_metrics(bins, seconds, access_ceiling):
    complete = [bins[second] for second in seconds if bins[second] is not None]
    scores = [jain_fairness(rates) for rates in complete if any(rates)]
    # Incomplete vectors cannot distinguish missing measurements from zero goodput.
    # Keep the coverage in the CSV and pair only fully measured runs in the scatter.
    fully_measured = len(complete) == len(seconds)
    total_rates = [mean(rates) for rates in zip(*complete)] if fully_measured else [math.nan] * 2
    total = sum(total_rates)
    return dict(
        foreground_a_goodput_mbps=total_rates[0], foreground_b_goodput_mbps=total_rates[1],
        aggregate_goodput_mbps=total, normalized_goodput=total / access_ceiling,
        mean_per_second_jain=mean(scores) if scores else math.nan,
        jain_of_mean_goodput=jain_fairness(total_rates),
        seconds_expected=len(seconds), seconds_with_jain=len(scores),
        seconds_all_zero=len(complete) - len(scores), seconds_missing_goodput=len(seconds) - len(complete),
    )


def run_metrics(config, receiver_series, burst_windows, access_ceiling):
    whole = ((config['measurement_start'], config['measurement_end']),)
    if len(burst_windows) != 2:
        raise ValueError('Expected two background burst windows')
    first, second = burst_windows
    windows = {'whole': whole, 'burst_1': (first,), 'burst_2': (second,), 'bursts': burst_windows}
    foreground = [[list(thread) for thread in receiver] for receiver in receiver_series(config)]
    if len(foreground) != 2:
        raise ValueError('Expected goodput samples for two foreground receivers')

    bins = {}
    for second in window_seconds(whole):
        rates, covered = zip(*(connection_goodput(series, second, second + 1) for series in foreground))
        bins[second] = rates if all(covered) and all(math.isfinite(rate) and rate >= 0 for rate in rates) else None

    rows = []
    for name, intervals in windows.items():
        covered_seconds = window_seconds(intervals)
        if any(second not in bins for second in covered_seconds):
            raise ValueError(f'{name} extends beyond the measurement window')
        rows.append(dict(
            protocol=config['protocol'], k=config['k'], run=config['run'], window=name,
            intervals_s=';'.join(f'{start:g}–{end:g}' for start, end in intervals),
            access_ceiling_mbps=access_ceiling,
            **window_metrics(bins, covered_seconds, access_ceiling),
        ))
    return rows


def summarize(rows, goodput_field='normalized_goodput'):
    cases = defaultdict(list)
    for row in rows:
        cases[row['window'], row['k'], row['protocol']].append(row)

    points, samples = [], {}
    for case, runs in cases.items():
        window, k, protocol = case
        usable = [run for run in runs if math.isfinite(run['mean_per_second_jain']) and
                  math.isfinite(run[goodput_field])]
        samples[case] = [(run['mean_per_second_jain'], run[goodput_field]) for run in usable]
        fields = ('mean_per_second_jain', 'jain_of_mean_goodput', 'aggregate_goodput_mbps', 'normalized_goodput')
        points.append(dict(
            window=window, k=k, protocol=protocol, intervals_s=runs[0]['intervals_s'],
            access_ceiling_mbps=runs[0]['access_ceiling_mbps'], runs_selected=len(runs),
            runs_plotted=len(usable),
            runs_with_partial_goodput=sum(run['seconds_missing_goodput'] > 0 for run in runs),
            seconds_with_jain=sum(run['seconds_with_jain'] for run in runs),
            seconds_all_zero=sum(run['seconds_all_zero'] for run in runs),
            seconds_missing_goodput=sum(run['seconds_missing_goodput'] for run in runs),
            **{metric: mean(run[metric] for run in usable) if usable else math.nan for metric in fields},
        ))
    return points, samples


def plot_bursts(points, paired_runs, ks, output, goodput_field):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Ellipse
    from matplotlib.ticker import MaxNLocator
    import numpy as np

    visible = [point for point in points if point['window'] == 'bursts' and point['runs_plotted']]
    colours = {protocol: values[3] for protocol, values in PROTOCOLS.items()}
    colours.update({'Omega10': '#7851A9', 'OrbSingle': '#333333',
                    'OLIA': '#CC79A7', 'BALIA': '#8C564B'})
    include_omega10 = any(point['protocol'] == 'Omega10' for point in visible)
    raw_goodput = goodput_field == 'aggregate_goodput_mbps'
    y_label = 'Aggregate foreground goodput (Mbps)' if raw_goodput else 'Normalized Aggregate Goodput'
    xlo, xhi, ylo, yhi = .95, 1.01, .85, 1.025
    if raw_goodput:
        ylo, yhi = (math.inf, -math.inf) if visible else (0, 100)
    lower_y_margin, upper_y_margin = (5, 5) if raw_goodput else (.03, .025)
    ellipses = {}
    for point in visible:
        k, protocol = point['k'], point['protocol']
        samples = paired_runs['bursts', k, protocol]
        x, y = point['mean_per_second_jain'], point[goodput_field]
        sx = sy = 0.
        if len(samples) > 1 and np.any(np.ptp(samples, axis=0) > 0):
            covariance = np.cov(samples, rowvar=False)
            variances, axes = np.linalg.eigh(covariance)
            height, width = 2 * np.sqrt(np.maximum(variances, 0))
            angle = math.degrees(math.atan2(axes[1, 1], axes[0, 1]))
            ellipses[k, protocol] = dict(width=width, height=height, angle=angle)
            sx, sy = np.sqrt(np.diag(covariance))
        xlo, xhi = min(xlo, x - sx - .01), max(xhi, x + sx + .01)
        ylo, yhi = min(ylo, y - sy - lower_y_margin), max(yhi, y + sy + upper_y_margin)

    def draw(ax, k):
        for point in visible:
            protocol, recorded_k = point['protocol'], point['k']
            plot_k = 1 if protocol == 'OrbSingle' and not raw_goodput else k
            if recorded_k != plot_k:
                continue
            x, y = point['mean_per_second_jain'], point[goodput_field]
            label = HEATMAP_LABELS.get(protocol, protocol).replace('\n', ' ')
            if protocol == 'Omega' and include_omega10:
                label = 'Omega 5%'
            colour = colours[protocol]
            ellipse = ellipses.get((recorded_k, protocol))
            if ellipse:
                ax.add_patch(Ellipse((x, y), **ellipse, facecolor=colour, edgecolor=colour,
                                     alpha=.22, linewidth=.8))
            ax.scatter(x, y, s=48, marker='o', facecolors='none', edgecolors=colour,
                       linewidths=1.4, label=label, zorder=3)
        if not ax.collections:
            ax.text(.5, .5, 'No usable runs', transform=ax.transAxes, ha='center', va='center')
        ax.set(xlim=(max(0., xlo), xhi), ylim=(max(0., ylo), yhi))
        ax.xaxis.set_major_locator(MaxNLocator(nbins=6))
        ax.yaxis.set_major_locator(MaxNLocator(nbins=6))
        ax.grid(alpha=.35)

    with plot_style(aggregate=True), plt.rc_context({'text.usetex': False}):
        if raw_goodput:
            columns = min(2, len(ks))
            rows = math.ceil(len(ks) / columns)
            fig, axes = plt.subplots(rows, columns, figsize=(4 * columns, 2.4 * rows + .65),
                                     squeeze=False, sharex=True, sharey=True)
            handles_by_label = {}
            for ax, k in zip(axes.flat, ks):
                draw(ax, k)
                ax.set_title(f'K={k}')
                handles, labels = ax.get_legend_handles_labels()
                handles_by_label.update(zip(labels, handles))
            for ax in list(axes.flat)[len(ks):]:
                ax.set_visible(False)
            if handles_by_label:
                fig.legend(handles_by_label.values(), handles_by_label.keys(), loc='upper center',
                           ncol=min(4, len(handles_by_label)), fontsize=8)
            fig.supxlabel('Mean per-second Jain fairness')
            fig.supylabel(y_label)
            fig.tight_layout(rect=(.02, .02, 1, .90 if handles_by_label else 1))
            save_figure(fig, output / 'goodput_fairness', aggregate=True)
            plt.close(fig)
        else:
            for k in ks:
                fig, ax = plt.subplots(figsize=(5, 3), layout='constrained')
                draw(ax, k)
                if ax.collections:
                    ax.legend(loc='lower center', bbox_to_anchor=(.5, 1.02), ncol=3, fontsize=8)
                ax.set(xlabel='Mean 1 s Jain Fairness', ylabel=y_label)
                fig.suptitle(f'LEO load balance during bursts, K={k}', fontsize=10)
                save_figure(fig, output / f'fairness_efficiency_K{k}', aggregate=True)
                plt.close(fig)


def plot(configs, output, receiver_series, *, burst_windows=((20, 40), (60, 80)),
         access_ceiling_mbps=200., raw_goodput=False):
    """Plot foreground metrics from extracted goodput, expressed in bits per second."""
    output = Path(output)
    configs = list(configs)
    if not configs or not math.isfinite(access_ceiling_mbps) or access_ceiling_mbps <= 0:
        raise ValueError('Expected experiment configs and a positive foreground access ceiling')
    rows = [row for config in configs
            for row in run_metrics(config, receiver_series, burst_windows, access_ceiling_mbps)]
    goodput_field = 'aggregate_goodput_mbps' if raw_goodput else 'normalized_goodput'
    points, samples = summarize(rows, goodput_field)
    write_csv(output / 'fairness_efficiency_runs.csv', rows)
    write_csv(output / 'fairness_efficiency_data.csv', points)
    for point in points:
        if point['window'] == 'bursts' and point['runs_plotted'] < point['runs_selected']:
            print(f'{point["protocol"]} K={point["k"]}: {point["runs_plotted"]}/{point["runs_selected"]} '
                  'runs have complete goodput and defined burst-window fairness', flush=True)
    plot_bursts(points, samples, sorted({config['k'] for config in configs}), output, goodput_field)


def plot_run(config, output, series, *, burst_windows=((20, 40), (60, 80))):
    """Plot one-second receiver goodput for a run, leaving gaps where coverage is missing."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    protocol, k, run = config['protocol'], config['k'], config['run']
    seconds = window_seconds(((0, config['sim_time']),))
    edges = [*seconds, seconds[-1] + 1]
    output = Path(output) / 'individual' / protocol / f'K{k}' / f'run{run}'
    output.mkdir(parents=True, exist_ok=True)

    with plot_style():
        fig, (foreground, background) = plt.subplots(2, 1, figsize=(8, 5), sharex=True,
                                                     layout='constrained')
        for axis in (foreground, background):
            for index, (start, end) in enumerate(burst_windows):
                axis.axvspan(start, end, color='#909090', alpha=.17, linewidth=0,
                             label='Scheduled burst' if axis is background and index == 0 else None)
            axis.set_ylabel('Goodput (Mbps)')
            axis.grid(axis='y', alpha=.3)

        for axis, roles in ((foreground, (('A', '#0072B2'), ('B', '#009E73'))),
                            (background, (('Background', '#7851A9'),))):
            missing = []
            for role, colour in roles:
                threads = list(series.get(role, {}).values())
                rates = []
                for second in seconds:
                    rate, complete = connection_goodput(threads, second, second + 1)
                    rates.append(rate if complete and math.isfinite(rate) and rate >= 0 else math.nan)
                if any(math.isfinite(rate) for rate in rates):
                    axis.stairs(rates, edges, baseline=None, color=colour, label=role)
                else:
                    missing.append(role)
            if missing:
                axis.text(.01, .9, f"{', '.join(missing)}: no complete goodput intervals",
                          transform=axis.transAxes, va='top', fontsize='small')
            if axis.get_legend_handles_labels()[0]:
                axis.legend(loc='upper right', fontsize='small', frameon=False)
            axis.set_ylim(bottom=0)

        foreground.set_title('Foreground connections')
        background.set(title='Aggregate background traffic', xlabel='Time (s)', xlim=(0, edges[-1]))
        label = HEATMAP_LABELS.get(protocol, protocol).replace('\n', ' ')
        fig.suptitle(f'{label}, K={k}, run {run}')
        save_figure(fig, output / 'goodput')
        plt.close(fig)
