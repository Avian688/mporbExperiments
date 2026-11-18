"""Prepare saved routes for three independent terminal pairs at the same sites."""

import contextlib
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
from pathlib import Path
import struct
import tempfile

import leo


PAIRS = ((0, 1), (2, 3), (4, 5))
ROLES = ('A', 'B', 'Background')
BURSTS = ((20, 40), (60, 80))
SNAPSHOT_SET = 'LeoLoadBalanceBackground'
REPORT = 'route_availability.json'
HEADER = struct.Struct('<23i')
GROUP = struct.Struct('<6i')
PATH = struct.Struct('<i3d')


def checkpoint_file(root, replay_start):
    return root / 'results/replay' / f'{replay_start:g}.leoreplay'


def write_topology(target, duration):
    generator = leo.shared_definitions()
    terminals = list(generator.TERMINAL_DEFINITIONS)
    pairs = []
    for role, (source, destination) in zip(ROLES, PAIRS):
        sender, receiver = f'New York {role}', f'London {role}'
        terminals[source] = (sender, *generator.CITY_COORDINATES['New York'])
        terminals[destination] = (receiver, *generator.CITY_COORDINATES['London'])
        pairs.append((role, sender, receiver, source, destination))
    generator.TERMINAL_DEFINITIONS = tuple(terminals)
    generator.PAIR_DEFINITIONS = tuple(pairs)
    generator.K_PATH_SNAPSHOT_SET = SNAPSHOT_SET
    with tempfile.TemporaryDirectory(prefix='leo-background-topology-') as temporary:
        generator.EXPERIMENT_DIR = Path(temporary)
        generator.INI_FILE = generator.EXPERIMENT_DIR / 'routing.ini'
        generator.VISUALIZATION_INI_FILE = generator.EXPERIMENT_DIR / 'visualize.ini'
        with contextlib.redirect_stdout(io.StringIO()):
            generator.generate_ini(duration)
        topology = (generator.EXPERIMENT_DIR / 'topology.ini').read_text()
    excluded = ('vector-recording', 'scalar-recording', 'bin-recording',
                'statistic-recording', '*.userTerminal[*].numApps')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('\n'.join(line for line in topology.splitlines()
                                if not any(key in line for key in excluded)) + '\n')


def route_directories(store):
    primary, _ = leo.route_directories(store)
    core = 1584 + len(leo.SHARED.CITY_COORDINATES) + len(leo.SHARED.load_ground_stations())
    # Same endpoint-pair identity as makeKPathSnapshotProfileName().
    pair_hash = 1469598103934665603
    ids = [len(PAIRS), *(core + terminal for pair in PAIRS for terminal in pair)]
    for value in ids:
        for byte in value.to_bytes(4, 'little'):
            pair_hash = ((pair_hash ^ byte) * 1099511628211) & ((1 << 64) - 1)
    profile = (f'v3-edge-disjoint-mincost-k{leo.SHARED.PATH_COUNT}'
               f'-rtt{leo.SHARED.MAX_RTT_SPREAD_MS}ms-pairs-{pair_hash:016x}')
    return primary, primary / 'kpaths' / SNAPSHOT_SET / profile, core


def _snapshots(directory, end_us):
    snapshots = {}
    for path in directory.glob('*.bin'):
        try:
            micros = Decimal(path.stem) * 1_000_000
            if micros == int(micros) and 0 <= micros < end_us:
                snapshots[int(micros)] = path
        except (InvalidOperation, OverflowError, ValueError):
            continue
    return snapshots


def _core_paths(path, micros, core):
    data = path.read_bytes()
    try:
        header = HEADER.unpack_from(data)
        metadata = (header[:3], struct.unpack_from('<q', data, 16)[0], header[6:11],
                    struct.unpack_from('<d', data, 44)[0], header[13], header[16])
        expected = ((0x4c454f4b, 3, 23), micros,
                    (core, core + len(leo.SHARED.TERMINAL_DEFINITIONS), leo.SHARED.PATH_COUNT, 1, -1),
                    leo.SHARED.MAX_RTT_SPREAD_MS, len(PAIRS), 2)
        if metadata != expected:
            raise ValueError('unexpected endpoint catalog metadata')
        offset = HEADER.size
        result = {}
        for source, destination in PAIRS:
            src, dst, _, _, count, _ = GROUP.unpack_from(data, offset)
            if (src, dst) != (core + source, core + destination) or not 0 <= count <= leo.SHARED.PATH_COUNT:
                raise ValueError('unexpected endpoint group or rank count')
            offset += GROUP.size
            paths = []
            for _ in range(count):
                node_count, *_ = PATH.unpack_from(data, offset)
                offset += PATH.size
                if not 3 <= node_count <= core + 2:
                    raise ValueError('invalid route node count')
                nodes = struct.unpack_from(f'<{node_count}i', data, offset)
                if (nodes[0], nodes[-1]) != (src, dst):
                    raise ValueError('unexpected route endpoints')
                paths.append(nodes[1:-1])
                offset += 4 * node_count
            result[(source, destination)] = paths
        if offset != len(data):
            raise ValueError('unexpected bytes following endpoint groups')
        return result
    except (struct.error, ValueError) as error:
        raise ValueError(f'{path}: {error}') from error


