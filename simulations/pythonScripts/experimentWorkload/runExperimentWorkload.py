#!/usr/bin/env python3
"""Run the web-search workload on saved LEO routes and retain aggregate JSON only."""

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import signal
import sys
import tempfile

import saveWorkloadRoutes as routes
from saveWorkloadRoutes import leo, runner
from runExperiments import ACTIVE, LOCK, STOP, logged, parallel, terminate
from webSearchWorkload import DEFAULT_OFFERED_MBPS, DEFAULT_WORKLOAD_SEED, MEAN_FLOW_BYTES, generate_flows
from workloadTopology import DURATION, PAIRS, PATH_COUNT, TERMINALS, routing_definitions


RESULTS = routes.STORE.parent / 'web-search'
LOGS = leo.SIM_DIR / 'logs/experimentWorkload/web-search'
PROTOCOLS = {
    'Omega': leo.PROTOCOLS['Omega'],
    'CubicUncoupled': leo.PROTOCOLS['CubicUncoupled'],
    'OrbUncoupled': leo.PROTOCOLS['OrbUncoupled'],
    'OLIA': ('MpTcp', 'MpTcpOlia', 'OLIA'),
    'BALIA': ('MpTcp', 'MpTcpBalia', 'BALIA'),
    'OrbSingle': leo.PROTOCOLS['OrbSingle'],
}
SUBFLOW_COUNTS = tuple(range(1, PATH_COUNT + 1))
ACTIVE_PORT = 33000
PASSIVE_PORT = 1000
PORT_STRIDE = PATH_COUNT + 1  # One meta port followed by the subflow ports.
LIBRARY = leo.PROJECT_DIR / 'src/mporbExperiments'
CDF_SOURCE = 'https://github.com/ht-gong/UCMP/blob/master/traffic/websearch.csv'
INI = 'web-search.ini'


def arrival_window(duration):
    # Short pilot runs keep a proportionate interval for arrivals and drain.
    scale = duration / DURATION
    return 10 * scale, 490 * scale


def port_slots(flows):
    occupied = [0] * len(TERMINALS)
    slots = []
    for flow in flows:
        in_use = occupied[flow.server] | occupied[flow.client]
        # Pick the lowest slot free at both endpoints. Each integer is a bitset.
        bit = (in_use + 1) & ~in_use
        slot = bit.bit_length() - 1
        slots.append(slot)
        occupied[flow.server] |= bit
        occupied[flow.client] |= bit
    max_offset = (max(slots, default=0) + 1) * PORT_STRIDE - 1
    if max_offset >= ACTIVE_PORT - PASSIVE_PORT or ACTIVE_PORT + max_offset > 65535:
        raise ValueError('The workload exceeds the TCP port space. Reduce the arrival rate or coverage duration.')
    return slots, max_offset


def write_schedule(path, flows, slots):
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle, lineterminator='\n')
        writer.writerow(('id', 'start_s', 'server', 'client', 'bytes', 'port_slot'))
        for flow, slot in zip(flows, slots, strict=True):
            writer.writerow((flow.id, f'{flow.start:.12f}', flow.server, flow.client, flow.bytes, slot))


def case_name(protocol, k, workload_seed):
    return f'WebSearch_{protocol}_K{k}_Seed{workload_seed}'


def scenario_folder(duration, offered_mbps):
    return Path(f'{duration:.12g}s') / f'{offered_mbps:.12g}Mbps'


