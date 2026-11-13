"""Per-flow summaries, goodput/raw-RTT heatmaps, and LEO fairness–efficiency plots."""
from collections import defaultdict
import csv
import json
import math
from pathlib import Path
import re
from statistics import mean, stdev
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import leo
from vectors import Vectors, interval_integral, seconds

HEATMAP_LABELS = {
    'Alpha': 'Alpha', 'Delta': 'Delta', 'Omega': 'Omega',
    'CubicUncoupled': 'CUBIC\nuncoupled', 'OrbUncoupled': 'OrbCC\nuncoupled',
    'OrbSingle': 'OrbCC\nsingle path', leo.OMEGA_10: 'Omega 10%',
}


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def checked_summary(config):
    path = leo.summary_path(config)
    data = json.loads(path.read_text())
    source = leo.result(config,'.vec')
    if (data['config'] != config or (source.is_file() and data['source_stat'] != leo.stat(source)) or
            data.get('input_hash') != leo.input_hash(config)):
        raise ValueError(f'Stale extraction: {path}')
    return data


def extract_ping(config):
    vectors = Vectors(leo.result(config, '.vec'))
    baseline, availability, rows = {}, {}, []
    for pair in leo.PAIRS:
        source = pair['source']
        for rank in range(leo.SHARED.PATH_COUNT + 1):
            module = next((v.module for v in vectors.items.values()
                if v.module.endswith(f'.userTerminal[{source}].app[{rank}]')), None)
            if module is None:
                raise ValueError(f'Missing ping application for {pair["pair"]}, rank {rank}')
            rtt = vectors.find(module, 'rtt')
            bins = seconds(vectors.samples(rtt[0]), 0, config['sim_time']) if rtt else {}
            key = f'{source}:{rank}'
            baseline[key] = {str(t): value for t, value in bins.items()}
            if rank:
                available = vectors.find(module, 'kPathAvailable')
                if len(available) != 1:
                    raise ValueError(f'Missing path availability: {module}')
                availability[key] = list(vectors.samples(available[0]))
                if not availability[key]:
                    raise ValueError(f'Empty path-availability vector: {module}')
            rows += [dict(pair=pair['pair'], rank=rank, second=t, baseline_rtt_ms=value*1000)
                     for t, value in bins.items()]
    data = dict(config=config, source_stat=leo.stat(leo.result(config, '.vec')), input_hash=leo.input_hash(config),
                baseline=baseline, availability=availability)
    target = leo.summary_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data) + '\n')
    write_csv(target.with_suffix('.csv'), rows)
    return data


def sender_ranks(vectors, source, k, single):
    # The ACTIVE side creates its meta connection, then all K slots synchronously
    # in ascending slot order (MpTcpFlowScheduler::createInitialSubflows).
    # ICMP exists only in the separate prepass. Never infer ranks from receiver
    # IDs, RTT order, or only the subflows that successfully exchanged data.
    pattern = re.compile(rf'\.userTerminal\[{source}\]\.tcp\.conn-(\d+)$')
    found = [(int(m.group(1)), v) for v in vectors.items.values()
             if v.metric == 'rtt' and (m := pattern.search(v.module))]
    found.sort(key=lambda x: x[0])
    expected = 1 if single else k + 1
    if len(found) != expected:
        raise ValueError(f'Cannot establish sender rank identity for terminal {source}: '
                         f'expected {expected} RTT vectors (including meta), got {len(found)}')
    chosen = found if single else found[1:]
    return {rank: v for rank, (_, v) in enumerate(chosen, 1)}


def availability_fraction(ping, source, k, start, end):
    changes = []
    for rank in range(1, k + 1):
        for t, value in ping['availability'][f'{source}:{rank}']:
            changes.append((t, rank, value))
    changes.sort()
    states, previous, area = {}, start, 0.
    for t, rank, value in changes:
        if t > end:
            break
        if t >= start:
            area += max(0., t - previous) * (len(states) == k and all(states.values()))
            previous = t
        states[rank] = value
    area += max(0., end - previous) * (len(states) == k and all(states.values()))
    return 100 * area / (end - start)


