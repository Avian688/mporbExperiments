#!/usr/bin/env python3
"""Export ExperimentLeo's configured forward-route sharing without simulating."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
import csv
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import leo

VERSION = 1
TOOLS = leo.PROJECT_DIR / 'tools'
FIELDS = ('start_s', 'end_s', 'node_a_id', 'node_a', 'node_b_id', 'node_b',
          'link_type', 'connection_count', 'subflow_count', 'a_to_b_count',
          'b_to_a_count', 'connections', 'subflows_a_to_b', 'subflows_b_to_a')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def temporary(path):
    return path.with_name(f'.{path.name}.{os.getpid()}.tmp')


def write_json(path, value):
    temp = temporary(path)
    try:
        temp.write_text(json.dumps(value, indent=2) + '\n')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def snapshot_inventory(directory, duration_us):
    if not directory.is_dir():
        raise FileNotFoundError(f'Missing saved routes: {directory}')
    files = []
    for path in directory.glob('*.bin'):
        try:
            timestamp = Decimal(path.stem) * 1000000
            if not timestamp.is_finite() or timestamp != int(timestamp):
                raise ValueError
        except (InvalidOperation, ValueError, OverflowError):
            raise ValueError(f'Invalid saved-route timestamp: {path}') from None
        if 0 <= timestamp < duration_us:
            files.append((int(timestamp), path.name, *leo.stat(path)))
    if not files:
        raise FileNotFoundError(f'No route snapshots for the requested interval: {directory}')
    return sorted(files)


def helper_sources():
    codec = leo.SAMPLES_DIR / 'leosatellites/src/networklayer/configurator/ipv4'
    return [TOOLS/'exportLeoRouteFrames.cc', TOOLS/'Makefile',
            *[codec/(name+suffix) for name in ('LeoKPathSnapshot', 'LeoRouteSnapshot',
                                              'LeoKShortestPaths') for suffix in ('.cc', '.h')]]


def prepare_frames(store, duration, include_primary, root):
    """Decode the large binary corpus once; publish only completely valid output."""
    primary, catalog = leo.route_directories(store)
    duration_us = round(duration * 1000000)
    identity = dict(version=VERSION, duration_us=duration_us, primary=include_primary,
                    catalog=str(catalog), k_files=snapshot_inventory(catalog, duration_us),
                    sources=[hashlib.sha256(p.read_bytes()).hexdigest() for p in helper_sources()])
    if include_primary:
        identity['primary_files'] = snapshot_inventory(primary, duration_us)
    fingerprint = digest(identity)
    frames, receipt = root/'routes.jsonl.gz', root/'routes.json'
    try:
        saved = json.loads(receipt.read_text())
        compatible = fingerprint
        # A complete export already includes all multipath routes. Selecting
        # one multipath controller must not discard that richer cache and force
        # another expensive primary-corpus read on the next complete export.
        if not include_primary and saved.get('inputs', {}).get('primary'):
            compatible = digest(dict(identity, primary=True,
                primary_files=snapshot_inventory(primary, duration_us)))
        if saved['fingerprint'] == compatible and saved['stat'] == leo.stat(frames):
            print('Link sharing: reuse validated saved-route cache', flush=True)
            return frames, compatible
    except (OSError, ValueError, KeyError):
        pass
    root.mkdir(parents=True, exist_ok=True)
    print('Link sharing: prepare saved routes (one native decoding pass; no simulation)', flush=True)
    # This is a small standalone codec utility, not an OMNeT++ project build.
    try:
        subprocess.run(['make', '-C', str(TOOLS)], check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError('Cannot build the route reader; a C++17 compiler and pkg-config igraph are required') from error
    command = [str(TOOLS/'exportLeoRouteFrames'), '--k-dir', str(catalog),
               '--duration-us', str(duration_us)]
    if include_primary:
        command += ['--primary-dir', str(primary)]
    temp = temporary(frames)
    process = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE)
        with gzip.open(temp, 'wb', compresslevel=1) as output:
            shutil.copyfileobj(process.stdout, output)
        process.stdout.close()
        if process.wait() != 0:
            raise RuntimeError('Saved-route decoding failed; no incomplete cache was published')
        os.replace(temp, frames)
        write_json(receipt, dict(fingerprint=fingerprint, stat=leo.stat(frames), inputs=identity))
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout:
                process.stdout.close()
        temp.unlink(missing_ok=True)
    return frames, fingerprint


def node_name(node, core_nodes):
    if node >= core_nodes:
        return f'userTerminal[{node-core_nodes}]'
    if node >= 1584:
        return f'groundStation[{node-1584}]'
    return f'satellite[{node}]'


def sharing(frame, config, time_us, pairs):
    """Physical links are unordered; members retain their forwarding direction."""
    routes = {(r['source'], r['destination']): r for r in frame['routes']}
    links, missing, active = {}, [], []
    for number, (pair, start) in enumerate(zip(pairs, config['starts']), 1):
        if time_us < round(start * 1000000):
            continue
        connection = f'C{number}'
        active.append(connection)
        route = routes[pair['source'], pair['destination']]
        paths = [route['primary']] if config['protocol'] == 'OrbSingle' else route['paths'][:config['k']]
        for rank in range(1, config['k'] + 1):
            path = paths[rank-1] if rank <= len(paths) else None
            member = f'{connection}:SF{rank}'
            if not path:
                reason = route['primary_status'] if config['protocol'] == 'OrbSingle' else 'rank_unavailable'
                missing.append((member, reason))
                continue
            # Count a subflow once on a physical link, even in a malformed looping route.
            seen = set()
            for source, destination in zip(path, path[1:]):
                if source == destination:
                    continue
                edge = tuple(sorted((source, destination)))
                if edge not in seen:
                    links.setdefault(edge, [[], []])[source != edge[0]].append(member)
                    seen.add(edge)
    shared = {edge: (tuple(sorted(forward)), tuple(sorted(reverse)))
              for edge, (forward, reverse) in links.items() if len(forward)+len(reverse) > 1}
    return shared, tuple(missing), tuple(active)


def extend_range(ranges, start, end, value=None):
    if ranges and ranges[-1]['end_s'] == start/1e6 and ranges[-1].get('members') == value:
        ranges[-1]['end_s'] = end/1e6
    else:
        item = dict(start_s=start/1e6, end_s=end/1e6)
        if value is not None:
            item['members'] = value
        ranges.append(item)


def profile_name(config):
    policy = 'OrbSingle' if config['protocol'] == 'OrbSingle' else 'EdgeDisjoint'
    return f'{policy}_K{config["k"]}_Run{config["run"]}'


def profile_identity(config, fingerprint, pairs):
    return dict(version=VERSION, routes=fingerprint, k=config['k'], starts=config['starts'],
                duration=config['sim_time'], single=config['protocol']=='OrbSingle', pairs=pairs)


def export_profile(job):
    """One CPU worker per unique policy/K/start pattern; never parse packet vectors."""
    frames, fingerprint, config, pairs, root = job
    name = profile_name(config)
    csv_path, metadata_path = root/(name+'.csv'), root/(name+'.json')
    signature = digest(profile_identity(config, fingerprint, pairs))
    try:
        previous = json.loads(metadata_path.read_text())
        if previous['signature'] == signature and previous['csv_stat'] == leo.stat(csv_path):
            return name
    except (OSError, ValueError, KeyError):
        pass
    temp = temporary(csv_path)
    no_sharing, unavailable, activity = [], [], []
    pending, intervals, frames_seen, rows = {}, 0, 0, 0
    expected_start = 0
    duration_us = round(config['sim_time']*1e6)
    starts = [round(s*1e6) for s in config['starts']]
    root.mkdir(parents=True, exist_ok=True)
    try:
        with gzip.open(frames, 'rt') as source, temp.open('w', newline='') as output:
            writer = csv.DictWriter(output, FIELDS)
            writer.writeheader()

            def finish(edge):
                nonlocal rows
                start, end, members, core = pending.pop(edge)
                forward, reverse = members
                connections = sorted({m.split(':')[0] for m in forward+reverse})
                a, b = edge
                writer.writerow(dict(start_s=f'{start/1e6:.6f}', end_s=f'{end/1e6:.6f}',
                    node_a_id=a, node_a=node_name(a, core), node_b_id=b, node_b=node_name(b, core),
                    link_type='access' if b >= core else 'core',
                    connection_count=len(connections), subflow_count=len(forward)+len(reverse),
                    a_to_b_count=len(forward), b_to_a_count=len(reverse),
                    connections=';'.join(connections), subflows_a_to_b=';'.join(forward),
                    subflows_b_to_a=';'.join(reverse)))
                rows += 1

            for line in source:
                frame = json.loads(line)
                start, end = frame['start_us'], frame['end_us']
                if start != expected_start or not start < end <= duration_us:
                    raise ValueError('Saved-route cache does not cover the complete simulation interval')
                expected_start = end
                frames_seen += 1
                boundaries = sorted({start, end, *[s for s in starts if start < s < end]})
                for left, right in zip(boundaries, boundaries[1:]):
                    shared, missing, active = sharing(frame, config, left, pairs)
                    intervals += 1
                    if not shared:
                        extend_range(no_sharing, left, right)
                    if missing:
                        extend_range(unavailable, left, right, [dict(subflow=m, reason=r) for m, r in missing])
                    extend_range(activity, left, right, list(active))
                    for edge in list(pending):
                        if edge not in shared or pending[edge][2] != shared[edge]:
                            finish(edge)
                    for edge, members in shared.items():
                        if edge in pending:
                            pending[edge][1] = right
                        else:
                            pending[edge] = [left, right, members, frame['core_nodes']]
            if expected_start != duration_us:
                raise ValueError('Saved-route cache ends before the simulation interval')
            for edge in sorted(pending):
                finish(edge)
        os.replace(temp, csv_path)
        write_json(metadata_path, dict(version=VERSION, signature=signature,
            csv_stat=leo.stat(csv_path), route_fingerprint=fingerprint,
            profile=name, duration_s=config['sim_time'], requested_subflows=config['k'],
            saved_route_intervals=frames_seen, evaluated_intervals=intervals, shared_link_ranges=rows,
            semantics='Configured forward data routes from scheduled application starts; not measured packet activity. '
                      'Physical links include shared access hops; opposite directions are reported separately. '
                      'Ranges are half-open [start_s,end_s); unchanged membership is coalesced per link. '
                      'Rows are written when a range closes; sort by start_s if chronological order is needed.',
            no_shared_links=no_sharing, unavailable_subflows=unavailable, active_connections=activity))
    finally:
        temp.unlink(missing_ok=True)
    return name


def sender_mapping(config, pairs):
    """Map logical ranks to TCP modules when extracted endpoint metadata exists."""
    from terminalVectors import CsvVectors
    from analyzeExperimentLeo import sender_ranks
    path = leo.vector_dir(config)
    if not (path/'vectors.json').is_file():
        return {}, 'not_extracted'
    try:
        vectors = CsvVectors(path, config)
        result = {f'C{number}:SF{rank}': vector.module
                  for number, pair in enumerate(pairs, 1)
                  for rank, vector in sender_ranks(vectors, pair['source'], config['k'],
                      config['protocol']=='OrbSingle').items()}
        return result, 'mapped_from_endpoint_vector_metadata'
    except (ValueError, KeyError, OSError) as error:
        return {}, str(error)


def publish(config, pairs, root):
    profile = profile_name(config)
    source = root/(profile+'.csv')
    destination = root.parent/(config['config']+'_link_sharing.csv')
    temp = temporary(destination)
    try:
        try:
            os.link(source, temp)
        except OSError:
            shutil.copyfile(source, temp)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    metadata = json.loads((root/(profile+'.json')).read_text())
    senders, status = sender_mapping(config, pairs)
    metadata.update(config=config, csv=destination.name, sender_modules=senders,
        sender_mapping_status=status,
        connections={f'C{index}': dict(pair, scheduled_start_s=start)
                     for index, (pair, start) in enumerate(zip(pairs, config['starts']), 1)})
    write_json(destination.with_suffix('.json'), metadata)


def export_reports(configs, store, cores=2):
    pairs = list(leo.PAIRS)
    if not configs or cores < 1:
        raise ValueError('Select at least one configuration and one worker')
    durations = {c['sim_time'] for c in configs}
    if len(durations) != 1:
        raise ValueError('Selected configurations have different simulation durations')
    for config in configs:
        if len(config['starts']) != len(pairs) or not 1 <= config['k'] <= 10 or any(
                not math.isfinite(s) or s < 0 or s >= config['sim_time'] for s in config['starts']):
            raise ValueError(f'Invalid starts or requested subflow count: {config["config"]}')
    root = leo.EXPERIMENT_DIR/'csvs/link_sharing'
    frames, fingerprint = prepare_frames(store, next(iter(durations)),
                                        any(c['protocol']=='OrbSingle' for c in configs), root)
    unique = {}
    for config in configs:
        name = profile_name(config)
        if name in unique and profile_identity(config, fingerprint, pairs) != profile_identity(unique[name], fingerprint, pairs):
            raise ValueError(f'Conflicting route profile: {name}')
        unique[name] = config
    jobs = [(frames, fingerprint, c, pairs, root) for c in unique.values()]
    workers = min(cores, len(jobs))
    print(f'Link sharing: export {len(jobs)} unique route profiles for {len(configs)} configurations '
          f'({workers} worker processes)', flush=True)
    if workers == 1:
        for job in jobs:
            print('Link sharing: '+export_profile(job), flush=True)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for future in as_completed([pool.submit(export_profile, job) for job in jobs]):
                print('Link sharing: '+future.result(), flush=True)
    for config in configs:
        publish(config, pairs, root)
    print(f'Link sharing: saved per-configuration CSV/JSON reports in {root.parent}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cores', type=int, default=int(os.environ.get('EXPERIMENT_CORES', '2')))
    parser.add_argument('--runs', type=int, choices=range(1, 6), default=5, help='Use the first N start patterns')
    parser.add_argument('--ks', nargs='+', type=int, choices=leo.KS, default=list(leo.KS))
    parser.add_argument('--protocols', nargs='+', choices=tuple(leo.PROTOCOLS), default=list(leo.PROTOCOLS))
    parser.add_argument('--configs', nargs='+', help='Restrict to exact configuration names')
    parser.add_argument('--route-store', type=Path)
    parser.add_argument('--dry-run', action='store_true', help='List reports without reading routes or writing files')
    args = parser.parse_args(argv)
    if args.cores < 1:
        parser.error('--cores must be positive')
    manifest_path = leo.EXPERIMENT_DIR/'matrix.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    selected = [c for c in manifest.get('configurations', leo.configurations())
                if c['protocol'] in args.protocols and c['run'] <= args.runs and
                (c['protocol']=='OrbSingle' or c['k'] in args.ks)]
    if args.configs:
        unknown = set(args.configs)-{c['config'] for c in selected}
        if unknown:
            parser.error(f'Unknown selected configurations: {sorted(unknown)}')
        selected = [c for c in selected if c['config'] in args.configs]
    if not selected:
        parser.error('No configurations selected')
    if args.dry_run:
        print(f'{len(selected)} configuration reports; {len({profile_name(c) for c in selected})} unique routing profiles')
        for config in selected:
            print(config['config']+'_link_sharing.csv')
        return
    export_reports(selected, args.route_store or Path(manifest.get('route_store', leo.DEFAULT_STORE)), args.cores)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
