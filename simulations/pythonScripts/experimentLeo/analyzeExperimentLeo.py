"""Per-flow goodput and RTT; RTT labels use raw delay, colours use excess delay."""
import csv
import json
import math
from pathlib import Path
import re
from statistics import mean, stdev
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import leo
from vectors import Vectors, interval_mean, seconds


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
    if (data['config'] != config or data['source_stat'] != leo.stat(leo.result(config, '.vec')) or
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


def extract(config):
    ping_config = leo.ping_config(config['sim_time'])
    ping = checked_summary(ping_config)
    vectors = Vectors(leo.result(config, '.vec'))
    start, end = config['measurement_start'], config['measurement_end']
    single = config['protocol'] == 'OrbSingle'
    rows, details = [], []
    for pair in leo.PAIRS:
        source, destination = pair['source'], pair['destination']
        goodput = [v for v in vectors.items.values() if v.metric == 'goodput' and
                   f'.userTerminal[{destination}].app[0].thread_' in v.module]
        goodput_mean = sum(interval_mean(vectors.samples(v), start, end, rate=True) for v in goodput)
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
            raw_rtt_ms=raw, excess_rtt_ms=excess, matched_baseline_rtt_ms=base,
            baseline_match_pct=100*matched/observed if observed else 0.,
            rtt_observed_pct=100*len(raw_by_second)/(end-start),
            all_k_available_pct=availability_fraction(ping, source, config['k'], start, end),
            receiver_connected=bool(goodput)))
    data = dict(config=config, source_stat=leo.stat(leo.result(config, '.vec')), input_hash=leo.input_hash(config),
                baseline_stat=leo.stat(leo.summary_path(ping_config)), rows=rows, subflows=details)
    target = leo.summary_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data) + '\n')
    write_csv(target.with_suffix('.csv'), rows)
    write_csv(target.with_name(target.stem + '_subflows.csv'), details)
    return rows


def load_rows(configs):
    rows = []
    for config in configs:
        checked_summary(leo.ping_config(config['sim_time']))
        data = checked_summary(config)
        if data['baseline_stat'] != leo.stat(leo.summary_path(leo.ping_config(config['sim_time']))):
            raise ValueError(f'Changed ping baseline: re-extract {config["config"]}')
        rows += data['rows']
    return rows


def aggregate(rows, ks):
    output = []
    for k in ks:
        for protocol in leo.PROTOCOLS:
            selected_k = 1 if protocol == 'OrbSingle' else k
            for pair in leo.PAIRS:
                selected = [r for r in rows if r['protocol'] == protocol and
                            r['k'] == selected_k and r['pair'] == pair['pair']]
                record = dict(k=k, protocol=protocol, pair=pair['pair'], n=len(selected))
                for metric in ('goodput_mbps', 'raw_rtt_ms', 'excess_rtt_ms',
                               'baseline_match_pct', 'rtt_observed_pct', 'all_k_available_pct'):
                    values = [r[metric] for r in selected if math.isfinite(r[metric])]
                    record[metric] = mean(values) if values else math.nan
                    record[metric + '_sd'] = stdev(values) if len(values) > 1 else math.nan
                    record[metric + '_n'] = len(values)
                valid_colour = [r['excess_rtt_ms'] for r in selected if
                    math.isfinite(r['excess_rtt_ms']) and r['baseline_match_pct'] >= 100*leo.MIN_BASELINE_MATCH]
                # All participating runs need adequate matching for a coloured cell.
                record['colour_excess_ms'] = mean(valid_colour) if selected and len(valid_colour) == len(selected) else math.nan
                output.append(record)
    return output


def plot(configs):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    sys.path.insert(0, str(leo.SCRIPT_DIR.parent))
    from plotStyle import plot_style, save_figure
    rows = load_rows(configs)
    ks = sorted({c['k'] for c in configs if c['protocol'] != 'OrbSingle'}) or [1]
    cells = aggregate(rows, ks)
    leo.PLOT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(leo.PLOT_DIR / 'runs.csv', rows)
    write_csv(leo.PLOT_DIR / 'heatmap_data.csv', cells)
    delay_max = max([1., *(r['colour_excess_ms'] for r in cells if math.isfinite(r['colour_excess_ms']))])
    for k in ks:
        subset = {(r['protocol'], r['pair']): r for r in cells if r['k'] == k}
        for metric, name in [('goodput_mbps', 'goodput'), ('raw_rtt_ms', 'delay')]:
            annotations = np.full((len(leo.PROTOCOLS), len(leo.PAIRS)), np.nan)
            colours = annotations.copy()
            for i, protocol in enumerate(leo.PROTOCOLS):
                for j, pair in enumerate(leo.PAIRS):
                    cell = subset[protocol, pair['pair']]
                    annotations[i,j] = cell[metric]
                    colours[i,j] = cell['colour_excess_ms'] if name == 'delay' else cell[metric]
            with plot_style(aggregate=True):
                fig, ax = plt.subplots(figsize=(10, 4.5), layout='constrained')
                cmap = plt.get_cmap('RdYlGn_r' if name == 'delay' else 'RdYlGn').with_extremes(bad='#e6e6e6')
                image = ax.imshow(np.ma.masked_invalid(colours), aspect='auto', cmap=cmap,
                                  vmin=0, vmax=delay_max if name == 'delay' else 100)
                for i in range(len(leo.PROTOCOLS)):
                    for j in range(len(leo.PAIRS)):
                        value, colour_value = annotations[i,j], colours[i,j]
                        label = f'{value:.1f}' if math.isfinite(value) else '--'
                        if math.isfinite(value) and not math.isfinite(colour_value):
                            label += '*'
                        rgba = image.cmap(image.norm(colour_value)) if math.isfinite(colour_value) else (.9,.9,.9,1)
                        brightness = sum(a*b for a,b in zip(rgba[:3], (.2126,.7152,.0722)))
                        ax.text(j,i,label,ha='center',va='center',color='black' if brightness>.5 else 'white')
                ax.set_xticks(range(len(leo.PAIRS)), [p['source_city']+'\n'+p['destination_city'] for p in leo.PAIRS], fontsize=9)
                ax.set_yticks(range(len(leo.PROTOCOLS)), [p[2] for p in leo.PROTOCOLS.values()], fontsize=9)
                ax.tick_params(which='both', bottom=False, top=False, left=False, right=False)
                ax.set_title(f'LEO: {k} requested subflow{"s" if k != 1 else ""} | ' +
                             ('Raw RTT (ms)' if name == 'delay' else 'Application goodput (Mbps)'))
                fig.colorbar(image, ax=ax, label='Excess RTT above matching ping (ms)' if name == 'delay' else 'Goodput (Mbps)')
                counts = sorted({subset[p, pair['pair']]['n'] for p in leo.PROTOCOLS for pair in leo.PAIRS if subset[p,pair['pair']]['n']})
                footer = 'Five simultaneous connections; mean across ' + '/'.join(map(str,counts)) + ' start-time runs. Single-path row reused.'
                if name == 'delay':
                    footer += '\nNumbers: raw RTT. Colours: excess RTT. Grey*: baseline match below 95%.'
                if plt.rcParams['text.usetex']:
                    footer = footer.replace('%',r'\%')
                fig.supxlabel(footer,fontsize=8)
                save_figure(fig, leo.PLOT_DIR / f'{name}_K{k}', aggregate=True)
                plt.close(fig)
    return cells


if __name__ == '__main__':
    manifest = json.loads((leo.EXPERIMENT_DIR / 'matrix.json').read_text())
    plot(manifest['configurations'])
