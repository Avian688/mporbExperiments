"""Individual endpoint figures from indexed vectors, without full CSV copies."""
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import leo
from analyzeExperimentLeo import sender_ranks, write_csv
from plotStyle import TRACE_SIZE, plot_style, save_figure
from vectors import Vectors

LABELS = {
    'goodput': ('Goodput (Mbps)',1e-6), 'rtt': ('RTT (ms)',1000),
    'cwnd': ('CWND (MSS)',1/leo.MSS), 'U': ('Congestion U',1),
    'holBlockedBytes': ('HoL blocked bytes',1), 'throughput': ('Throughput (Mbps)',1e-6),
    'retransmissionRate': ('Retransmission rate (Mbps)',1e-6),
    'mbytesInFlight': ('Bytes In-Flight (MSS)',1/leo.MSS),
    'metaReinjectedBytes': ('Cumulative meta-level reinjected bytes',1),
    'numRtos': ('Cumulative RTO count',1),
}
TITLES = dict(goodput='Goodput',rtt='RTT',cwnd='CWND',U='Congestion U',
    holBlockedBytes='HoL Blocking',throughput='Throughput',retransmissionRate='Retransmission Rate',
    mbytesInFlight='Bytes In-Flight',metaReinjectedBytes='Reinjected Bytes',numRtos='Retransmission timeouts')
RATES = {'goodput','throughput','retransmissionRate'}
COUNTERS = {'numRtos', 'metaReinjectedBytes'}
DATA_SUBFLOWS = {'rtt', 'cwnd', 'U', 'mbytesInFlight'}
RECEIVER_METRICS = {'goodput', 'throughput', 'holBlockedBytes'}


def terminal_folder(pair, receiver=False):
    side = 'destination' if receiver else 'source'
    city = re.sub(r'[^A-Za-z0-9]+', '_', pair[side+'_city'])
    return f'userTerminal{pair[side]}_{city}'


def sample_series(samples, horizon, bins=3000):
    """Keep first/last/min/max per bin, with bounded memory and original times."""
    if bins < 1 or not math.isfinite(horizon) or horizon <= 0:
        raise ValueError('Invalid plotting horizon or bin count')
    points, bucket = [], []
    current = None
    count = 0

    def flush():
        if bucket:
            chosen = {bucket[0], bucket[-1], min(bucket, key=lambda p:p[2]),
                      max(bucket, key=lambda p:p[2])}
            points.extend((t, v) for _, t, v in sorted(chosen))

    for index, (time, value) in enumerate(samples):
        if time > horizon:
            break
        if time < 0 or not math.isfinite(value):
            continue
        count += 1
        key = min(bins - 1, int(time / horizon * bins))
        if current != key:
            flush()
            bucket.clear()
            current = key
        point = index, time, value
        if not bucket:
            bucket = [point]
        else:
            bucket = [bucket[0], min((*bucket, point), key=lambda p:p[2]),
                      max((*bucket, point), key=lambda p:p[2]), point]
    flush()
    return points, count


def prepare_curve(vectors, vector, metric, horizon, bins=3000):
    points, count = sample_series(vectors.samples(vector), horizon, bins)
    status = 'recorded' if points else 'no_samples'
    if metric in COUNTERS:
        if not vector.blocks:
            # These registered cumulative counters start at zero and emit when
            # incremented. An absent recording is handled separately, never here.
            points, status = [(0., 0.), (horizon, 0.)], 'zero_events'
        elif points and points[0][0] > 0:
            points.insert(0, (0., 0.))
    # Measured RTT is a set of observations, not a state held across an outage.
    # Periodic rates also must not extend beyond their last actual interval.
    if points and metric != 'rtt' and metric not in RATES and points[-1][0] < horizon:
        points.append((horizon, points[-1][1]))
    return points, count, status


def applicable(config, metric):
    return not ((metric == 'U' and config['protocol'] == 'CubicUncoupled') or
                (metric in ('holBlockedBytes', 'metaReinjectedBytes') and config['protocol'] == 'OrbSingle'))