def _check_catalog(catalog, core, replay_start, duration):
    end_us = math.ceil((replay_start + duration) * 1_000_000)
    ticks = _snapshots(catalog, end_us)
    missing = {0, *range(100001, end_us, 100000)} - ticks.keys()
    if missing:
        raise FileNotFoundError(f'{catalog}: missing {len(missing)} endpoint snapshots, '
                                f'first at {min(missing) / 1e6:g}s; prepare background routes first')
    reconnects = _snapshots(catalog / 'reconnects', end_us)
    frames = sorted({**ticks, **reconnects}.items())
    digest = hashlib.sha256()
    def window(start, end):
        return dict(start=start, end=end, rank_seconds={role: {
            str(rank): 0 for rank in range(1, leo.SHARED.PATH_COUNT + 1)} for role in ROLES})
    bursts = [window(start, end) for start, end in BURSTS]
    for micros, path in sorted(ticks.items()) + sorted(reconnects.items()):
        digest.update(str(path.relative_to(catalog)).encode() + b'\0' + path.read_bytes())
    for (start, path), end in zip(frames, [t for t, _ in frames[1:]] + [end_us]):
        if end <= round(replay_start * 1e6):
            continue
        groups = _core_paths(path, start, core)
        for rank in range(leo.SHARED.PATH_COUNT):
            routes = {paths[rank] for paths in groups.values() if rank < len(paths)}
            if len(routes) > 1:
                raise ValueError(f'{path}: co-located paths disagree at rank {rank + 1}')
        for burst in bursts:
            overlap = max(0, min(end, round((replay_start + burst['end']) * 1e6)) -
                          max(start, round((replay_start + burst['start']) * 1e6)))
            for role, pair in zip(ROLES, PAIRS):
                for rank in range(1, len(groups[pair]) + 1):
                    burst['rank_seconds'][role][str(rank)] += overlap
    for burst in bursts:
        for ranks in burst['rank_seconds'].values():
            for rank in ranks:
                ranks[rank] /= 1e6
    return digest.hexdigest(), dict(replay_start=replay_start, duration=duration,
                                   snapshots=len(ticks), reconnect_snapshots=len(reconnects), bursts=bursts)


def _check_routes(store, root, replay_start, duration, primary_hash):
    _, catalog, core = route_directories(store)
    checkpoint = b''
    if replay_start > 0:
        checkpoint = checkpoint_file(root, replay_start).read_bytes()
        if not checkpoint:
            raise ValueError('Empty background replay checkpoint')
    route_hash, report = _check_catalog(catalog, core, replay_start, duration)
    (root / REPORT).write_text(json.dumps(report, indent=2) + '\n')
    return hashlib.sha256(primary_hash.encode() + route_hash.encode() + checkpoint).hexdigest()


def check_routes(store, root, replay_start, duration):
    primary_hash = leo.check_routes(store, replay_start + duration, primary_only=True)
    return _check_routes(store, root, replay_start, duration, primary_hash)


def report_burst_ranks(root, ranks_by_k, burst_ranks):
    report = json.loads((root / REPORT).read_text())
    for k, foreground in sorted(ranks_by_k.items()):
        for index, burst in enumerate(report['bursts']):
            for role, ranks in zip(ROLES, (*foreground, (burst_ranks[k][index],))):
                availability = burst['rank_seconds'][role]
                for rank in ranks:
                    seconds = availability[str(rank)]
                    if seconds < burst['end'] - burst['start']:
                        suffix = ' (unavailable for the entire burst)' if seconds == 0 else ''
                        print(f'K={k}: {role} rank {rank} available for {seconds:g}s during '
                              f'local {burst["start"]}–{burst["end"]}s{suffix}', flush=True)


