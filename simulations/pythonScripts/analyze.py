"""Bounded extraction of the OrbTCP baseline vectors and two MPTCP essentials."""
import csv
import itertools
import json
import math
import mmap
from pathlib import Path
import re
import tempfile

import numpy as np
from experiments import SIM_DIR, PROTOCOLS, TCP_METRICS, QUEUE_METRICS, recorded_metrics, directory
from streamScaveCsv import vector_records, tokens
from plotStyle import CDF_SIZE, RTT_SIZE, MARKERS, plot_style, save_figure

BLOCK = 65536
RECEIVER_THREAD = re.compile(r'\.server\[(\d+)\]\.app\[0\]\.thread_\d+$')
RECEIVER_APP = re.compile(r'\.server\[(\d+)\]\.app\[0\]$')
TCP_CONNECTION = re.compile(r'\.(client|server)\[\d+\]\.tcp\.conn-\d+$')
SENDER_RTT = re.compile(r'\.client\[0\]\.tcp\.conn-\d+$')
CORE_QUEUE = re.compile(r'\.(left|right)\[[01]\]\.ppp\[0\]\.queue$')


def accepted_vector(module, metric, number):
    if metric not in recorded_metrics(number):
        return False
    if metric == 'goodput':
        return RECEIVER_THREAD.search(module) is not None
    if metric in QUEUE_METRICS:
        return CORE_QUEUE.search(module) is not None
    match = TCP_CONNECTION.search(module)
    if not match:
        return False
    if metric == 'holBlockedBytes':
        return match[1] == 'server'
    if metric == 'metaReinjectedBytes':
        return match[1] == 'client'
    return metric in TCP_METRICS


def discard_pages(*arrays):
    for array in arrays:
        mapping = getattr(array, '_mmap', None)
        if mapping is not None and hasattr(mapping, 'madvise') and hasattr(mmap, 'MADV_DONTNEED'):
            mapping.madvise(mmap.MADV_DONTNEED)


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def vector_arrays(spools, temporary):
    arrays = []
    for field in ('vectime', 'vecvalue'):
        target = Path(temporary) / f'{field}.bin'
        count = 0
        with target.open('wb') as output:
            for_values = tokens(spools[field]) if field in spools else iter(())
            while batch := list(itertools.islice(for_values, BLOCK)):
                values = np.fromstring(b' '.join(batch), sep=' ')
                if len(values) != len(batch):
                    raise ValueError('Malformed numeric vector')
                values.tofile(output)
                count += len(values)
        arrays.append(np.memmap(target, dtype=float, mode='r') if count else np.array([]))
    if len(arrays[0]) != len(arrays[1]):
        raise ValueError('Vector timestamps and values have different lengths')
    for offset in range(0, len(arrays[0]), BLOCK):
        if np.any(np.diff(arrays[0][max(0, offset-1):offset+BLOCK]) < 0):
            raise ValueError('Vector timestamps are not ordered')
        discard_pages(arrays[0])
    return arrays


def rate_integral(times, values, start, end):
    """A receiver rate describes the interval ending at its timestamp."""
    total = 0.
    for offset in range(0, len(times)-1, BLOCK):
        stop = min(offset + BLOCK, len(times)-1)
        widths = np.maximum(0, np.minimum(times[offset+1:stop+1], end) - np.maximum(times[offset:stop], start))
        total += float(np.sum(widths * values[offset+1:stop+1]))
        discard_pages(times, values)
    return total


def available_capacity(records, grid):
    paths = np.zeros((2, len(grid)))
    for record in records:
        path, down, up = record['path'], record['time'], record['up_time']
        paths[path, grid >= down] = 0
        paths[path, grid >= up] = record['bandwidth_mbps']
    return paths


