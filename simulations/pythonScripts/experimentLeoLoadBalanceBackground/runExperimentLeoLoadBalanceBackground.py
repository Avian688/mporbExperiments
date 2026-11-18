#!/usr/bin/env python3
"""Prepare saved routes, simulate, extract, and plot the two-burst LEO experiment."""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import re
import signal
import sys

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / 'experimentLeo'))
sys.path.insert(0, str(SCRIPTS))

import backgroundRoutes
import leo
from runExperimentLeo import ensure_index, simulate
from runExperiments import ACTIVE, LOCK, STOP, parallel, terminate


EXPERIMENT = 'experimentLeoLoadBalanceBackground'
DURATION = 100
BURST_WINDOWS = backgroundRoutes.BURSTS
BACKGROUND_FLOWS = 5
BACKGROUND_MBPS = 60
FOREGROUND_PAIRS = tuple(
    dict(pair=role, source=sender, destination=receiver,
         source_city=f'New York {role}', destination_city=f'London {role}')
    for role, (sender, receiver) in zip(backgroundRoutes.ROLES[:2], backgroundRoutes.PAIRS[:2]))
RANKS = {
    1: ((1,), (1,)),
    2: ((1, 2), (2, 3)),
    3: ((1, 2, 4), (2, 3, 4)),
    4: ((1, 2, 4, 5), (2, 3, 4, 5)),
    5: ((1, 2, 4, 5, 6), (2, 3, 4, 5, 6)),
}
CSV_FIELDS = ('connection', 'module', 'time', 'goodput_bps')
SINK_THREAD = '"tcpgoodputapplications.applications.tcpapp.TcpGoodputSinkAppThread"'


def configurations(replay_start, experiment=EXPERIMENT, shared_budget=False):
    matrix = []
    for config in leo.configurations(DURATION, replay_start, include_omega10=True):
        k = config['k']
        config.update(experiment=experiment, starts=config['starts'][:2],
                      ranks=[list(ranks) for ranks in RANKS[k]])
        if experiment != EXPERIMENT:
            config.update(terminal_mbps=400, background_ranks=[1, 3 if k > 1 else 1])
            if shared_budget and k > 1:
                config.update(shared_isl_budget_mbps=100)
        matrix.append(config)
    return matrix


def recording_lines():
    sender = FOREGROUND_PAIRS[0]['source']
    background_receiver = backgroundRoutes.PAIRS[2][1]
    return [f'*.userTerminal[{sender}].app[0].tOpen.param-recording = true',
            '**.param-recording = false',
            *leo.recording_lines(pairs=FOREGROUND_PAIRS, extra_goodput_receivers=(background_receiver,))]


def path_lines(pair, group, ranks):
    entries = []
    rank_map = ' '.join(map(str, ranks))
    for terminal in pair:
        prefix = f'*.userTerminal[{terminal}].ipv4.ip'
        entries += [f'{prefix}.kPathTcpPathGroup = {group}',
                    f'{prefix}.kPathTcpSubflows = {len(ranks)}',
                    f'{prefix}.kPathTcpRankMap = "{rank_map}"']
    return entries


def background_lines(port_offset=0):
    pair = backgroundRoutes.PAIRS[2]
    source, destination = pair
    senders, receivers = f'*.userTerminal[{source}]', f'*.userTerminal[{destination}]'
    sessions = len(BURST_WINDOWS) * BACKGROUND_FLOWS
    lines = path_lines(pair, 2, (1,) * sessions)
    for terminal in (senders, receivers):
        lines += [f'{terminal}.tcp.typename = "Orbtcp"',
                  f'{terminal}.tcp.tcpAlgorithmClass = "OrbtcpPintFlavour"',
                  f'{terminal}.numApps = {sessions}']
    # Each session has its own port tuple and uses the same saved route.
    period_ms = 20
    bytes_per_write = BACKGROUND_MBPS * 1000 * period_ms // 8
    for burst, (start, end) in enumerate(BURST_WINDOWS):
        script = json.dumps(';'.join(f'{tick / 1000:.3f} {bytes_per_write}'
                                    for tick in range(start * 1000, end * 1000, period_ms)))
        for flow in range(BACKGROUND_FLOWS):
            index = burst * BACKGROUND_FLOWS + flow
            port_slot = index + port_offset
            lines += leo.app_lines(f'{senders}.app[{index}]', dict(
                typename='"TcpGoodputSessionApp"', localPort=4000 + port_slot,
                connectAddress=f'"userTerminal[{destination}]"', connectPort=1000 + port_slot,
                tOpen=f'{start - .5:g}s', tClose=f'{end}s', sendBytes='0B',
                sendScript=script, abortAtClose='true'))
            lines += leo.app_lines(f'{receivers}.app[{index}]', dict(
                typename='"TcpSinkApp"', localPort=1000 + port_slot, serverThreadModuleType=SINK_THREAD))
    return lines