def shared_config(store, duration, max_offset):
    terminals = '*.userTerminal[*]'
    ip = f'{terminals}.ipv4.ip'
    ranks = ' '.join(str(max(1, offset % PORT_STRIDE)) for offset in range(1, max_offset + 1))
    lines = [
        'include topology.ini', '', '[Config WebSearch]',
        'network = mporbexperiments.workload.WorkloadLeoNetwork',
        f'sim-time-limit = {duration:.12g}s', 'seed-set = 1',
        'cmdenv-status-frequency = 60s', 'record-eventlog = false',
        '**.vector-recording = false', '**.scalar-recording = false',
        '**.bin-recording = false', '**.statistic-recording = false',
        '**.param-recording = false',
        '**.loadFiles = true', '*.configurator.kPathSnapshotMode = "load"',
        '*.configurator.kPathPingRouting = true', '*.configurator.allowRouteSnapshotOverwrite = false',
        f'*.configurator.configLocation = {json.dumps(str(store))}',
        f'{terminals}.numApps = 1', f'{terminals}.app[0].typename = "mporbexperiments.workload.WorkloadEndpoint"',
        f'{terminals}.app[0].workloadFile = "arrivals.csv"',
        f'{terminals}.app[0].activePortBase = {ACTIVE_PORT}',
        f'{terminals}.app[0].passivePortBase = {PASSIVE_PORT}',
        f'{terminals}.app[0].portStride = {PORT_STRIDE}',
        f'{ip}.kPathTcpClientPort = {ACTIVE_PORT}',
        f'{ip}.kPathTcpServerPort = {PASSIVE_PORT}',
        f'{ip}.kPathTcpSubflows = {max_offset}',
        f'{ip}.kPathTcpRankMap = "{ranks}"',
        *[line for line in leo.transport_parameters() if '.kPathTcp' not in line],
    ]
    by_terminal = [[] for _ in TERMINALS]
    for group, (server, client) in enumerate(PAIRS):
        by_terminal[server].append(str(group))
        by_terminal[client].append(str(group))
    lines.extend(f'*.userTerminal[{terminal}].ipv4.ip.kPathTcpPathGroups = "{" ".join(groups)}"'
                 for terminal, groups in enumerate(by_terminal))
    return lines


def case_config(protocol, k, workload_seed):
    tcp, algorithm, _ = PROTOCOLS[protocol]
    config = case_name(protocol, k, workload_seed)
    lines = ['', f'[Config {config}]', 'extends = WebSearch',
             f'*.workloadMetrics.outputFile = "{config}.json"',
             f'*.userTerminal[*].tcp.typename = "{tcp}"',
             f'*.userTerminal[*].tcp.tcpAlgorithmClass = "{algorithm}"']
    if protocol == 'OrbSingle':
        lines.append('*.userTerminal[*].app[0].fullMssPadding = true')
    else:
        lines += ['*.userTerminal[*].tcp.independentAppConnections = true',
                  f'*.userTerminal[*].tcp.**.numberOfSubflows = {k}']
    shuffle = leo.SHUFFLE_FRACTIONS.get(protocol)
    for host in ('satellite', 'groundStation', 'userTerminal'):
        queue = f'*.{host}[*].ppp[*].queue'
        lines.append(f'{queue}.typename = "{"MpOrbDeltaQueue" if shuffle else "PintQueue"}"')
        if shuffle:
            lines.extend((f'{queue}.deltaShuffleFraction = {shuffle:g}', f'{queue}.deltaShuffleInterval = 0s'))
    return lines


def write_config(root, store, duration, max_offset, cases, workload_seed):
    (root / 'topology.ini').write_bytes((store / 'topology.ini').read_bytes())
    lines = shared_config(store, duration, max_offset)
    for protocol, k in cases:
        lines.extend(case_config(protocol, k, workload_seed))
    (root / INI).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def command(config):
    args = runner.command(dict(config=config, protocol='WebSearch'))
    args[args.index('-f') + 1] = INI
    args[args.index('-n') + 1] += ':' + str(LIBRARY.parent)
    return [*args, '-l', str(LIBRARY)]


