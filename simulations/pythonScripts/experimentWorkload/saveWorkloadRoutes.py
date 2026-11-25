#!/usr/bin/env python3
"""Save the route snapshots for experimentWorkload without running traffic apps."""

import argparse
import errno
import fcntl
import json
import math
from pathlib import Path
import sys
import tempfile


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / 'experimentLeo'))

import leo
import runExperimentLeo as runner
from runExperiments import logged

from workloadTopology import (
    DEFAULT_LAYOUT, DURATION, LAYOUT_NAMES, PATH_COUNT, get_layout,
    routing_definitions, write_topology,
)


INI = 'save-routes.ini'
RECONNECT_MANIFEST = 'reconnect-snapshots.json'
GENERATE = 'GenerateWorkloadRoutes'
REUSE_PRIMARY = 'GenerateWorkloadKPaths'


def write_inputs(store, duration, layout):
    write_topology(store / 'topology.ini', duration, layout)
    (store / INI).write_text('''include topology.ini

[Config GenerateWorkloadRoutes]
seed-set = 1
record-eventlog = false
**.vector-recording = false
**.scalar-recording = false
**.bin-recording = false
**.statistic-recording = false
**.param-recording = false
*.userTerminal[*].numApps = 0
**.loadFiles = false
*.configurator.configLocation = "."
*.configurator.kPathSnapshotMode = "generate"
*.configurator.kPathPingRouting = false
*.configurator.allowRouteSnapshotOverwrite = false

[Config GenerateWorkloadKPaths]
extends = GenerateWorkloadRoutes
**.loadFiles = true
''', encoding='utf-8')


def topology_identity(path):
    return tuple(line for line in path.read_text(encoding='utf-8').splitlines()
                 if not line.startswith('sim-time-limit = '))


def compatible_primary(store, duration, definitions):
    leo.check_routes(store, duration, primary_only=True, definitions=definitions)
    return leo.route_directories(store, definitions=definitions)[0]


def find_primary(args, definitions, target_store, same_topology):
    if args.primary_store is not None:
        return compatible_primary(args.primary_store, args.sim_time, definitions)
    for store in ((target_store, leo.DEFAULT_STORE) if same_topology else (leo.DEFAULT_STORE,)):
        try:
            return compatible_primary(store, args.sim_time, definitions)
        except (FileNotFoundError, ValueError) as error:
            print(f'Primary routes unavailable for reuse: {error}', flush=True)
    return None


def stage_primary(source, target, duration, target_store):
    target.mkdir()
    target_store = target_store.resolve()
    names = ['0.bin', *(f'{tick / 1_000_000:.6f}.bin'
              for tick in range(100001, math.ceil(duration * 1_000_000), 100000))]
    symlinks = 0
    for name in names:
        item = (source / name).resolve(strict=True)
        try:
            (target / name).hardlink_to(item)
        except OSError as error:
            if error.errno not in (errno.EXDEV, errno.EACCES, errno.EPERM) or item.is_relative_to(target_store):
                raise
            (target / name).symlink_to(item)
            symlinks += 1
    if symlinks:
        print(f'{symlinks} primary files are symlinks to the source store; keep that store available.', flush=True)


def reconnect_files(catalog):
    return {path.name: path.stat().st_size for path in sorted((catalog / 'reconnects').glob('*.bin'))}


def check_workload_routes(store, duration, definitions):
    _, catalog = leo.route_directories(store, definitions=definitions)
    leo.check_routes(store, duration, definitions=definitions)
    expected = json.loads((catalog / RECONNECT_MANIFEST).read_text(encoding='utf-8'))
    if reconnect_files(catalog) != expected:
        raise ValueError(f'Reconnect snapshots are missing or have changed: {catalog}')
    return catalog


def command(config):
    args = runner.command(dict(config=config, protocol='Capture'))
    args[args.index('-f') + 1] = INI
    return args


