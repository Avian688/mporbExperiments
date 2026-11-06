"""Individual endpoint figures from extracted terminal CSVs."""
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import leo
from analyzeExperimentLeo import write_csv
from plotStyle import TRACE_SIZE, plot_style, save_figure
from terminalVectors import (CsvVectors, RATES, RECEIVER_METRICS, applicable, pair_vectors,
                             prepare_curve, sample_series, terminal_folder)

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
def draw_metric(ax, curves, metric, horizon, terminal_colors=None, legend_columns=1, compact_legend=False):
    """One terminal and one metric, shared by standalone and combined figures."""
    from matplotlib.ticker import MaxNLocator
    import numpy as np
    plotted = 0
    styles_used = {}
    terminal_handles, ranks = {}, set()
    for points,label,rank,displayed in curves:
        if not displayed:
            continue
        data = np.asarray(points,dtype=float)
        style = {'color':f'C{(rank-1)%10}'} if rank is not None else {}
        if terminal_colors is not None:
            terminal = terminal_colors[label]
            ordinal = styles_used.get(terminal,0)
            styles_used[terminal] = ordinal+1
            style = {'color':f'C{terminal%10}'}
            if metric!='rtt':
                style['linestyle'] = ('-','--','-.',':',(0,(5,1,1,1)))[((rank-1) if rank is not None else ordinal)%5]
            terminal_handles[label.split(': ',1)[0]] = terminal
            if rank is not None:
                ranks.add(rank)
        if 'Meta/control' in label:
            style.update(linestyle='--')
            if terminal_colors is None:
                style['color'] = '.4'
        if metric == 'rtt':
            ax.plot(data[:,0],data[:,1]*LABELS[metric][1],linestyle='none',
                    marker='.',markersize=1.8,label=label,**style)
        else:
            ax.step(data[:,0],data[:,1]*LABELS[metric][1],
                    where='pre' if metric in RATES else 'post',label=label,**style)
        plotted += 1
    if plotted:
        legend_args = {}
        if compact_legend:
            from matplotlib.lines import Line2D
            handles = [Line2D([],[],color=f'C{color%10}',label=label)
                       for label,color in terminal_handles.items()]
            if metric!='rtt':
                handles += [Line2D([],[],color='.4',
                    linestyle=('-','--','-.',':',(0,(5,1,1,1)))[(rank-1)%5],label=f'Path {rank}')
                    for rank in sorted(ranks)]
            legend_args['handles'] = handles
        ax.legend(fontsize=6.5 if terminal_colors is not None else 8,
                  loc='upper left',ncol=legend_columns,**legend_args)
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


def save_combined(prepared, title, path, horizon, terminal_colors=None):
    import matplotlib.pyplot as plt
    if not prepared:
        path.unlink(missing_ok=True)
        return False
    with plot_style():
        rows = (len(prepared)+1)//2
        fig, axes = plt.subplots(rows,2,figsize=(17,4*rows),squeeze=False)
        for ax,(metric,curves) in zip(axes.flat,prepared):
            draw_metric(ax,curves,metric,horizon,terminal_colors,
                        legend_columns=2 if terminal_colors is not None else 1,
                        compact_legend=terminal_colors is not None)
            ax.set_title(TITLES[metric])
        for ax in list(axes.flat)[len(prepared):]:
            ax.set_visible(False)
        fig.suptitle(title)
        fig.tight_layout(rect=[0,0,1,1-.045/rows])
        fig.savefig(path)
        plt.close(fig)
    return True


