#!/usr/bin/env python3
"""Run the web-search workload on saved LEO routes and retain aggregate JSON only."""

import argparse
from collections import deque
from contextlib import ExitStack
import csv
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

import saveWorkloadRoutes as routes
from saveWorkloadRoutes import leo, runner
from runExperiments import STOP, terminate
from webSearchWorkload import DEFAULT_OFFERED_MBPS, DEFAULT_WORKLOAD_SEED, MEAN_FLOW_BYTES, generate_flows
from workloadTopology import (
    DEFAULT_LAYOUT, DURATION, LAYOUT_NAMES, PAIRS, PATH_COUNT, TERMINAL_COUNT, get_layout, routing_definitions,
)


PROTOCOLS = {
    'Omega': leo.PROTOCOLS['Omega'],
    'CubicUncoupled': leo.PROTOCOLS['CubicUncoupled'],
    'OrbUncoupled': leo.PROTOCOLS['OrbUncoupled'],
    'OLIA': ('MpTcp', 'MpTcpOlia', 'OLIA'),
    'BALIA': ('MpTcp', 'MpTcpBalia', 'BALIA'),
    'OrbSingle': leo.PROTOCOLS['OrbSingle'],
}
SUBFLOW_COUNTS = tuple(range(1, PATH_COUNT + 1))
DEFAULT_WORKLOAD_SEEDS = tuple(range(DEFAULT_WORKLOAD_SEED, DEFAULT_WORKLOAD_SEED + 5))
ACTIVE_PORT = 33000
PASSIVE_PORT = 1000
PORT_STRIDE = PATH_COUNT + 1  # One meta port followed by the subflow ports.
LIBRARY = leo.PROJECT_DIR / 'src/mporbExperiments'
CDF_SOURCE = 'https://github.com/ht-gong/UCMP/blob/master/traffic/websearch.csv'
INI = 'web-search.ini'
MEMORY_RESERVE = 10_000_000_000
MEMORY_CHECK_SECONDS = 30
MEMORY_WORKER_STEP = 5


@dataclass
class RunningCase:
    job: tuple
    process: subprocess.Popen
    log: Path
    deadline: float


def arrival_window(duration):
    # Short pilot runs keep a proportionate interval for arrivals and drain.
    scale = duration / DURATION
    return 10 * scale, 490 * scale


def port_slots(flows):
    occupied = [0] * TERMINAL_COUNT
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
    by_terminal = [[] for _ in range(TERMINAL_COUNT)]
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
    if shuffle:
        queue_type = 'MpOrbDeltaQueue'
    else:
        queue_type = 'DropTailQueue' if tcp == 'MpTcp' else 'PintQueue'
    for host in ('satellite', 'groundStation', 'userTerminal'):
        queue = f'*.{host}[*].ppp[*].queue'
        lines.append(f'{queue}.typename = "{queue_type}"')
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


def available_memory():
    if sys.platform == 'darwin':
        stats = subprocess.check_output(['/usr/bin/vm_stat'], text=True, timeout=3)
        page_size = re.search(r'page size of (\d+) bytes', stats)
        counts = dict(re.findall(r'^(Pages free|Pages purgeable|File-backed pages):\s+(\d+)', stats, re.M))
        if page_size and len(counts) == 3:
            # Approximate XNU's readily reclaimable pages, excluding compressed memory.
            return int(page_size[1]) * sum(int(count) for count in counts.values())
    elif sys.platform.startswith('linux'):
        with open('/proc/meminfo', encoding='ascii') as stats:
            for line in stats:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
    raise ValueError('Available RAM statistics are missing')