def pair_vectors(vectors, config, pair, metric):
    """Return (vector, label, rank) without inferring passive-side path identity."""
    if not applicable(config, metric):
        return []
    receiver = metric in RECEIVER_METRICS
    terminal = pair['destination'] if receiver else pair['source']
    host = rf'\.userTerminal\[{terminal}\]'
    pattern = re.compile(host + (r'\.app\[0\]\.thread_\d+$' if metric == 'goodput'
                                else r'\.tcp\.conn-\d+$'))
    found = [v for v in vectors.items.values() if v.metric == metric and pattern.search(v.module)]
    found.sort(key=lambda v:int(v.module.rsplit('_' if metric == 'goodput' else '-', 1)[1]))
    if receiver:
        return [(v, 'Application goodput' if metric == 'goodput' and len(found) == 1 else
                    ('Receiver ' + v.module.rsplit('.',1)[-1]), None) for v in found]
    ranks = sender_ranks(vectors, pair['source'], config['k'], config['protocol'] == 'OrbSingle')
    by_module = {v.module:rank for rank,v in ranks.items()}
    if metric in DATA_SUBFLOWS:
        found = [v for v in found if v.module in by_module]
    curves = []
    for vector in found:
        rank = by_module.get(vector.module)
        label = (f'Path {rank} ({vector.module.rsplit(".",1)[-1]})' if rank is not None else
                 'Meta/control ' + vector.module.rsplit('.',1)[-1])
        curves.append((vector,label,rank))
    return curves


def draw_metric(ax, curves, metric, horizon):
    """One terminal and one metric, shared by standalone and combined figures."""
    from matplotlib.ticker import MaxNLocator
    import numpy as np
    plotted = 0
    for points,label,rank,displayed in curves:
        if not displayed:
            continue
        data = np.asarray(points,dtype=float)
        style = {'color':f'C{(rank-1)%10}'} if rank is not None else {}
        if label.startswith('Meta/control'):
            style = {'color':'.4','linestyle':'--'}
        if metric == 'rtt':
            ax.plot(data[:,0],data[:,1]*LABELS[metric][1],linestyle='none',
                    marker='.',markersize=1.8,label=label,**style)
        else:
            ax.step(data[:,0],data[:,1]*LABELS[metric][1],
                    where='pre' if metric in RATES else 'post',label=label,**style)
        plotted += 1
    if plotted:
        ax.legend(fontsize=8,loc='upper left')
    else:
        ax.text(.5,.5,'No recorded samples',ha='center',va='center',transform=ax.transAxes)
    ax.set_xlim(0,horizon)
    ax.set_ylim(bottom=0)
    if metric == 'numRtos':
        ax.set_ylim(top=max(1.,ax.get_ylim()[1]))
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel('Time (s)')
    ax.set_ylabel(LABELS[metric][0])
    ax.grid(True,alpha=.35)