def coverage_seconds(intervals):
    """Union of receiver-thread coverage; overlapping threads count once."""
    total, previous = 0., -math.inf
    for start,end in sorted(intervals):
        total += max(0.,end-max(start,previous))
        previous = max(previous,end)
    return total


def extract(config, vectors=None):
    ping_config = leo.baseline_config(config)
    ping = checked_summary(ping_config)
    vectors = vectors if vectors is not None else Vectors(leo.result(config, '.vec'))
    start, end = config['measurement_start'], config['measurement_end']
    single = config['protocol'] == 'OrbSingle'
    rows, details, goodput_details = [], [], []
    for pair in leo.PAIRS:
        source, destination = pair['source'], pair['destination']
        goodput = [v for v in vectors.items.values() if v.metric == 'goodput' and
                   f'.userTerminal[{destination}].app[0].thread_' in v.module]
        goodput_bits, intervals = 0., []
        if not goodput:
            goodput_details.append(dict(config=config['config'],pair=pair['pair'],module='',
                recorded_bits=math.nan,covered_seconds=0.,covered_start_s='',covered_end_s='',status='not_recorded'))
        for vector in goodput:
            try:
                bits, covered, bounds = interval_integral(vectors.samples(vector),start,end,rate=True)
            except ValueError as error:
                raise ValueError(f'{config["config"]}: {vector.module}/goodput: {error}') from error
            goodput_bits += bits
            if bounds:
                intervals.append(bounds)
            goodput_details.append(dict(config=config['config'],pair=pair['pair'],module=vector.module,
                recorded_bits=bits,covered_seconds=covered,
                covered_start_s=bounds[0] if bounds else '',covered_end_s=bounds[1] if bounds else '',
                status='recorded' if bounds else 'no_covered_intervals'))
        goodput_coverage = coverage_seconds(intervals)
        # Keep the fixed measurement denominator, including time when a late or
        # closed thread emits nothing. Never extend a final nonzero rate.
        goodput_mean = goodput_bits/(end-start) if goodput else math.nan
        ranks = sender_ranks(vectors, source, config['k'], single)
        raw_by_second, excess_by_second = {}, {}
        base_by_second = {}
        matched = observed = 0
        for rank, vector in ranks.items():
            bins = seconds(vectors.samples(vector), start, end)
            base = ping['baseline'][f'{source}:{0 if single else rank}']
            matches = 0
            for t, value in bins.items():
                observed += 1
                raw_by_second.setdefault(t, []).append(value)
                if str(t) in base:
                    matched += 1
                    matches += 1
                    excess_by_second.setdefault(t, []).append(value - base[str(t)])
                    base_by_second.setdefault(t, []).append(base[str(t)])
            details.append(dict(config=config['config'], pair=pair['pair'], rank=rank,
                module=vector.module, observed_seconds=len(bins), matched_seconds=matches,
                rtt_ms=mean(bins.values())*1000 if bins else math.nan))
        raw = mean(mean(v) for v in raw_by_second.values())*1000 if raw_by_second else math.nan
        excess = mean(mean(v) for v in excess_by_second.values())*1000 if excess_by_second else math.nan
        base = mean(mean(v) for v in base_by_second.values())*1000 if base_by_second else math.nan
        rows.append(dict(config=config['config'], protocol=config['protocol'], k=config['k'],
            run=config['run'], pair=pair['pair'], goodput_mbps=goodput_mean/1e6,
            goodput_observed_pct=100*goodput_coverage/(end-start),
            goodput_complete=goodput_coverage>=end-start-1e-6,
            raw_rtt_ms=raw, excess_rtt_ms=excess, matched_baseline_rtt_ms=base,
            baseline_match_pct=100*matched/observed if observed else 0.,
            rtt_observed_pct=100*len(raw_by_second)/(end-start),
            all_k_available_pct=availability_fraction(ping, source, config['k'], start, end),
            receiver_connected=bool(goodput)))
    data = dict(config=config, source_stat=leo.stat(leo.result(config, '.vec')), input_hash=leo.input_hash(config),
                baseline_stat=leo.stat(leo.summary_path(ping_config)), rows=rows, subflows=details,
                goodput_recordings=goodput_details)
    target = leo.summary_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data) + '\n')
    write_csv(target.with_suffix('.csv'), rows)
    write_csv(target.with_name(target.stem + '_subflows.csv'), details)
    write_csv(target.with_name(target.stem + '_goodput_coverage.csv'),goodput_details)
    incomplete = [r['pair'] for r in rows if not r['goodput_complete']]
    if incomplete:
        print(f'{config["config"]}: incomplete goodput coverage for {", ".join(incomplete)}; '
              'using recorded traffic / full measurement duration; see *_goodput_coverage.csv',flush=True)
    return rows


