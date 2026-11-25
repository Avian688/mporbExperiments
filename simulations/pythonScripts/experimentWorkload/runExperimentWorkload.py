#!/usr/bin/env python3
"""Run the web-search workload on saved LEO routes."""

import argparse
from collections import deque
from contextlib import ExitStack
import csv
from dataclasses import dataclass
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
from statistics import median
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
DIAGNOSTIC_PROTOCOLS = ('Omega', 'OrbUncoupled')
DIAGNOSTIC_KS = (3, 5)
DIAGNOSTIC_CSVS = ('tcp_vectors.csv', 'flow_outcomes.csv')
DIAGNOSTIC_START = 20
DIAGNOSTIC_END = 120
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


def diagnostic_flow_ids(flows, start, end):
    # Sample across all servers and phases, with the same flow IDs in every run.
    quarter = (end - start) / 4
    groups = {}
    for flow in flows:
        if not start <= flow.start < end:
            continue
        if flow.bytes <= 100_000:
            short = True
        elif flow.bytes > 10_000_000:
            short = False
        else:
            continue
        phase = min(3, int((flow.start - start) / quarter))
        groups.setdefault((flow.server, phase, short), []).append(flow)

    selected = []
    for (_, phase, short), group in groups.items():
        midpoint = start + (phase + 0.5) * quarter
        representative_size = 0 if short else median(flow.bytes for flow in group)
        chosen = min(group, key=lambda flow: (
            0 if short else abs(flow.bytes - representative_size), abs(flow.start - midpoint), flow.id,
        ))
        selected.append(chosen.id)
    return sorted(selected)


def case_name(protocol, k, workload_seed):
    return f'WebSearch_{protocol}_K{k}_Seed{workload_seed}'


def scenario_folder(duration, offered_mbps):
    return Path(f'{duration:.12g}s') / f'{offered_mbps:.12g}Mbps'


def case_scenario_folder(workload):
    return scenario_folder(workload.get('baseline_duration_s', workload['duration_s']), workload['offered_mbps'])


def verify_diagnostic_schedule(results, cases, workload_seed, schedule_hash):
    for protocol, k in cases:
        original = results / f'{case_name(protocol, k, workload_seed)}.json'
        if original.is_file():
            baseline = json.loads(original.read_text(encoding='utf-8'))
            expected = baseline.get('case', {}).get('workload', {}).get('schedule_sha256')
            if expected is not None and expected != schedule_hash:
                raise ValueError(f'Diagnostic workload schedule differs from {original}')


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


def write_config(root, store, duration, max_offset, cases, workload_seed, *, diagnostic_ids=()):
    (root / 'topology.ini').write_bytes((store / 'topology.ini').read_bytes())
    lines = shared_config(store, duration, max_offset)
    for protocol, k in cases:
        lines.extend(case_config(protocol, k, workload_seed))
        if diagnostic_ids:
            config = case_name(protocol, k, workload_seed)
            ids = ' '.join(map(str, diagnostic_ids))
            lines.extend((f'sim-time-limit = {DIAGNOSTIC_END}s',
                          f'*.workloadMetrics.diagnosticDir = "{config}.diagnostics"',
                          f'*.workloadMetrics.diagnosticFlowIds = "{ids}"'))
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


def start_case(job, layout, *, diagnostic=False):
    root, config, _, _, workload, timeout = job
    log_dir = layout.logs / 'web-search' / case_scenario_folder(workload)
    if diagnostic:
        log_dir /= 'diagnostics'
        diagnostics = root / f'{config}.diagnostics'
        if diagnostics.exists():
            shutil.rmtree(diagnostics)
        diagnostics.mkdir()
    log = log_dir / f'{config}.log'
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