def extract(config):
    root = directory(config['experiment'])
    raw = root / 'csvs/raw' / f'{config["config"]}.csv'
    out = root / 'csvs/extracted' / config['config']
    out.mkdir(parents=True, exist_ok=True)
    count = config['connections']
    start, end = config['measurement_start'], config['measurement_end']
    if end <= start:
        raise ValueError('Measurement window is empty')
    grid = np.arange(0, config['sim_time'], .1)
    traces = np.zeros((count, len(grid)))
    means = np.zeros(count)
    bytes_received = [None] * count
    rates_found, late = [0] * count, [False] * count
    seen = set()
    written_vectors = set()
    def accepted(row):
        return (row.get('type') == 'vector' and accepted_vector(row.get('module', ''),
                row.get('name', '').split(':')[0], config['experiment'])) or (
            row.get('type') == 'scalar' and row.get('name') == 'bytesRcvd' and
            RECEIVER_APP.search(row.get('module', '')) is not None)
    with tempfile.TemporaryDirectory(prefix='mporb-extract-') as temporary:
        for row, spools, wide in vector_records(raw, accepted):
            module, metric = row['module'], row['name'].split(':')[0]
            if row['type'] == 'scalar':
                match = RECEIVER_APP.search(module)
                if match:
                    index = int(match[1])
                    value = float(row['value'])
                    if index >= count or bytes_received[index] is not None or not math.isfinite(value) or value < 0:
                        raise ValueError('Invalid or duplicate application byte counter')
                    bytes_received[index] = value
                continue
            if not wide or (module, metric) in seen:
                raise ValueError('Expected one CSV-R vector per original module/metric')
            seen.add((module, metric))
            times, values = vector_arrays(spools, temporary)
            filename = re.sub(r'[^A-Za-z0-9_.\[\]-]', '_', f'{module}__{metric}') + '.csv'
            written_vectors.add(filename)
            with (out / filename).open('w') as handle:
                handle.write('time,value\n')
                for offset in range(0, len(times), BLOCK):
                    np.savetxt(handle, np.column_stack((times[offset:offset+BLOCK], values[offset:offset+BLOCK])),
                               delimiter=',', fmt='%.12g')
                    discard_pages(times, values)
            match = RECEIVER_THREAD.search(module)
            if metric == 'goodput' and match:
                index = int(match[1])
                for offset in range(0, len(values), BLOCK):
                    if np.any(~np.isfinite(values[offset:offset+BLOCK])) or np.any(values[offset:offset+BLOCK] < 0):
                        raise ValueError('Invalid goodput vector')
                    discard_pages(values)
                rates_found[index] += 1
                late[index] |= bool(len(times) and times[0] > start)
                means[index] += rate_integral(times, values, start, end) / (end - start) / 1e6
                if len(times):
                    indices = np.searchsorted(times, grid, side='left')
                    valid = (indices < len(times)) & (grid >= times[0])
                    traces[index, valid] += values[indices[valid]] / 1e6
            discard_pages(times, values)
            del times, values
    if any(value is None for value in bytes_received):
        raise ValueError(f'{config["config"]}: missing receiver byte counter')
    if any(delivered > 0 and not vectors for delivered, vectors in zip(bytes_received, rates_found)):
        raise ValueError('Receiver bytes exist without an application goodput vector')
    means[np.asarray(late)] = math.nan
    total = float(np.sum(means))
    jain = total**2 / (count * float(np.sum(means**2))) if np.all(np.isfinite(means)) and np.sum(means**2) > 0 else math.nan
    columns = {'time': grid, **{f'connection_{i}_goodput_mbps': traces[i] for i in range(count)}}
    if config['experiment'] <= 2:
        records = json.loads((root / 'scenarios' / f'run{config["run"]}.json').read_text())
        capacity = available_capacity(records, grid)
        columns.update(path_0_capacity_mbps=capacity[0], path_1_capacity_mbps=capacity[1])
        columns['normalized_goodput'] = np.divide(np.sum(traces, axis=0), np.sum(capacity, axis=0),
            out=np.full(len(grid), np.nan), where=np.sum(capacity, axis=0) > 0)
    else:
        columns['normalized_goodput'] = np.sum(traces, axis=0) / 200
    np.savetxt(out / 'timeseries.csv', np.column_stack(list(columns.values())), delimiter=',',
               header=','.join(columns), comments='', fmt='%.12g')
    connections = [dict(connection=i, goodput_mbps=float(means[i]), bytes_received=bytes_received[i],
        full_run_goodput_mbps=bytes_received[i] * 8 / (config['sim_time'] - config['starts'][i]) / 1e6,
        late_goodput_start=late[i]) for i in range(count)]
    write_csv(out / 'connections.csv', connections)
    for old in out.glob('*__*.csv'):
        if old.name not in written_vectors:
            old.unlink()  # Remove obsolete derived vectors only after successful extraction.
    info = raw.stat()
    summary = dict(config, raw_stat=[info.st_size, info.st_mtime_ns], goodput_mbps=means.tolist(),
        total_goodput_mbps=total, jain=jain,
        b_over_a=float(means[1] / means[0]) if count == 2 and means[0] > 0 else math.nan)
    (out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def write_summaries(summaries):
    for number in sorted({s['experiment'] for s in summaries}):
        rows = [s for s in summaries if s['experiment'] == number]
        write_csv(directory(number) / 'csvs/summary.csv', rows)


def read_summary(config):
    root = directory(config['experiment'])
    path = root / 'csvs/extracted' / config['config'] / 'summary.json'
    if not path.is_file():
        return None
    summary = json.loads(path.read_text())
    raw = root / 'csvs/raw' / f'{config["config"]}.csv'
    valid = (all(summary.get(key) == value for key, value in config.items()) and raw.is_file() and
             summary['raw_stat'] == [raw.stat().st_size, raw.stat().st_mtime_ns])
    if not valid:
        raise ValueError(f'Stale or mismatched extraction for {config["config"]}')
    return summary


def aggregate_summaries(configs):
    """Load only the selected experiments and require matching measurement windows."""
    summaries = {}
    for config in configs:
        summary = read_summary(config)
        if summary is not None:
            summaries[(config['experiment'], config['config'])] = summary
    values = list(summaries.values())
    if not values:
        raise ValueError('No extracted results to plot')
    windows = {(s['sim_time'], s['measurement_start'], s['measurement_end']) for s in values}
    if len(windows) != 1:
        raise ValueError('Aggregate runs have different durations/windows; select one --sim-time')
    return values


def average_capacity(records, start, end):
    """Time-weighted sum of both paths' raw capacity, including outage intervals."""
    if end <= start:
        raise ValueError('Capacity measurement window is empty')
    integral = 0.
    for path in range(2):
        events = {}
        for row in records:
            if row['path'] == path:
                events[row['time']] = 0.
                events[row['up_time']] = row['bandwidth_mbps']
        ordered = sorted(events.items())
        for index, (time, value) in enumerate(ordered):
            next_time = ordered[index + 1][0] if index + 1 < len(ordered) else end
            integral += max(0., min(end, next_time) - max(start, time)) * value
    return integral / (end - start)


def average_base_rtt(records, start, end):
    """Equal-path propagation RTT mean; outages contribute no RTT observations."""
    means = []
    for path in range(2):
        rows = sorted((r for r in records if r['path'] == path), key=lambda r: r['time'])
        integral = duration = 0.
        for index, row in enumerate(rows):
            next_down = rows[index + 1]['time'] if index + 1 < len(rows) else end
            width = max(0., min(end, next_down) - max(start, row['up_time']))
            integral += width * row['rtt_ms']
            duration += width
        means.append(integral / duration if duration > 0 else math.nan)
    return float(np.mean(means))


def rtt_observation(summary):
    """OrbTCP's per-second averaging, with equal weight per sending subflow."""
    root = directory(summary['experiment'])
    out = root / 'csvs/extracted' / summary['config']
    subflows = []
    for path in sorted(out.glob('*__rtt.csv')):
        module = path.stem.removesuffix('__rtt')
        if not SENDER_RTT.search(module):
            continue
        seconds = {}
        with path.open() as handle:
            rows = csv.DictReader(handle)
            for row in rows:
                time, value = float(row['time']), float(row['value'])
                if not (math.isfinite(time) and math.isfinite(value) and value > 0 and
                        summary['measurement_start'] <= time < summary['measurement_end']):
                    continue
                bucket = seconds.setdefault(math.floor(time), [0., 0])
                bucket[0] += value
                bucket[1] += 1
        if seconds:
            subflows.append(dict(experiment=summary['experiment'], config=summary['config'],
                protocol=summary['protocol'], run=summary['run'], module=module,
                average_rtt_ms=float(np.mean([total / count for total, count in seconds.values()])) * 1000,
                seconds=len(seconds), samples=sum(count for _, count in seconds.values())))
    # Empty meta-level RTT vectors and receiver handshake RTTs never contribute.
    # An incomplete or ambiguous set of sending subflows is reported as missing.
    included = len(subflows) == summary['subflows']
    records = json.loads((root / 'scenarios' / f'run{summary["run"]}.json').read_text())
    row = dict(experiment=summary['experiment'], config=summary['config'], protocol=summary['protocol'],
        run=summary['run'], seed=summary['seed'],
        average_rtt_ms=float(np.mean([r['average_rtt_ms'] for r in subflows])) if included else math.nan,
        base_rtt_ms=average_base_rtt(records, summary['measurement_start'], summary['measurement_end']),
        observed_subflows=len(subflows), expected_subflows=summary['subflows'],
        samples=sum(r['samples'] for r in subflows), included=included)
    return row, subflows


def cdf_points(values):
    ordered = np.sort([value for value in values if math.isfinite(value)])
    if not len(ordered):
        return np.array([]), np.array([])
    return np.r_[ordered[0], ordered], np.r_[0., np.arange(1, len(ordered) + 1) / len(ordered) * 100]


def ratio_observation(summary):
    """Mean of aligned min/max ratios, not min/max of the two overall means."""
    path = directory(summary['experiment']) / 'csvs/extracted' / summary['config'] / 'timeseries.csv'
    trace = np.atleast_1d(np.genfromtxt(path, delimiter=',', names=True))
    first, second = trace['connection_0_goodput_mbps'], trace['connection_1_goodput_mbps']
    window = (trace['time'] > summary['measurement_start']) & (trace['time'] <= summary['measurement_end'])
    finite = np.isfinite(first) & np.isfinite(second) & (first >= 0) & (second >= 0)
    maximum = np.maximum(first, second)
    valid = window & finite & (maximum > 0)
    ratios = np.minimum(first[valid], second[valid]) / maximum[valid]
    if not np.all(np.isfinite(summary['goodput_mbps'])):
        ratios = np.array([])
    return dict(experiment=summary['experiment'], config=summary['config'], protocol=summary['protocol'],
                layout=summary['layout'], rtt_ms=summary['rtt_ms'], run=summary['run'],
                goodput_ratio=float(np.mean(ratios)) if len(ratios) else math.nan,
                samples=len(ratios), both_idle_samples=int(np.count_nonzero(window & finite & (maximum == 0))))


@plot_style(aggregate=True, cdf=True)
def plot_cdf(summaries):
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    number = summaries[0]['experiment']
    output = SIM_DIR / 'plots' / f'experiment{number}'
    output.mkdir(parents=True, exist_ok=True)
    observations, references = [], {}
    for summary in summaries:
        mean = summary['goodput_mbps'][0]
        records = json.loads((directory(summary['experiment']) / 'scenarios' / f'run{summary["run"]}.json').read_text())
        capacity = average_capacity(records, summary['measurement_start'], summary['measurement_end'])
        observations.append(dict(experiment=summary['experiment'], config=summary['config'], protocol=summary['protocol'],
            run=summary['run'], seed=summary['seed'], average_goodput_mbps=mean, average_capacity_mbps=capacity,
            included=math.isfinite(mean)))
        if math.isfinite(mean):
            references[(summary['run'], summary['seed'], capacity)] = capacity
    if not any(row['included'] for row in observations):
        raise ValueError(f'Experiment {number}: no valid per-run goodput averages for the CDF')
    fig = plt.figure(figsize=CDF_SIZE)
    ax = fig.add_axes([.16, .28, .82, .57])
    present = set()
    for protocol, (_, _, label, color) in PROTOCOLS.items():
        values = [r['average_goodput_mbps'] for r in observations if r['protocol'] == protocol]
        x, y = cdf_points(values)
        if len(x):
            ax.step(x, y, where='post', color=color)
            present.add(protocol)
    x, y = cdf_points(references.values())
    ax.step(x, y, where='post', color='black')
    controller_handles = [Line2D([], [], color=PROTOCOLS[p][3], label=PROTOCOLS[p][2]) for p in PROTOCOLS if p in present]
    controller_handles.append(Line2D([], [], color='black', label='Capacity reference'))
    ax.legend(handles=controller_handles, loc='upper left', ncols=3, fontsize=7,
              columnspacing=.8, handletextpad=.5)
    ax.set(xlabel='Average Goodput (Mbps)',
           ylabel=r'\% of Trials' if plt.rcParams['text.usetex'] else '% of Trials',
           ylim=(0, 105), yticks=[0, 50, 100])
    ax.set_title('Responsiveness' if number == 1 else 'Responsiveness with Loss')
    save_figure(fig, output / 'goodput_cdf', aggregate=True)
    plt.close(fig)
    write_csv(output / 'plot_data.csv', observations)
    write_csv(output / 'capacity_reference.csv', [dict(run=run, seed=seed, average_capacity_mbps=value)
        for (run, seed, _), value in sorted(references.items())])
    # Retire earlier generated comparisons; individual plots remain.
    for name in ('responsiveness.png', 'responsiveness.pdf'):
        (output / name).unlink(missing_ok=True)
    for name in ('joined_goodput_cdf.png', 'joined_goodput_cdf.pdf', 'plot_data.csv', 'capacity_reference.csv'):
        (SIM_DIR / 'plots/experiment1and2' / name).unlink(missing_ok=True)
    print(f'Experiment {number} goodput CDF: {output}', flush=True)


@plot_style(aggregate=True, cdf=True)
def plot_rtt_cdf(summaries):
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    number = summaries[0]['experiment']
    output = SIM_DIR / 'plots' / f'experiment{number}'
    output.mkdir(parents=True, exist_ok=True)
    observations, subflows, references = [], [], {}
    for summary in summaries:
        row, paths = rtt_observation(summary)
        observations.append(row)
        subflows.extend(paths)
        if row['included'] and math.isfinite(row['base_rtt_ms']):
            references[(row['run'], row['seed'], row['base_rtt_ms'])] = row['base_rtt_ms']
    write_csv(output / 'rtt_plot_data.csv', observations)
    if subflows:
        write_csv(output / 'rtt_subflows.csv', subflows)
    else:
        (output / 'rtt_subflows.csv').unlink(missing_ok=True)
    if not any(row['included'] for row in observations):
        for name in ('rtt_cdf.png', 'rtt_cdf.pdf', 'base_rtt_reference.csv'):
            (output / name).unlink(missing_ok=True)
        raise ValueError(f'Experiment {number}: no trials with RTT measurements for both sending subflows; see rtt_plot_data.csv')
    fig = plt.figure(figsize=CDF_SIZE)
    ax = fig.add_axes([.16, .28, .82, .57])
    handles = []
    for protocol, (_, _, label, color) in PROTOCOLS.items():
        x, y = cdf_points(r['average_rtt_ms'] for r in observations if r['protocol'] == protocol)
        if len(x):
            ax.step(x, y, where='post', color=color)
            handles.append(Line2D([], [], color=color, label=label))
    if references:
        x, y = cdf_points(references.values())
        ax.step(x, y, where='post', color='black')
        handles.append(Line2D([], [], color='black', label='Propagation RTT'))
        write_csv(output / 'base_rtt_reference.csv', [dict(run=run, seed=seed, average_base_rtt_ms=value)
            for (run, seed, _), value in sorted(references.items())])
    else:
        (output / 'base_rtt_reference.csv').unlink(missing_ok=True)
    ax.legend(handles=handles, loc='upper left', ncols=3, fontsize=7,
              columnspacing=.8, handletextpad=.5)
    ax.set(xlabel='Average RTT (ms)',
           ylabel=r'\% of Trials' if plt.rcParams['text.usetex'] else '% of Trials',
           ylim=(0, 105), yticks=[0, 50, 100])
    ax.set_title('RTT without Loss' if number == 1 else 'RTT with Loss')
    save_figure(fig, output / 'rtt_cdf', aggregate=True)
    plt.close(fig)
    included = sum(row['included'] for row in observations)
    print(f'Experiment {number} RTT CDF: {output} ({included}/{len(observations)} valid trials)', flush=True)


@plot_style(aggregate=True)
def plot_ratios(summaries):
    import matplotlib.pyplot as plt
    from matplotlib.ticker import ScalarFormatter
    number = summaries[0]['experiment']
    output = SIM_DIR / 'plots' / f'experiment{number}'
    output.mkdir(parents=True, exist_ok=True)
    observations = [ratio_observation(summary) for summary in summaries]
    layouts = [None] if number == 3 else [layout for layout in ('Aligned', 'Mirrored')
        if any(row['layout'] == layout or row['rtt_ms'] == 20 for row in observations)]
    plot_data = []
    for layout in layouts:
        fig, ax = plt.subplots(figsize=RTT_SIZE)
        layout_runs = [r for r in observations if number == 3 or r['layout'] == layout or r['rtt_ms'] == 20]
        for protocol, (_, _, label, color) in PROTOCOLS.items():
            selected = [r for r in layout_runs if r['protocol'] == protocol]
            if not selected:
                continue
            rtts = sorted({r['rtt_ms'] for r in selected})
            means, deviations = [], []
            for rtt in rtts:
                values = [r['goodput_ratio'] for r in selected if r['rtt_ms'] == rtt and math.isfinite(r['goodput_ratio'])]
                mean = float(np.mean(values)) if values else math.nan
                std = float(np.std(values, ddof=1)) if len(values) > 1 else 0 if values else math.nan
                means.append(mean)
                deviations.append(std)
                plot_data.append(dict(protocol=protocol, layout=layout, rtt_ms=rtt,
                                      metric='goodput_ratio', n=len(values), mean=mean, std=std))
            _, caps, bars = ax.errorbar(rtts, means, yerr=deviations, marker=MARKERS[protocol],
                linewidth=.30, elinewidth=.75, capsize=2, capthick=.75, color=color, label=label)
            for item in (*caps, *bars):
                item.set_alpha(.5)
        ax.set(xlabel='RTT (ms)', ylabel='Goodput Ratio', ylim=(0, 1.1))
        for axis in (ax.xaxis, ax.yaxis):
            axis.set_major_formatter(ScalarFormatter())
        ax.legend(fontsize='small', ncols=len(PROTOCOLS), loc='upper center',
                  bbox_to_anchor=(.5, 1.20), columnspacing=1., handletextpad=.5,
                  labelspacing=.1, borderaxespad=0.)
        ax.set_title('Inter-RTT Fairness' if number == 3 else f'Intra-RTT Fairness ({layout.lower()})', pad=27)
        name = 'inter_rtt' if number == 3 else f'intra_rtt_{layout.lower()}'
        save_figure(fig, output / name, aggregate=True)
        plt.close(fig)
    write_csv(output / 'plot_data.csv', plot_data)
    write_csv(output / 'ratio_runs.csv', observations)
    print(f'Goodput ratio plots: {output}')


def plot(configs, cdf_metrics=None):
    import matplotlib
    matplotlib.use('Agg')
    summaries = aggregate_summaries(configs)
    cdf_metrics = ('goodput', 'rtt') if cdf_metrics is None else cdf_metrics
    for number in (1, 2, 3, 4):
        selected = [s for s in summaries if s['experiment'] == number]
        if selected:
            if number <= 2:
                if 'goodput' in cdf_metrics:
                    plot_cdf(selected)
                if 'rtt' in cdf_metrics:
                    plot_rtt_cdf(selected)
            else:
                plot_ratios(selected)