def load_rows(configs):
    rows = []
    for config in configs:
        ping_config = leo.baseline_config(config)
        checked_summary(ping_config)
        data = checked_summary(config)
        if data['baseline_stat'] != leo.stat(leo.summary_path(ping_config)):
            raise ValueError(f'Changed ping baseline: re-extract {config["config"]}')
        rows += data['rows']
    return rows


def aggregate(rows, ks, protocols=leo.DEFAULT_PROTOCOLS):
    output = []
    for k in ks:
        for protocol in protocols:
            selected_k = 1 if protocol == 'OrbSingle' else k
            for pair in leo.PAIRS:
                selected = [r for r in rows if r['protocol'] == protocol and
                            r['k'] == selected_k and r['pair'] == pair['pair']]
                record = dict(k=k, protocol=protocol, pair=pair['pair'], n=len(selected))
                record['goodput_complete'] = bool(selected) and all(r.get('goodput_complete',True) for r in selected)
                for metric in ('goodput_mbps', 'goodput_observed_pct', 'raw_rtt_ms', 'excess_rtt_ms',
                               'baseline_match_pct', 'rtt_observed_pct', 'all_k_available_pct'):
                    values = [r[metric] for r in selected if math.isfinite(r.get(metric,math.nan))]
                    record[metric] = mean(values) if values else math.nan
                    record[metric + '_sd'] = stdev(values) if len(values) > 1 else math.nan
                    record[metric + '_n'] = len(values)
                valid_colour = [r['excess_rtt_ms'] for r in selected if
                    math.isfinite(r['excess_rtt_ms']) and r['baseline_match_pct'] >= 100*leo.MIN_BASELINE_MATCH]
                # All participating runs need adequate matching for a coloured cell.
                record['colour_excess_ms'] = mean(valid_colour) if selected and len(valid_colour) == len(selected) else math.nan
                output.append(record)
    return output


def mean_base_rtt(configs):
    """Equal-weight ping-path means, counting each pair/rank/window once."""
    requested = set()
    for config in configs:
        ranks = (0,) if config['protocol']=='OrbSingle' else range(1,config['k']+1)
        requested.update((config['sim_time'],config.get('replay_start',0),pair['source'],rank,
                          config['measurement_start'],config['measurement_end'])
                         for pair in leo.PAIRS for rank in ranks)
    ping, means = {}, []
    for duration,replay_start,source,rank,start,end in sorted(requested):
        key = duration,replay_start
        if key not in ping:
            ping[key] = checked_summary(leo.ping_config(duration,replay_start))
        values = [value for second,value in ping[key]['baseline'][f'{source}:{rank}'].items()
                  if start<=float(second)<end and math.isfinite(value) and value>0]
        if values:
            means.append(mean(values)*1000)
    return mean(means) if means else math.nan


def jain_fairness(rates):
    if not rates or any(not math.isfinite(rate) or rate < 0 for rate in rates) or not any(rates):
        return math.nan
    return sum(rates)**2 / (len(rates) * sum(rate**2 for rate in rates))


def connection_goodput(series, start, end):
    if not series:
        return math.nan, False
    bits, intervals = 0., []
    for samples in series:
        area, _, bounds = interval_integral(samples, start, end, rate=True)
        bits += area
        if bounds:
            intervals.append(bounds)
    complete = coverage_seconds(intervals) >= end - start - 1e-6
    return bits / (end - start) / 1e6, complete