def traffic_lines(config):
    protocol, k = config['protocol'], config['k']
    tcp, algorithm, _ = leo.PROTOCOLS[protocol]
    single = protocol == 'OrbSingle'
    name = config['config']
    entries = ['', f'[Config {name}]', 'extends = BackgroundTransport',
               f'seed-set = {config["seed"]}', f'sim-time-limit = {DURATION}s',
               f'output-vector-file = "results/{name}-#0.vec"',
               f'output-scalar-file = "results/{name}-#0.sca"']
    if 'background_ranks' in config:
        burst_ranks = [rank for rank in config['background_ranks'] for _ in range(BACKGROUND_FLOWS)]
        entries += path_lines(backgroundRoutes.PAIRS[2], 2, burst_ranks)
    if 'shared_isl_budget_mbps' in config:
        common_ranks = sorted(set(config['ranks'][0]).intersection(config['ranks'][1]))
        ranks = ' '.join(map(str, common_ranks))
        entries += [f'*.channelConstructor.sharedIslBudget = {config["shared_isl_budget_mbps"]}Mbps',
                    f'*.channelConstructor.sharedIslRanks = "{ranks}"',
                    '*.channelConstructor.sharedIslPathGroups = "0 1"']
    for group, (pair, ranks, start) in enumerate(zip(
            backgroundRoutes.PAIRS[:2], config['ranks'], config['starts'])):
        source, destination = pair
        entries += path_lines(pair, group, ranks)
        for terminal in pair:
            host = f'*.userTerminal[{terminal}]'
            entries += [f'{host}.tcp.typename = "{tcp}"',
                        f'{host}.tcp.tcpAlgorithmClass = "{algorithm}"',
                        f'{host}.numApps = 1']
        sender = dict(
            typename='"TcpGoodputSessionApp"' if single else '"MpTcpSessionApp"',
            localPort=4000, connectPort=1000, connectAddress=f'"userTerminal[{destination}]"',
            tOpen=f'{start:g}s', tSend=f'{start:g}s', tClose=f'{DURATION + 1}s',
            sendBytes='1536MiB', dataTransferMode='"bytecount"')
        if not single and 'terminal_mbps' in config:
            sender.update(sendBytes='8MiB', keepBacklogged='true')
        entries += leo.app_lines(f'*.userTerminal[{source}].app[0]', sender)
        entries += leo.app_lines(f'*.userTerminal[{destination}].app[0]', dict(
            typename='"TcpSinkApp"' if single else '"MpTcpSinkApp"', localPort=1000,
            serverThreadModuleType=SINK_THREAD))
    if not single:
        # Both the socket apps and the TCP module expose this parameter.
        entries += [f'**.numberOfSubflows = {k}']
    shuffle = leo.SHUFFLE_FRACTIONS.get(protocol)
    for node in ('userTerminal', 'groundStation', 'satellite'):
        queue = f'*.{node}[*].ppp[*].queue'
        entries += [f'{queue}.typename = "{"MpOrbDeltaQueue" if shuffle else "PintQueue"}"']
        if shuffle:
            entries += [f'{queue}.deltaShuffleFraction = {shuffle:g}',
                        f'{queue}.deltaShuffleInterval = 0s']
    return entries


def generate(matrix, store, replay_start):
    root = leo.directory(matrix[0])
    root.mkdir(parents=True, exist_ok=True)
    backgroundRoutes.write_topology(leo.topology_file(matrix[0]), replay_start + DURATION)
    # Offset zero aliases slot one. The two-rank setup uses slots 1–10 exactly once.
    port_offset = int('background_ranks' in matrix[0])
    lines = ['include topology.ini', '', '[Config BackgroundTransport]',
             *leo.replay_lines(store, root, matrix[0]), *leo.transport_parameters(),
             *recording_lines(), *background_lines(port_offset), '*.userTerminal[*].numApps = 0']
    if 'terminal_mbps' in matrix[0]:
        lines += [f'*.channelConstructor.userTerminalDataRate = {matrix[0]["terminal_mbps"]}Mbps']
    for config in matrix:
        lines.extend(traffic_lines(config))
    leo.ini_file(matrix[0]).write_text('\n'.join(lines) + '\n')
    (root / 'matrix.json').write_text(json.dumps(dict(configurations=matrix,
        replay_start=replay_start, duration=DURATION, burst_windows=BURST_WINDOWS,
        background_flows_per_burst=BACKGROUND_FLOWS, background_mbps=BACKGROUND_MBPS,
        route_store=str(store)), indent=2) + '\n')