def start_case(job, layout):
    root, config, _, _, workload, timeout = job
    folder = scenario_folder(workload['duration_s'], workload['offered_mbps'])
    log = layout.logs / 'web-search' / folder / f'{config}.log'
    # A killed attempt may leave an unpublished result in the private staging directory.
    for suffix in ('.json', '.json.tmp'):
        (root / f'{config}{suffix}').unlink(missing_ok=True)
    argv = command(config)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('w', encoding='utf-8') as output:
        output.write(json.dumps(argv) + '\n')
        output.flush()
        process = subprocess.Popen(argv, cwd=root, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    return RunningCase(job, process, log, time.monotonic() + timeout)


def finish_case(case, layout):
    root, config, protocol, k, workload, _ = case.job
    log = case.log
    with log.open('a', encoding='utf-8') as output:
        output.write(f'\nExit code: {case.process.returncode}\n')
    if case.process.returncode != 0:
        raise RuntimeError(f'{config} failed; inspect {log}')
    staged = root / f'{config}.json'
    with staged.open(encoding='utf-8') as handle:
        result = json.load(handle)
    if result.get('schema_version') != 1:
        raise ValueError(f'{config} did not produce supported aggregate metrics; inspect {log}')
    if not {'endpoint_pairs', 'transport_recovery', 'link_pressure'}.issubset(result):
        raise ValueError(f'{config} produced outdated workload metrics; rebuild {LIBRARY.parent.parent}')
    if (result.get('offered_flows') != workload['scheduled_flows'] or
            result.get('offered_bytes') != workload['scheduled_bytes']):
        raise ValueError(f'{config} did not register every scheduled transfer; inspect {log}')
    result['case'] = {'controller': protocol, 'requested_subflows': k, 'workload': workload}
    staged.write_text(json.dumps(result, separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')
    destination = layout.results / scenario_folder(workload['duration_s'], workload['offered_mbps']) / staged.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged.replace(destination)
    log.unlink(missing_ok=True)
    print(f'Saved {destination}', flush=True)


def run_cases(jobs, cores, layout):
    pending = deque(jobs)
    running = []
    limit = min(cores, len(jobs))
    completed = 0
    next_memory_check = time.monotonic()
    warned = False

    def finish(case):
        nonlocal completed
        running.remove(case)
        finish_case(case, layout)
        completed += 1
        print(f'Simulations: {completed}/{len(jobs)}', flush=True)

    try:
        while pending or running:
            if STOP.is_set():
                raise KeyboardInterrupt
            now = time.monotonic()
            for case in running[:]:
                if case.process.poll() is not None:
                    finish(case)
                elif now >= case.deadline:
                    terminate(case.process)
                    with case.log.open('a', encoding='utf-8') as output:
                        output.write('\nTIMEOUT\n')
                    raise RuntimeError(f'{case.job[1]} timed out; inspect {case.log}')

            if now >= next_memory_check and (pending or running):
                next_memory_check = now + MEMORY_CHECK_SECONDS
                try:
                    available = available_memory()
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    if not warned:
                        print(f'Unable to check available RAM: {error}. Will retry in 30 seconds.',
                              file=sys.stderr, flush=True)
                    warned = True
                else:
                    warned = False
                    if available <= MEMORY_RESERVE and limit > 1:
                        previous = min(limit, len(running) or len(pending))
                        limit = max(1, previous - MEMORY_WORKER_STEP)
                        print(f'RAM headroom {available / 1e9:.1f} GB. Reducing workers {previous} to {limit}.',
                              flush=True)
                        victims = running[limit:]
                        killed = set()
                        for case in victims:
                            if case.process.poll() is None:
                                try:
                                    # Reclaim RAM immediately and avoid producing a partial result in finish().
                                    os.killpg(case.process.pid, signal.SIGKILL)
                                    killed.add(case.process.pid)
                                except ProcessLookupError:
                                    pass
                        for case in victims:
                            status = case.process.wait()
                            if status == -signal.SIGKILL and case.process.pid in killed:
                                running.remove(case)
                                pending.append(case.job)
                                print(f'Requeued {case.job[1]} after memory cancellation.', flush=True)
                            else:
                                finish(case)

            while pending and len(running) < limit and not STOP.is_set():
                running.append(start_case(pending.popleft(), layout))
            if running:
                time.sleep(min(1, max(0, next_memory_check - time.monotonic())))
        if STOP.is_set():
            raise KeyboardInterrupt
    finally:
        for case in running:
            terminate(case.process)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--layout', choices=LAYOUT_NAMES, default=DEFAULT_LAYOUT,
                        help='Terminal layout (default: %(default)s)')
    parser.add_argument('--sim-time', type=float, default=DURATION, help='Simulation duration in seconds (default: 600)')
    parser.add_argument('--route-store', type=Path, help='Override the selected layout’s saved route store')
    parser.add_argument('--offered-mbps', type=float, default=DEFAULT_OFFERED_MBPS,
                        help='Expected application payload offered per second in Mbps (default: %(default)s)')
    parser.add_argument('--workload-seed', nargs='+', type=int, default=DEFAULT_WORKLOAD_SEEDS,
                        help='One or more workload seeds, each shared across controllers and K (default: 1–5)')
    parser.add_argument('--protocols', nargs='+', choices=tuple(PROTOCOLS), default=tuple(PROTOCOLS),
                        help='Controllers to run (default: all six)')
    parser.add_argument('--ks', nargs='+', type=int, choices=SUBFLOW_COUNTS, default=SUBFLOW_COUNTS,
                        help='Requested subflow counts for multipath controllers (default: 1–5)')
    parser.add_argument('--cores', type=int, default=1, help='Maximum simultaneous simulations')
    parser.add_argument('--sim-timeout-seconds', type=float, default=16 * 3600,
                        help='Wall-clock limit per traffic simulation (default: 16 hours)')
    parser.add_argument('--dry-run', action='store_true', help='Generate the workload in memory and print the run scope')
    args = parser.parse_args(argv)
    if not math.isfinite(args.sim_time) or args.sim_time <= 3:
        parser.error('--sim-time must be finite and above 3 seconds')
    if args.cores < 1 or not math.isfinite(args.sim_timeout_seconds) or args.sim_timeout_seconds <= 0:
        parser.error('Cores and wall-clock timeout must be positive')
    seeds = tuple(dict.fromkeys(args.workload_seed))
    if any(seed < 0 for seed in seeds):
        parser.error('--workload-seed values must be nonnegative')

    layout = get_layout(args.layout)
    store = (args.route_store or layout.store).resolve()
    cases = list(dict.fromkeys((protocol, k) for protocol in args.protocols
                              for k in ([1] if protocol == 'OrbSingle' else args.ks)))
    print(f'Web search ({layout.name}): {len(cases) * len(seeds)} runs '
          f'({len(cases)} configurations × {len(seeds)} workload seeds)', flush=True)
    if args.dry_run:
        print(f'Saved routes: {store}\nAggregate results: {layout.results / scenario_folder(args.sim_time, args.offered_mbps)}')
    else:
        routes.check_workload_routes(store, args.sim_time, routing_definitions(layout))
        if not any(LIBRARY.with_name(f'lib{LIBRARY.name}{suffix}').is_file() for suffix in ('.so', '.dylib')):
            raise FileNotFoundError(f'Build the release library for {leo.PROJECT_DIR} before running this workload')
        layout.results.mkdir(parents=True, exist_ok=True)

    start, end = arrival_window(args.sim_time)
    with ExitStack() as batches:
        jobs = []
        for seed in seeds:
            flows = generate_flows(start, end, args.offered_mbps, seed)
            if not flows:
                raise ValueError(f'Workload seed {seed} has no arrivals. Increase the load or duration.')
            slots, max_offset = port_slots(flows)
            payload_bytes = sum(flow.bytes for flow in flows)
            print(f'Seed {seed}: {len(flows):,} downloads over {start:g}–{end:g}s; '
                  f'{args.offered_mbps:g} Mbps offered ({payload_bytes * 8 / (end-start) / 1e6:.1f} Mbps drawn)',
                  flush=True)
            if args.dry_run:
                for protocol, k in cases:
                    print(case_name(protocol, k, seed))
                continue

            root = Path(batches.enter_context(tempfile.TemporaryDirectory(
                prefix=f'.web-search-seed{seed}-', dir=layout.results)))
            schedule = root / 'arrivals.csv'
            write_schedule(schedule, flows, slots)
            write_config(root, store, args.sim_time, max_offset, cases, seed)
            workload = dict(profile='web-search', layout=layout.name, cdf_source=CDF_SOURCE, cdf_mean_bytes=MEAN_FLOW_BYTES,
                            terminals=[name for name, _, _ in layout.terminals],
                            offered_mbps=args.offered_mbps, seed=seed, arrival_start_s=start,
                            arrival_end_s=end, duration_s=args.sim_time, scheduled_flows=len(flows),
                            scheduled_bytes=payload_bytes, schedule_sha256=hashlib.sha256(schedule.read_bytes()).hexdigest())
            jobs.extend((root, case_name(protocol, k, seed), protocol, k, workload, args.sim_timeout_seconds)
                        for protocol, k in cases)
        if jobs:
            run_cases(jobs, args.cores, layout)


def stop(signum, _frame):
    STOP.set()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('Cancelled web-search simulations')
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error))