def plot_run(job):
    config, metrics, bins, output_root, source = job
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    vectors = Vectors(source)
    output_dir = Path(output_root) / config['config']
    output_dir.mkdir(parents=True,exist_ok=True)
    records, outputs = [], []
    figure_count = 0
    for pair in leo.PAIRS:
        for receiver in (False,True):
            side = 'destination' if receiver else 'source'
            terminal = pair[side]
            terminal_dir = output_dir / terminal_folder(pair,receiver)
            terminal_dir.mkdir(exist_ok=True)
            title = (f'{pair[side+"_city"]} (UT{terminal}, {"receiver" if receiver else "sender"}) - '
                     f'{leo.PROTOCOLS[config["protocol"]][2]}, K={config["k"]}, Run {config["run"]}')
            prepared = []
            for metric in metrics:
                if (metric in RECEIVER_METRICS) != receiver:
                    continue
                found = pair_vectors(vectors,config,pair,metric)
                output = terminal_dir / metric
                curves = []
                if not found:
                    status = 'not_applicable' if not applicable(config,metric) else 'not_recorded'
                    records.append(dict(config=config['config'],protocol=config['protocol'],k=config['k'],run=config['run'],
                        pair=pair['pair'],terminal=terminal,metric=metric,module='',label='',rank='',status=status,
                        raw_points=0,plot_points=0,displayed=False,plot_unit=LABELS[metric][0]))
                    for suffix in ('.png','.pdf'):
                        output.with_suffix(suffix).unlink(missing_ok=True)
                    continue
                for vector,label,rank in found:
                    points,count,status = prepare_curve(vectors,vector,metric,config['sim_time'],bins)
                    omit = (metric in ('retransmissionRate','numRtos') and rank is None and
                            all(value == 0 for _,value in points))
                    displayed = bool(points and not omit)
                    records.append(dict(config=config['config'],protocol=config['protocol'],k=config['k'],run=config['run'],
                        pair=pair['pair'],terminal=terminal,metric=metric,module=vector.module,label=label,
                        rank=rank if rank is not None else '',status=status,
                        raw_points=count,plot_points=len(points),displayed=displayed,plot_unit=LABELS[metric][0]))
                    curves.append((points,label,rank,displayed))
                prepared.append((metric,curves))
                with plot_style():
                    fig, ax = plt.subplots(figsize=TRACE_SIZE)
                    draw_metric(ax,curves,metric,config['sim_time'])
                    ax.set_title(f'{TITLES[metric]} - {title}')
                    fig.tight_layout()
                    save_figure(fig,output)
                    plt.close(fig)
                outputs += [str(output.with_suffix(s)) for s in ('.png','.pdf')]
                figure_count += 1
            combined = terminal_dir/'combined.pdf'
            if prepared:
                with plot_style():
                    rows = (len(prepared)+1)//2
                    fig, axes = plt.subplots(rows,2,figsize=(17,4*rows),squeeze=False)
                    for ax,(metric,curves) in zip(axes.flat,prepared):
                        draw_metric(ax,curves,metric,config['sim_time'])
                        ax.set_title(TITLES[metric])
                    for ax in list(axes.flat)[len(prepared):]:
                        ax.set_visible(False)
                    fig.suptitle(title)
                    fig.tight_layout(rect=[0,0,1,1-.045/rows])
                    fig.savefig(combined)
                    plt.close(fig)
                outputs.append(str(combined))
            else:
                combined.unlink(missing_ok=True)
    # Remove the superseded five-terminal panel figures when replotting.
    for metric in metrics:
        for suffix in ('.png','.pdf'):
            (output_dir/f'{metric}{suffix}').unlink(missing_ok=True)
    write_csv(output_dir/'vectors.csv',records)
    (output_dir/'plots.json').write_text(json.dumps(dict(config=config,
        source_stat=leo.stat(vectors.path),metrics=list(metrics),bins=bins,
        figures=figure_count,combined_pdfs=sum(p.endswith('/combined.pdf') for p in outputs),outputs=outputs),indent=2)+'\n')
    return dict(config=config['config'],figures=figure_count,
                combined_pdfs=sum(p.endswith('/combined.pdf') for p in outputs),records=records)


def plot_metrics(configs, metrics=None, cores=1, bins=3000):
    metrics = list(leo.USER_METRICS if metrics is None else metrics)
    unknown = set(metrics)-set(leo.USER_METRICS)
    if unknown or not metrics or cores<1 or bins<1:
        raise ValueError(f'Invalid individual-plot selection: {sorted(unknown)}')
    output_root = leo.PLOT_DIR/'individual'
    jobs = [(c,metrics,bins,str(output_root),str(leo.result(c,'.vec'))) for c in configs]
    if not jobs:
        raise ValueError('No runs selected for individual plots')
    if cores == 1 or len(jobs) == 1:
        results = []
        for index,job in enumerate(jobs,1):
            results.append(plot_run(job))
            print(f'Individual plots: {index}/{len(jobs)} runs',flush=True)
    else:
        # Matplotlib is not thread-safe. Keep each run's figures in a separate
        # process, using the runner's existing cancellation/progress handling.
        from runExperiments import parallel
        results = parallel(plot_run,jobs,cores,processes=True)
    records = [row for result in results for row in result['records']]
    write_csv(output_root/'vectors.csv',records)
    count = sum(r['figures'] for r in results)
    combined = sum(r['combined_pdfs'] for r in results)
    missing = sum(r['status']=='not_recorded' for r in records)
    print(f'Wrote {count} independent metric figures (PNG/PDF) and {combined} combined terminal PDFs; {missing} recordings absent. Index: {output_root / "vectors.csv"}',flush=True)
    return records


if __name__=='__main__':
    from runExperimentLeo import run
    # Same filters and --plots controls as the runner, restricted to plotting.
    run(['--plots',*leo.USER_METRICS,*sys.argv[1:],
         '--start-step','4','--end-step','4','--skip-generate'])