def prepare_routes(store, root, replay_start, duration, timeout, *, reuse_existing=False):
    import runExperimentLeo as runner
    from runExperiments import logged

    end = replay_start + duration
    primary_hash = leo.check_routes(store, end, primary_only=True)
    primary, catalog, core = route_directories(store)
    if reuse_existing:
        try:
            route_hash = _check_routes(store, root, replay_start, duration, primary_hash)
        except FileNotFoundError:
            pass
        else:
            print(f'Reuse background routes: {catalog}', flush=True)
            return route_hash
    checkpoint = checkpoint_file(root, replay_start) if replay_start > 0 else None
    receipt = root / 'results/replay' / f'{replay_start:g}.routes.json'
    input_hash = hashlib.sha256((root / 'topology.ini').read_bytes())
    input_hash.update(json.dumps([primary_hash, replay_start, duration]).encode())
    for library in runner.libraries(dict(protocol='Capture')):
        built = next((path for suffix in ('.dylib', '.so')
                      if (path := library.with_name(f'lib{library.name}{suffix}')).is_file()), None)
        if built is None:
            raise FileNotFoundError(f'Build the library {library} before preparing routes')
        input_hash.update(json.dumps([str(built), leo.stat(built)]).encode())
    inputs = input_hash.hexdigest()
    if receipt.is_file():
        try:
            saved = json.loads(receipt.read_text())
            if saved['inputs'] == inputs:
                route_hash = _check_routes(store, root, replay_start, duration, primary_hash)
                if saved['routes'] == route_hash:
                    artifact = 'background routes and replay checkpoint' if checkpoint else 'background routes'
                    print(f'Reuse {artifact}: {catalog}', flush=True)
                    return route_hash
        except (OSError, ValueError, KeyError):
            pass

    receipt.parent.mkdir(parents=True, exist_ok=True)
    catalog.parent.mkdir(parents=True, exist_ok=True)
    capture = f' and capture at {replay_start:g}s' if checkpoint else ''
    print(f'Generate background routes through original {end:g}s{capture}', flush=True)
    with tempfile.TemporaryDirectory(prefix='.prepare-', dir=catalog.parent) as route_temp, \
            tempfile.TemporaryDirectory(prefix='prepare-', dir=receipt.parent) as replay_temp:
        stage = Path(route_temp)
        staged_primary, staged_catalog, _ = route_directories(stage)
        staged_primary.mkdir()
        for path in primary.glob('*.bin'):
            (staged_primary / path.name).symlink_to(path)
        staged_checkpoint = Path(replay_temp) / checkpoint.name if checkpoint else None
        ini = Path(replay_temp) / 'prepare.ini'
        lines = [
            f'include {(root / "topology.ini").resolve()}', '', '[Config PrepareBackgroundReplay]',
            'seed-set = 1', f'sim-time-limit = {end:g}s', '*.userTerminal[*].numApps = 0',
            '**.scalar-recording = false', '**.vector-recording = false',
            '**.bin-recording = false', '**.statistic-recording = false',
            '**.param-recording = false',
            f'*.configurator.configLocation = {json.dumps(str(stage))}',
            '*.configurator.kPathSnapshotMode = "generate"', '*.configurator.kPathsEdgeDisjoint = true',
            '*.configurator.kPathMaxSharedLinks = -1',
        ]
        if staged_checkpoint is not None:
            lines += [f'*.channelConstructor.captureReplayAt = {replay_start:g}s',
                      f'*.channelConstructor.replayStateFile = {json.dumps(str(staged_checkpoint))}']
        ini.write_text('\n'.join(lines) + '\n')
        cmd = runner.command(dict(config='PrepareBackgroundReplay', protocol='Capture', replay_start=replay_start))
        cmd[cmd.index('-f') + 1] = str(ini)
        log = leo.SIM_DIR / 'logs' / root.name / 'prepare-routes.log'
        if not logged(cmd, root, log, timeout):
            raise RuntimeError(f'Background route generation failed; inspect {log}')
        if staged_checkpoint is not None and (not staged_checkpoint.is_file() or not staged_checkpoint.stat().st_size):
            raise RuntimeError(f'Background replay checkpoint missing or empty; inspect {log}')
        _check_catalog(staged_catalog, core, replay_start, duration)
        backup = stage / 'previous-catalog'
        if catalog.exists():
            catalog.rename(backup)
        try:
            staged_catalog.rename(catalog)
        except OSError:
            if backup.exists():
                backup.rename(catalog)
            raise
        if staged_checkpoint is not None:
            staged_checkpoint.replace(checkpoint)
        route_hash = _check_routes(store, root, replay_start, duration, primary_hash)
        staged_receipt = Path(replay_temp) / receipt.name
        staged_receipt.write_text(json.dumps(dict(inputs=inputs, routes=route_hash), indent=2) + '\n')
        staged_receipt.replace(receipt)
    return route_hash