def csv_path(config):
    return leo.directory(config) / 'csvs' / f'{config["config"]}_goodput.csv'


def extract(config):
    from terminalVectors import export_vectors

    vectors = ensure_index(config)
    export_vectors(config, vectors, pairs=FOREGROUND_PAIRS)
    target = csv_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    with temporary.open('w', newline='') as output:
        writer = csv.writer(output)
        writer.writerow(CSV_FIELDS)
        for role, (_, receiver) in zip(('A', 'B', 'Background'), backgroundRoutes.PAIRS):
            pattern = re.compile(rf'\.userTerminal\[{receiver}\]\.app\[\d+\]\.thread_\d+$')
            for vector in vectors.items.values():
                if vector.metric == 'goodput' and pattern.search(vector.module):
                    writer.writerows((role, vector.module, time, value)
                                     for time, value in vectors.samples(vector))
    temporary.replace(target)
    return config['config']


def goodput_series(config):
    path = csv_path(config)
    if not path.is_file():
        raise FileNotFoundError(f'{path}: extract selected simulations with --start-step 3 first')
    series = {role: {} for role in backgroundRoutes.ROLES}
    with path.open(newline='') as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != CSV_FIELDS:
            raise ValueError(f'Invalid extracted CSV header: {path}')
        for row in reader:
            role = row['connection']
            if role in series:
                samples = series[role].setdefault(row['module'], [])
                samples.append((float(row['time']), float(row['goodput_bps'])))
    return series


def receiver_series(config):
    series = goodput_series(config)
    return [list(series[role].values()) for role in ('A', 'B')]


def plot_individual(job):
    from plotExperimentLeoLoadBalanceBackground import plot_run
    config, output = job
    plot_run(config, output, goodput_series(config), burst_windows=BURST_WINDOWS)
    return config['config']