def plot_run(job):
    config, metrics, output_root, source = job
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    vectors = CsvVectors(source,config)
    output_dir = Path(output_root) / leo.run_subdir(config)
    output_dir.mkdir(parents=True,exist_ok=True)
    records, outputs = [], []
    figure_count = 0
    comparisons = {metric:[] for metric in metrics}
    terminal_colors = {}
    for terminal_index,pair in enumerate(leo.PAIRS):
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
                    points,count,status = vectors.curve(vector)
                    omit = (metric in ('retransmissionRate','numRtos') and rank is None and
                            all(value == 0 for _,value in points))
                    displayed = bool(points and not omit)
                    records.append(dict(config=config['config'],protocol=config['protocol'],k=config['k'],run=config['run'],
                        pair=pair['pair'],terminal=terminal,metric=metric,module=vector.module,label=label,
                        rank=rank if rank is not None else '',status=status,
                        raw_points=count,plot_points=len(points),displayed=displayed,plot_unit=LABELS[metric][0]))
                    curves.append((points,label,rank,displayed))
                    comparison_label = f'{pair[side+"_city"]} (UT{terminal}): {label}'
                    comparisons[metric].append((points,comparison_label,rank,displayed))
                    terminal_colors[comparison_label] = terminal_index
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
            if save_combined(prepared,title,combined,config['sim_time']):
                outputs.append(str(combined))
    aggregate = output_dir/'aggregate'
    aggregate.mkdir(exist_ok=True)
    title = (f'All user terminals - {leo.PROTOCOLS[config["protocol"]][2]}, '
             f'K={config["k"]}, Run {config["run"]}')
    prepared = []
    for metric,curves in comparisons.items():
        output = aggregate/metric
        if not curves:
            for suffix in ('.png','.pdf'):
                output.with_suffix(suffix).unlink(missing_ok=True)
            continue
        prepared.append((metric,curves))
        with plot_style():
            fig, ax = plt.subplots(figsize=TRACE_SIZE)
            draw_metric(ax,curves,metric,config['sim_time'],terminal_colors,legend_columns=5)
            ax.set_title(f'{TITLES[metric]} - {title}')
            fig.tight_layout()
            save_figure(fig,output)
            plt.close(fig)
        outputs += [str(output.with_suffix(s)) for s in ('.png','.pdf')]
    if save_combined(prepared,title,aggregate/'combined.pdf',config['sim_time'],terminal_colors):
        outputs.append(str(aggregate/'combined.pdf'))
    write_csv(output_dir/'vectors.csv',records)
    (output_dir/'plots.json').write_text(json.dumps(dict(config=config,
        source_stat=vectors.manifest['source_stat'],csv_source=str(vectors.root),
        metrics=list(metrics),bins=vectors.manifest['bins'],
        figures=figure_count,comparison_figures=len(prepared),
        combined_pdfs=sum(p.endswith('/combined.pdf') for p in outputs),outputs=outputs),indent=2)+'\n')
    # Migrate the old flat configuration layout after the replacement succeeds.
    legacy = Path(output_root)/config['config']
    if legacy!=output_dir and legacy.is_dir():
        shutil.rmtree(legacy)
    return dict(config=config['config'],figures=figure_count,
                comparison_figures=len(prepared),
                combined_pdfs=sum(p.endswith('/combined.pdf') for p in outputs),records=records)


def plot_metrics(configs, metrics=None, cores=1):
    metrics = list(leo.USER_METRICS if metrics is None else metrics)
    unknown = set(metrics)-set(leo.USER_METRICS)
    if unknown or not metrics or cores<1:
        raise ValueError(f'Invalid individual-plot selection: {sorted(unknown)}')
    if not configs:
        raise ValueError('No runs selected for individual plots')
    output_root = leo.plot_directory(configs[0])/'individual'
    jobs = []
    for config in configs:
        source = leo.vector_dir(config)
        exported = CsvVectors(source,config)
        native = leo.result(config,'.vec')
        if ((native.is_file() and leo.stat(native)!=exported.manifest['source_stat']) or
                leo.input_hash(config)!=exported.manifest['input_hash']):
            raise ValueError(f'{config["config"]}: terminal CSVs are stale; re-extract step 3')
        jobs.append((config,metrics,str(output_root),str(source)))
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
    comparisons = sum(r['comparison_figures'] for r in results)
    combined = sum(r['combined_pdfs'] for r in results)
    missing = sum(r['status']=='not_recorded' for r in records)
    print(f'Wrote {count} terminal and {comparisons} all-terminal metric figures (PNG/PDF), '
          f'plus {combined} combined PDFs; {missing} recordings absent. Index: {output_root / "vectors.csv"}',flush=True)
    return records


if __name__=='__main__':
    from runExperimentLeo import run
    # Same filters and --plots controls as the runner, restricted to plotting.
    run(['--plots',*leo.USER_METRICS,*sys.argv[1:],
         '--start-step','4','--end-step','4','--skip-generate'])