def publish(stage, target):
    backup = stage.parent / 'previous-routes'
    try:
        if target.exists():
            target.rename(backup)
        stage.rename(target)
    except BaseException:
        if backup.exists() and not target.exists():
            backup.rename(target)
        raise


def save_routes(args, layout, definitions):
    store = layout.store
    log = layout.logs / 'save-routes.log'
    store.parent.mkdir(parents=True, exist_ok=True)
    with (store.parent / '.save-routes.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix='.workload-routes-', dir=store.parent) as temporary:
            stage = Path(temporary) / 'routes'
            write_inputs(stage, args.sim_time, layout)
            previous_topology = store / 'topology.ini'
            same_topology = previous_topology.is_file() and (
                topology_identity(previous_topology) == topology_identity(stage / 'topology.ini'))
            if same_topology and not args.rerun and args.primary_store is None:
                try:
                    check_workload_routes(store, args.sim_time, definitions)
                except (FileNotFoundError, ValueError):
                    pass
                else:
                    print(f'Reuse complete workload routes: {store}', flush=True)
                    return

            primary = find_primary(args, definitions, store, same_topology)
            config = GENERATE
            if primary is not None:
                target = leo.route_directories(stage, definitions=definitions)[0]
                stage_primary(primary, target, args.sim_time, store)
                print(f'Reuse primary routes: {primary}', flush=True)
                config = REUSE_PRIMARY

            print(f'{config} ({layout.name}): {args.sim_time:g}s, {len(layout.pairs)} pairs, '
                  f'up to {PATH_COUNT} edge-disjoint paths per pair', flush=True)
            if not logged(command(config), stage, log, args.sim_timeout_seconds):
                raise RuntimeError(f'Workload route generation failed; inspect {log}')
            leo.check_routes(stage, args.sim_time, definitions=definitions)
            _, catalog = leo.route_directories(stage, definitions=definitions)
            (catalog / RECONNECT_MANIFEST).write_text(json.dumps(reconnect_files(catalog), indent=2) + '\n',
                                                       encoding='utf-8')
            publish(stage, store)

    _, catalog = leo.route_directories(store, definitions=definitions)
    print(f'Saved workload routes: {catalog}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--layout', choices=LAYOUT_NAMES, default=DEFAULT_LAYOUT,
                        help='Terminal layout (default: %(default)s)')
    parser.add_argument('--sim-time', type=float, default=DURATION, help='Route coverage in seconds (default: 600)')
    parser.add_argument('--primary-store', type=Path, help='Reuse a compatible primary route store covering the full duration')
    parser.add_argument('--sim-timeout-seconds', type=float, default=72 * 3600,
                        help='Wall-clock limit for the route generation (default: 72 hours)')
    parser.add_argument('--rerun', action='store_true', help='Regenerate the workload catalog even if complete')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Show the route-saving scope without launching OMNeT++')
    mode.add_argument('--check-routes', action='store_true', help='Check the saved snapshot headers and time coverage')
    args = parser.parse_args(argv)
    if not math.isfinite(args.sim_time) or args.sim_time <= 3:
        parser.error('--sim-time must be finite and above 3 seconds')
    if not math.isfinite(args.sim_timeout_seconds) or args.sim_timeout_seconds <= 0:
        parser.error('--sim-timeout-seconds must be finite and positive')

    layout = get_layout(args.layout)
    definitions = routing_definitions(layout)
    _, catalog = leo.route_directories(layout.store, definitions=definitions)
    if args.dry_run:
        print(f'{layout.name}: {len(layout.terminals)} terminals, {len(layout.pairs)} pairs, '
              f'up to {PATH_COUNT} edge-disjoint paths per pair')
        print(f'Save {args.sim_time:g}s of routes to {catalog}')
        print('One route-only simulation if needed. Apps and recording disabled.')
    elif args.check_routes:
        check_workload_routes(layout.store, args.sim_time, definitions)
        print(f'Workload route headers, time coverage, and reconnect files verified for {args.sim_time:g}s: {catalog}')
    else:
        save_routes(args, layout, definitions)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('Cancelled workload route generation')
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error))