def finish_case(case, layout, *, diagnostic=False):
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
    if diagnostic and result.get('duration_s') != workload['duration_s']:
        raise ValueError(f'{config} did not use the diagnostic time limit; inspect {log}')
    result['case'] = {'controller': protocol, 'requested_subflows': k, 'workload': workload}
    staged.write_text(json.dumps(result, separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')
    results = layout.results / case_scenario_folder(workload)
    if diagnostic:
        diagnostics = root / f'{config}.diagnostics'
        for name in DIAGNOSTIC_CSVS:
            csv_path = diagnostics / name
            if not csv_path.is_file() or not csv_path.stat().st_size:
                raise ValueError(f'{config} did not produce {name}; inspect {log}')
            with csv_path.open('rb') as source, gzip.open(csv_path.with_suffix('.csv.gz'), 'wb', compresslevel=3) as target:
                shutil.copyfileobj(source, target)
            csv_path.unlink()
        staged.replace(diagnostics / staged.name)
        destination = results / 'diagnostics' / config
        backup = root / f'{config}.previous'
        if destination.exists():
            destination.rename(backup)
        try:
            diagnostics.rename(destination)
        except OSError:
            if backup.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        print(f'Saved {destination}', flush=True)
        return

    destination = results / staged.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged.replace(destination)
    log.unlink(missing_ok=True)
    print(f'Saved {destination}', flush=True)


def run_cases(jobs, cores, layout, *, diagnostic=False):
    pending = deque(jobs)
    running = []
    limit = min(cores, len(jobs))
    completed = 0
    next_memory_check = time.monotonic()
    warned = False

    def finish(case):
        nonlocal completed
        running.remove(case)
        finish_case(case, layout, diagnostic=diagnostic)
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
                running.append(start_case(pending.popleft(), layout, diagnostic=diagnostic))
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
    parser.add_argument('--workload-seed', nargs='+', type=int,
                        help='One or more workload seeds, each shared across controllers and K (default: 1–5)')
    parser.add_argument('--protocols', nargs='+', choices=tuple(PROTOCOLS),
                        help='Controllers to run (default: all six)')
    parser.add_argument('--ks', nargs='+', type=int, choices=SUBFLOW_COUNTS,
                        help='Requested subflow counts for multipath controllers (default: 1–5)')
    parser.add_argument('--diagnostic', action='store_true',
                        help='Observe 20–120 s of the 600 s, 640 Mb/s Eastbound schedule (seed 1, Omega and OrbUncoupled at K=3 and K=5)')
    parser.add_argument('--cores', type=int, default=1, help='Maximum simultaneous simulations')
    parser.add_argument('--sim-timeout-seconds', type=float, default=16 * 3600,
                        help='Wall-clock limit per traffic simulation (default: 16 hours)')
    parser.add_argument('--dry-run', action='store_true', help='Generate the workload in memory and print the run scope')
    args = parser.parse_args(argv)
    if not math.isfinite(args.sim_time) or args.sim_time <= 3:
        parser.error('--sim-time must be finite and above 3 seconds')
    if args.cores < 1 or not math.isfinite(args.sim_timeout_seconds) or args.sim_timeout_seconds <= 0:
        parser.error('Cores and wall-clock timeout must be positive')
    seeds = tuple(dict.fromkeys(args.workload_seed or ((1,) if args.diagnostic else DEFAULT_WORKLOAD_SEEDS)))
    protocols = args.protocols or (DIAGNOSTIC_PROTOCOLS if args.diagnostic else PROTOCOLS)
    subflow_counts = args.ks or (DIAGNOSTIC_KS if args.diagnostic else SUBFLOW_COUNTS)
    if any(seed < 0 for seed in seeds):
        parser.error('--workload-seed values must be nonnegative')
    if args.diagnostic:
        if (args.layout, args.sim_time, args.offered_mbps, seeds) != ('eastbound', 600, 640, (1,)):
            parser.error('--diagnostic requires the 600 s Eastbound schedule at 640 Mb/s and workload seed 1')
        if set(protocols) - set(DIAGNOSTIC_PROTOCOLS) or set(subflow_counts) - set(DIAGNOSTIC_KS):
            parser.error('--diagnostic supports Omega and OrbUncoupled at K=3 and K=5')

    layout = get_layout(args.layout)
    store = (args.route_store or layout.store).resolve()
    cases = list(dict.fromkeys((protocol, k) for protocol in protocols
                              for k in ([1] if protocol == 'OrbSingle' else subflow_counts)))
    scenario = scenario_folder(args.sim_time, args.offered_mbps)
    staging_parent = layout.results / scenario / 'diagnostics' if args.diagnostic else layout.results
    print(f'Web search ({layout.name}): {len(cases) * len(seeds)} runs '
          f'({len(cases)} configurations × {len(seeds)} workload seeds)', flush=True)
    if args.dry_run:
        print(f'Saved routes: {store}')
        if args.diagnostic:
            print(f'Diagnostic results: {staging_parent}')
            print(f'Diagnostic logs: {layout.logs / "web-search" / scenario / "diagnostics"}')
        else:
            print(f'Aggregate results: {layout.results / scenario}')
    else:
        routes.check_workload_routes(store, args.sim_time, routing_definitions(layout))
        if not any(LIBRARY.with_name(f'lib{LIBRARY.name}{suffix}').is_file() for suffix in ('.so', '.dylib')):
            raise FileNotFoundError(f'Build the release library for {leo.PROJECT_DIR} before running this workload')
        staging_parent.mkdir(parents=True, exist_ok=True)

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
            scheduled_flows, scheduled_slots = flows, slots
            scheduled_bytes = payload_bytes
            duration, arrival_end = args.sim_time, end
            diagnostic_ids = ()
            if args.diagnostic:
                duration = arrival_end = DIAGNOSTIC_END
                scheduled_flows = [flow for flow in flows if flow.start < arrival_end]
                scheduled_slots = slots[:len(scheduled_flows)]
                scheduled_bytes = sum(flow.bytes for flow in scheduled_flows)
                diagnostic_ids = diagnostic_flow_ids(scheduled_flows, DIAGNOSTIC_START, DIAGNOSTIC_END)
                if not diagnostic_ids:
                    raise ValueError(f'No eligible diagnostic downloads in workload seed {seed}')
                print(f'Run to {duration:g}s with {len(scheduled_flows):,} arrivals from the original schedule. '
                      f'Sample {len(diagnostic_ids)} selected downloads over {DIAGNOSTIC_START}–{DIAGNOSTIC_END}s.', flush=True)
            if args.dry_run:
                for protocol, k in cases:
                    print(case_name(protocol, k, seed))
                continue

            root = Path(batches.enter_context(tempfile.TemporaryDirectory(
                prefix=f'.web-search-seed{seed}-', dir=staging_parent)))
            schedule = root / 'arrivals.csv'
            write_schedule(schedule, scheduled_flows, scheduled_slots)
            schedule_hash = hashlib.sha256(schedule.read_bytes()).hexdigest()
            workload = dict(profile='web-search', layout=layout.name, cdf_source=CDF_SOURCE, cdf_mean_bytes=MEAN_FLOW_BYTES,
                            terminals=[name for name, _, _ in layout.terminals],
                            offered_mbps=args.offered_mbps, seed=seed, arrival_start_s=start,
                            arrival_end_s=arrival_end, duration_s=duration, scheduled_flows=len(scheduled_flows),
                            scheduled_bytes=scheduled_bytes, schedule_sha256=schedule_hash)
            if args.diagnostic:
                archive = root / 'original_arrivals.csv'
                write_schedule(archive, flows, slots)
                baseline_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
                verify_diagnostic_schedule(layout.results / scenario, cases, seed, baseline_hash)
                archive.replace(staging_parent / schedule.name)
                prefix = root / 'diagnostic_arrivals.csv'
                shutil.copyfile(schedule, prefix)
                prefix.replace(staging_parent / prefix.name)
                cohort = root / 'selected_flows.csv'
                selected = set(diagnostic_ids)
                selected_rows = [(flow, slot) for flow, slot in zip(scheduled_flows, scheduled_slots, strict=True)
                                 if flow.id in selected]
                write_schedule(cohort, *zip(*selected_rows, strict=True))
                cohort.replace(staging_parent / cohort.name)
                workload.update(baseline_duration_s=args.sim_time, baseline_arrival_end_s=end,
                                baseline_schedule_sha256=baseline_hash, tcp_recording_start_s=DIAGNOSTIC_START,
                                tcp_recording_end_s=DIAGNOSTIC_END)
                print(f'Seed {seed} original schedule SHA-256: {baseline_hash}\n'
                      f'Diagnostic schedule prefix SHA-256: {schedule_hash}', flush=True)
            write_config(root, store, args.sim_time, max_offset, cases, seed, diagnostic_ids=diagnostic_ids)
            jobs.extend((root, case_name(protocol, k, seed), protocol, k, workload, args.sim_timeout_seconds)
                        for protocol, k in cases)
        if jobs:
            run_cases(jobs, args.cores, layout, diagnostic=args.diagnostic)


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