def run_fairness_efficiency(config):
    from terminalVectors import CsvVectors, pair_vectors

    vectors = CsvVectors(leo.vector_dir(config), config)
    native = leo.result(config, '.vec')
    if ((native.is_file() and leo.stat(native) != vectors.manifest['source_stat']) or
            leo.input_hash(config) != vectors.manifest['input_hash']):
        raise ValueError(f'{config["config"]}: stale terminal CSVs; re-extract step 3')
    series = [[list(vectors.samples(vector)) for vector, _, _ in pair_vectors(vectors, config, pair, 'goodput')]
              for pair in leo.PAIRS]
    start, end = config['measurement_start'], config['measurement_end']
    rates, coverage = zip(*(connection_goodput(samples, start, end) for samples in series))
    scores = []
    missing = all_zero = 0
    # Integrate each receiver over the same complete seconds. A rate sample
    # describes the interval ending at its timestamp.
    for second in range(math.ceil(start), math.floor(end)):
        rates_in_bin, covered = zip(*(connection_goodput(samples, second, second + 1) for samples in series))
        if not all(covered):
            missing += 1
        elif not any(rates_in_bin):
            all_zero += 1
        else:
            score = jain_fairness(rates_in_bin)
            if math.isfinite(score):
                scores.append(score)
            else:
                missing += 1
    return dict(jain_fairness=jain_fairness(rates), aggregate_goodput_mbps=sum(rates),
                goodput_complete=all(coverage), mean_per_second_jain=mean(scores) if scores else math.nan,
                seconds_with_jain=len(scores), seconds_all_zero=all_zero, seconds_missing_goodput=missing)


def fairness_efficiency(configs, ks, protocols):
    """Average per-run metrics over the requested start-time patterns."""
    # Each connection shares its 100 Mbps access link across all of its subflows.
    capacity_mbps = 100.0 * len(leo.PAIRS)
    by_case = defaultdict(list)
    for config in configs:
        by_case[config['protocol'], config['k']].append(run_fairness_efficiency(config))

    points, paired_runs = [], {}
    for k in ks:
        for protocol in protocols:
            runs = by_case[protocol, 1 if protocol == 'OrbSingle' else k]
            if not runs:
                continue
            per_second = [run['mean_per_second_jain'] for run in runs if run['seconds_with_jain']]
            whole_run = [run['jain_fairness'] for run in runs if math.isfinite(run['jain_fairness'])]
            totals = [run['aggregate_goodput_mbps'] for run in runs if math.isfinite(run['aggregate_goodput_mbps'])]
            goodput = mean(totals) if totals else math.nan
            paired_runs[k, protocol] = [
                (run['mean_per_second_jain'], run['aggregate_goodput_mbps'] / capacity_mbps)
                for run in runs if run['seconds_with_jain'] and math.isfinite(run['aggregate_goodput_mbps'])
            ]
            points.append(dict(k=k, protocol=protocol, runs_selected=len(runs), runs_plotted=len(per_second),
                runs_with_goodput=len(totals),
                runs_with_partial_goodput=sum(not run['goodput_complete'] for run in runs),
                mean_per_second_jain=mean(per_second) if per_second else math.nan,
                jain_fairness=mean(whole_run) if whole_run else math.nan,
                seconds_with_jain=sum(run['seconds_with_jain'] for run in runs),
                seconds_all_zero=sum(run['seconds_all_zero'] for run in runs),
                seconds_missing_goodput=sum(run['seconds_missing_goodput'] for run in runs),
                aggregate_goodput_mbps=goodput, access_ceiling_mbps=capacity_mbps,
                normalized_goodput=goodput / capacity_mbps))
    return points, paired_runs