def main(argv=None, *, experiment=EXPERIMENT, shared_budget=False):
    bottleneck = experiment != EXPERIMENT
    root = leo.SIM_DIR / 'experiments' / experiment
    plot_root = leo.SIM_DIR / 'plots' / experiment
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cores', type=int, default=int(os.environ.get('EXPERIMENT_CORES', '2')))
    parser.add_argument('--runs', type=int, choices=leo.RUNS, default=len(leo.RUNS))
    parser.add_argument('--ks', nargs='+', type=int, choices=leo.KS, default=leo.KS[1:] if bottleneck else leo.KS)
    parser.add_argument('--protocols', nargs='+', choices=tuple(leo.PROTOCOLS),
                        default=[p for p in leo.DEFAULT_PROTOCOLS if not bottleneck or p != 'OrbSingle'])
    parser.add_argument('--include-omega10', action='store_true')
    parser.add_argument('--route-store', type=Path)
    parser.add_argument('--replay-start', type=float, help='Original start time (default 0 s), aligned to 100 ms')
    parser.add_argument('--start-step', type=int, choices=range(1, 5), default=1)
    parser.add_argument('--end-step', type=int, choices=range(1, 5), default=4)
    parser.add_argument('--plots', nargs='+', choices=('all', 'fairness-efficiency', 'individual'),
                        default=['all'])
    parser.add_argument('--sim-timeout-seconds', type=float, default=12 * 3600)
    parser.add_argument('--retries', type=int, default=1)
    parser.add_argument('--skip-generate', action='store_true')
    parser.add_argument('--rerun', action='store_true', help='Replace selected traffic results')
    parser.add_argument('--check-routes', action='store_true', help='Check an existing catalog without simulating')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if (args.cores < 1 or args.retries < 0 or args.start_step > args.end_step or
            not math.isfinite(args.sim_timeout_seconds) or args.sim_timeout_seconds <= 0):
        parser.error('Invalid cores, step range, retry count or timeout')

    manifest = json.loads((root / 'matrix.json').read_text()) if args.skip_generate else {}
    store = (args.route_store or Path(manifest.get('route_store', leo.DEFAULT_STORE))).resolve()
    replay_start = (args.replay_start if args.replay_start is not None else
                    manifest.get('replay_start', 0))
    if (not math.isfinite(replay_start) or replay_start < 0 or
            not math.isclose(replay_start * 10, round(replay_start * 10), abs_tol=1e-8)):
        parser.error('--replay-start must be nonnegative and aligned to 100 ms')
    if args.skip_generate and (str(store) != manifest['route_store'] or replay_start != manifest['replay_start']):
        parser.error('Replay inputs differ from the existing manifest. Regenerate the INI files.')
    matrix = manifest['configurations'] if args.skip_generate else configurations(replay_start, experiment, shared_budget)
    protocols = set(args.protocols) | ({leo.OMEGA_10} if args.include_omega10 else set())
    selected = [c for c in matrix if c['protocol'] in protocols and c['run'] <= args.runs and
                (c['k'] in args.ks or c['protocol'] == 'OrbSingle')]
    if not selected:
        parser.error('No traffic configurations selected')
    if args.dry_run:
        flows = manifest.get('background_flows_per_burst', 1) if args.skip_generate else BACKGROUND_FLOWS
        print(f'Original timeline {replay_start:g}–{replay_start + DURATION:g}s '
              f'({"with replay checkpoint" if replay_start else "no checkpoint"}), '
              f'{flows} background flows per burst at local {BURST_WINDOWS}')
        print(f'{len(selected)} traffic runs, two measured connections per run')
        for config in selected:
            settings = ''
            if bottleneck:
                settings = f', bursts on ranks {config["background_ranks"]}, access {config["terminal_mbps"]} Mbps'
                if 'shared_isl_budget_mbps' in config:
                    settings += f', shared budget {config["shared_isl_budget_mbps"]} Mbps'
            print(f'{config["config"]}: A {config["ranks"][0]}, B {config["ranks"][1]}{settings}')
        return

    active_ranks = {k: RANKS[k] for k in sorted({c['k'] for c in selected})}
    burst_ranks = {c['k']: c.get('background_ranks', [1, 1]) for c in selected}
    if args.check_routes:
        backgroundRoutes.check_routes(store, root, replay_start, DURATION)
        backgroundRoutes.report_burst_ranks(root, active_ranks, burst_ranks)
        return
    if not args.skip_generate and args.start_step <= 2:
        generate(matrix, store, replay_start)
    if args.start_step <= 2:
        route_hash = backgroundRoutes.prepare_routes(store, root, replay_start, DURATION,
                                                     args.sim_timeout_seconds, reuse_existing=bottleneck)
        backgroundRoutes.report_burst_ranks(root, active_ranks, burst_ranks)
        if args.end_step >= 2:
            print(f'{experiment}: simulate {len(selected)} traffic runs', flush=True)
            parallel(simulate, [(c, args, route_hash) for c in selected], args.cores)
    if args.start_step <= 3 <= args.end_step:
        print(f'{experiment}: extract goodput CSVs', flush=True)
        parallel(extract, selected, args.cores, processes=True)
    if args.start_step <= 4 <= args.end_step:
        output = plot_root.with_name(experiment + 'Omega10') if leo.OMEGA_10 in protocols else plot_root
        plots = set(args.plots)
        if plots & {'all', 'fairness-efficiency'}:
            from plotExperimentLeoLoadBalanceBackground import plot
            print(f'{experiment}: plot foreground goodput and fairness', flush=True)
            plot(selected, output, receiver_series, burst_windows=BURST_WINDOWS,
                 access_ceiling_mbps=2 * selected[0].get('terminal_mbps', 100), raw_goodput=bottleneck)
        if plots & {'all', 'individual'}:
            print(f'{experiment}: plot {len(selected)} individual goodput overviews', flush=True)
            parallel(plot_individual, [(config, output) for config in selected],
                     args.cores, processes=True)
            from plotExperimentLeo import plot_metrics
            detailed = [c for c in selected if (leo.vector_dir(c) / 'vectors.json').is_file()]
            if detailed:
                plot_metrics(detailed, cores=args.cores, pairs=FOREGROUND_PAIRS,
                             output_root=output / 'individual')
            if len(detailed) < len(selected):
                print(f'{len(selected) - len(detailed)} runs have only the goodput CSV. '
                      'Run step 3 to extract any recorded endpoint metrics.', flush=True)


def run(**settings):
    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        main(**settings)
    except KeyboardInterrupt:
        STOP.set()
        with LOCK:
            for process in list(ACTIVE):
                terminate(process)
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    run()