def run_case(job):
    root, config, protocol, k, workload, timeout = job
    folder = scenario_folder(workload['duration_s'], workload['offered_mbps'])
    log = LOGS / folder / f'{config}.log'
    if not logged(command(config), root, log, timeout):
        raise RuntimeError(f'{config} failed; inspect {log}')
    staged = root / f'{config}.json'
    with staged.open(encoding='utf-8') as handle:
        result = json.load(handle)
    if result.get('schema_version') != 1:
        raise ValueError(f'{config} did not produce supported aggregate metrics; inspect {log}')
    if (result.get('offered_flows') != workload['scheduled_flows'] or
            result.get('offered_bytes') != workload['scheduled_bytes']):
        raise ValueError(f'{config} did not register every scheduled transfer; inspect {log}')
    result['case'] = {'controller': protocol, 'requested_subflows': k, 'workload': workload}
    staged.write_text(json.dumps(result, separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')
    destination = RESULTS / folder / staged.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged.replace(destination)
    log.unlink(missing_ok=True)
    print(f'Saved {destination}', flush=True)
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sim-time', type=float, default=DURATION, help='Simulation duration in seconds (default: 600)')
    parser.add_argument('--route-store', type=Path, default=routes.STORE, help='Store prepared by saveWorkloadRoutes.py')
    parser.add_argument('--offered-mbps', type=float, default=DEFAULT_OFFERED_MBPS,
                        help='Expected application payload offered per second in Mbps (default: 470)')
    parser.add_argument('--workload-seed', type=int, default=DEFAULT_WORKLOAD_SEED,
                        help='Use the same workload draw for every selected controller and K')
    parser.add_argument('--protocols', nargs='+', choices=tuple(PROTOCOLS), default=tuple(PROTOCOLS),
                        help='Controllers to run (default: all six)')
    parser.add_argument('--ks', nargs='+', type=int, choices=SUBFLOW_COUNTS, default=SUBFLOW_COUNTS,
                        help='Requested subflow counts for multipath controllers (default: 1–5)')
    parser.add_argument('--cores', type=int, default=1, help='Maximum simultaneous simulations')
    parser.add_argument('--sim-timeout-seconds', type=float, default=12 * 3600,
                        help='Wall-clock limit per traffic simulation (default: 12 hours)')
    parser.add_argument('--dry-run', action='store_true', help='Generate the workload in memory and print the run scope')
    args = parser.parse_args(argv)
    if not math.isfinite(args.sim_time) or args.sim_time <= 3:
        parser.error('--sim-time must be finite and above 3 seconds')
    if args.cores < 1 or not math.isfinite(args.sim_timeout_seconds) or args.sim_timeout_seconds <= 0:
        parser.error('Cores and wall-clock timeout must be positive')
    if args.workload_seed < 0:
        parser.error('--workload-seed must be nonnegative')

    store = args.route_store.resolve()
    cases = list(dict.fromkeys((protocol, k) for protocol in args.protocols
                              for k in ([1] if protocol == 'OrbSingle' else args.ks)))
    start, end = arrival_window(args.sim_time)
    flows = generate_flows(start, end, args.offered_mbps, args.workload_seed)
    if not flows:
        raise ValueError('The chosen workload draw has no arrivals. Increase the load or duration.')
    slots, max_offset = port_slots(flows)
    payload_bytes = sum(flow.bytes for flow in flows)
    print(f'Web search: {len(flows):,} downloads over {start:g}–{end:g}s; '
          f'{args.offered_mbps:g} Mbps offered ({payload_bytes * 8 / (end-start) / 1e6:.1f} Mbps drawn); '
          f'{len(cases)} matching cases', flush=True)
    if args.dry_run:
        print(f'Saved routes: {store}\nAggregate results: {RESULTS / scenario_folder(args.sim_time, args.offered_mbps)}')
        for protocol, k in cases:
            print(case_name(protocol, k, args.workload_seed))
        return

    routes.check_workload_routes(store, args.sim_time, routing_definitions())
    if not any(LIBRARY.with_name(f'lib{LIBRARY.name}{suffix}').is_file() for suffix in ('.so', '.dylib')):
        raise FileNotFoundError(f'Build the release library for {leo.PROJECT_DIR} before running this workload')
    RESULTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.web-search-', dir=RESULTS) as temporary:
        root = Path(temporary)
        schedule = root / 'arrivals.csv'
        write_schedule(schedule, flows, slots)
        write_config(root, store, args.sim_time, max_offset, cases, args.workload_seed)
        workload = dict(profile='web-search', cdf_source=CDF_SOURCE, cdf_mean_bytes=MEAN_FLOW_BYTES,
                        offered_mbps=args.offered_mbps, seed=args.workload_seed, arrival_start_s=start,
                        arrival_end_s=end, duration_s=args.sim_time, scheduled_flows=len(flows),
                        scheduled_bytes=payload_bytes, schedule_sha256=hashlib.sha256(schedule.read_bytes()).hexdigest())
        jobs = [(root, case_name(protocol, k, args.workload_seed), protocol, k, workload, args.sim_timeout_seconds)
                for protocol, k in cases]
        parallel(run_case, jobs, args.cores)


def stop(signum, _frame):
    STOP.set()
    with LOCK:
        processes = tuple(ACTIVE)
    for process in processes:
        terminate(process)
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('Cancelled web-search simulations')
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error))