def plot_fairness_efficiency(configs, ks, protocols, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Ellipse
    from matplotlib.ticker import MaxNLocator
    import numpy as np
    from experiments import PROTOCOLS as terrestrial_protocols
    from plotStyle import plot_style, save_figure

    points, paired_runs = fairness_efficiency(configs, ks, protocols)
    for point in points:
        omitted = point['runs_selected'] - point['runs_plotted']
        if omitted:
            print(f'{point["protocol"]} K={point["k"]}: {omitted} runs lack a usable per-second Jain score',
                  flush=True)
    visible = [point for point in points if point['runs_plotted'] and math.isfinite(point['normalized_goodput'])]
    write_csv(output / 'fairness_efficiency_data.csv', points)
    # Keep the same scales across K, including the spread around each mean.
    xmin, xmax, ymin, ymax = .95, 1.01, .85, 1.025
    covariance_by_case = {}
    for point in visible:
        case = point['k'], point['protocol']
        samples = paired_runs[case]
        x, y = point['mean_per_second_jain'], point['normalized_goodput']
        sx = sy = 0.
        if len(samples) > 1 and np.any(np.ptp(samples, axis=0) > 0):
            covariance = np.cov(samples, rowvar=False)
            covariance_by_case[case] = covariance
            sx, sy = np.sqrt(np.diag(covariance))
        xmin, xmax = min(xmin, x - sx - .01), max(xmax, x + sx + .01)
        ymin, ymax = min(ymin, y - sy - .03), max(ymax, y + sy + .025)
    colours = {protocol: values[3] for protocol, values in terrestrial_protocols.items()}
    colours.update({leo.OMEGA_10: '#7851A9', 'OrbSingle': '#333333'})
    for k in ks:
        with plot_style(aggregate=True), plt.rc_context({'text.usetex': False}):
            fig, ax = plt.subplots(figsize=(5, 3), layout='constrained')
            for point in visible:
                if point['k'] != k:
                    continue
                protocol = point['protocol']
                label = HEATMAP_LABELS[protocol].replace('\n', ' ')
                if protocol == 'Omega' and leo.OMEGA_10 in protocols:
                    label = 'Omega 5%'
                x, y = point['mean_per_second_jain'], point['normalized_goodput']
                colour = colours[protocol]
                covariance = covariance_by_case.get((k, protocol))
                if covariance is not None:
                    variances, axes = np.linalg.eigh(covariance)
                    height, width = 2 * np.sqrt(np.maximum(variances, 0))
                    angle = math.degrees(math.atan2(axes[1, 1], axes[0, 1]))
                    ax.add_patch(Ellipse((x, y), width, height, angle=angle,
                                         facecolor=colour, edgecolor=colour, alpha=.22, linewidth=.8))
                ax.scatter(x, y, s=48, marker='o', facecolors='none', edgecolors=colour,
                           linewidths=1.4, label=label, zorder=3)
            if ax.collections:
                ax.legend(loc='lower center', bbox_to_anchor=(.5, 1.02), ncol=3, fontsize=8)
            else:
                ax.text(.5, .5, 'No usable runs', transform=ax.transAxes, ha='center', va='center')
            ax.set(xlabel='Mean 1 s Jain Fairness', ylabel='Normalized Aggregate Goodput',
                   xlim=(max(0., xmin), xmax), ylim=(max(0., ymin), ymax))
            ax.xaxis.set_major_locator(MaxNLocator(nbins=6))
            ax.yaxis.set_major_locator(MaxNLocator(nbins=6))
            ax.grid(alpha=.35)
            fig.suptitle(f'LEO load balance, K={k}', fontsize=10)
            save_figure(fig, output / f'fairness_efficiency_K{k}', aggregate=True)
            plt.close(fig)


def plot(configs, *, fairness_efficiency_only=False):
    sys.path.insert(0, str(leo.SCRIPT_DIR.parent))
    variant = next((config for config in configs if config['protocol'] == leo.OMEGA_10), None)
    ks = sorted({c['k'] for c in configs if c['protocol'] != 'OrbSingle'}) or [1]
    protocols = tuple(leo.PROTOCOLS) if variant else leo.DEFAULT_PROTOCOLS
    output = leo.plot_directory(variant or configs[0])
    output.mkdir(parents=True, exist_ok=True)
    if configs[0].get('replay_start'):
        plot_fairness_efficiency(configs, ks, protocols, output)
    if fairness_efficiency_only:
        return

    try:
        rows = load_rows(configs)
    except (FileNotFoundError, ValueError) as error:
        if variant:
            raise ValueError(f'Omega10 summary plots require matching extracted results for all controllers. {error}') from error
        raise

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.ticker import FormatStrFormatter
    import numpy as np
    from plotStyle import plot_style, save_figure

    cells = aggregate(rows, ks, protocols)
    write_csv(output / 'runs.csv', rows)
    write_csv(output / 'heatmap_data.csv', cells)
    baseline = mean_base_rtt(configs)
    observed = [r['raw_rtt_ms'] for r in cells if math.isfinite(r['raw_rtt_ms'])]
    delay_min = baseline if math.isfinite(baseline) else 0.
    delay_max = max(observed,default=delay_min)
    # A flat/missing data set still needs a non-degenerate plotting interval.
    delay_max = delay_max if delay_max>delay_min else delay_min+1.
    write_csv(output/'heatmap_scales.csv',[
        dict(metric='goodput_mbps',minimum=0.,maximum=100.,mean_base_rtt_ms=''),
        dict(metric='raw_rtt_ms',minimum=delay_min,maximum=delay_max,mean_base_rtt_ms=baseline)])
    for k in ks:
        subset = {(r['protocol'], r['pair']): r for r in cells if r['k'] == k}
        for metric, name in [('goodput_mbps', 'goodput'), ('raw_rtt_ms', 'delay')]:
            annotations = np.full((len(leo.PAIRS),len(protocols)),np.nan)
            for i, protocol in enumerate(protocols):
                for j, pair in enumerate(leo.PAIRS):
                    cell = subset[protocol, pair['pair']]
                    annotations[j,i] = cell[metric]
            colours = annotations.copy()
            if name=='delay' and not math.isfinite(baseline):
                colours[:] = np.nan
            with plot_style(aggregate=True), plt.rc_context({'text.usetex':False,'font.family':'serif',
                    'font.weight':'bold','axes.labelweight':'bold','axes.titleweight':'bold','font.size':8}):
                fig, ax = plt.subplots(figsize=(7.3 if variant else 6.6,3.1),layout='constrained')
                palette = ['green','yellow','red'] if name=='delay' else ['red','yellow','green']
                cmap = LinearSegmentedColormap.from_list(name,palette).with_extremes(bad='#e6e6e6')
                image = ax.imshow(np.ma.masked_invalid(colours), aspect='auto', cmap=cmap,
                                  interpolation='nearest',vmin=delay_min if name=='delay' else 0,
                                  vmax=delay_max if name=='delay' else 100)
                for i,pair in enumerate(leo.PAIRS):
                    for j,protocol in enumerate(protocols):
                        value = annotations[i,j]
                        label = f'{value:.1f}' if math.isfinite(value) else '--'
                        cell = subset[protocol,pair['pair']]
                        incomplete = name=='goodput' and not cell['goodput_complete']
                        if math.isfinite(value) and incomplete:
                            label += '*'
                        ax.text(j,i,label,ha='center',va='center',color='black',fontsize=8.5)
                labels = ['Omega 5%' if variant and p == 'Omega' else HEATMAP_LABELS[p] for p in protocols]
                ax.set_xticks(range(len(protocols)), labels, fontsize=8)
                ax.set_yticks(range(len(leo.PAIRS)),
                    [p['source_city']+' to '+p['destination_city'] for p in leo.PAIRS],fontsize=8)
                ax.tick_params(which='both', bottom=False, top=False, left=False, right=False)
                ax.set_title(f'{"RTT" if name=="delay" else "Goodput"}, K={k}',fontsize=10)
                if name!='delay' or math.isfinite(baseline):
                    colorbar = fig.colorbar(image,ax=ax,label='RTT (ms)' if name=='delay' else 'Goodput (Mbps)',pad=.025)
                    ticks = np.linspace(delay_min,delay_max,3) if name=='delay' else [0,50,100]
                    colorbar.set_ticks(ticks)
                    colorbar.ax.yaxis.set_major_formatter(FormatStrFormatter('%.1f' if name=='delay' else '%.0f'))
                    colorbar.ax.tick_params(labelsize=8)
                save_figure(fig, output / f'{name}_K{k}', aggregate=True)
                plt.close(fig)
    return cells


if __name__ == '__main__':
    manifest = json.loads((leo.EXPERIMENT_DIR / 'matrix.json').read_text())
    plot(manifest['configurations'])
