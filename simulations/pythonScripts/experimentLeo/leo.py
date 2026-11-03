"""Shared route definitions and generated inputs for the five-flow LEO experiment."""
import contextlib
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import random
import struct
import tempfile

SCRIPT_DIR = Path(__file__).resolve().parent
SIM_DIR = SCRIPT_DIR.parents[1]
PROJECT_DIR = SIM_DIR.parent
SAMPLES_DIR = PROJECT_DIR.parent
REPO_DIR = SAMPLES_DIR.parent
EXPERIMENT_DIR = SIM_DIR / 'experiments/experimentLeo'
PING_DIR = SIM_DIR / 'experiments/experimentLeoPing'
PLOT_DIR = SIM_DIR / 'plots/experimentLeo'
SHARED_GENERATOR = SAMPLES_DIR / 'leosatellites/simulations/pythonScripts/routingExamples/generateRoutingExamples.py'
DEFAULT_STORE = SAMPLES_DIR / 'leosatellites/simulations/leoSaves'
PROJECTS = ('inet4.5', 'os3', 'leosatellites', 'tcpGoodputApplications', 'tcpPaced',
            'cubic', 'orbtcp', 'mptcp', 'mporb')
PROTOCOLS = {
    'Alpha': ('MpOrb', 'MpOrbSemiCoupledAlpha', 'MPORB Alpha'),
    'Delta': ('MpOrb', 'MpOrbDelta', 'MPORB Delta'),
    'Omega': ('MpOrb', 'MpOrbOmega', 'MPORB Omega'),
    'CubicUncoupled': ('MpTcp', 'MpTcpMetaCubic', 'CUBIC uncoupled'),
    'OrbUncoupled': ('MpOrb', 'MpOrbUncoupled', 'OrbCC uncoupled'),
    'OrbSingle': ('Orbtcp', 'OrbtcpPintFlavour', 'OrbCC single path'),
}
KS = (1, 2, 3, 4, 5)
RUNS = (1, 2, 3, 4, 5)
DURATION = 300
WARMUP = 10
MIN_BASELINE_MATCH = .95


def shared_definitions():
    spec = importlib.util.spec_from_file_location('shared_leo_routing', SHARED_GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SHARED = shared_definitions()
PAIRS = tuple(dict(pair=key, source_city=src_city, destination_city=dst_city,
                   source=src, destination=dst, group=group)
              for group, (key, src_city, dst_city, src, dst) in enumerate(SHARED.PAIR_DEFINITIONS)
              if 5 <= src < 10 and dst == src + 5)
if len(PAIRS) != 5:
    raise ValueError('The shared route definitions must contain all five NY/UK pairs')


def configurations(duration=DURATION):
    if not math.isfinite(duration) or duration <= WARMUP + 2:
        raise ValueError('Duration must leave a measurement interval after the 10s warmup')
    matrix = []
    for protocol in PROTOCOLS:
        for k in ((1,) if protocol == 'OrbSingle' else KS):
            for run in RUNS:
                rng = random.Random(7999 + run)
                matrix.append(dict(config=f'{protocol}_EdgeDisjoint_K{k}_Run{run}',
                    protocol=protocol, k=k, run=run, seed=1,
                    starts=[round(rng.uniform(1, 2), 6) for _ in PAIRS],
                    sim_time=duration, measurement_start=WARMUP, measurement_end=duration - 1))
    return matrix


def ping_config(duration=DURATION):
    return dict(config='LeoPing', protocol='Ping', k=SHARED.PATH_COUNT,
                run=1, seed=1, sim_time=duration)


def directory(config):
    return PING_DIR if config['protocol'] == 'Ping' else EXPERIMENT_DIR


def ini_file(config):
    return directory(config) / ('experimentLeoPing.ini' if config['protocol'] == 'Ping' else 'experimentLeo.ini')


def result(config, suffix):
    return directory(config) / 'results' / f'{config["config"]}-#0{suffix}'


def summary_path(config):
    return directory(config) / 'csvs' / f'{config["config"]}.json'


def stat(path):
    info = Path(path).stat()
    return [info.st_size, info.st_mtime_ns]


def input_hash(config):
    digest = hashlib.sha256()
    for path in (ini_file(config), EXPERIMENT_DIR / 'topology.ini'):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def write_topology(duration):
    # Run the existing generator in isolation. Do not rewrite its consumer INIs,
    # generate routes, or alter any files in leosatellites.
    generator = shared_definitions()
    with tempfile.TemporaryDirectory(prefix='leo-inputs-') as temp:
        generator.EXPERIMENT_DIR = Path(temp)
        generator.INI_FILE = Path(temp) / 'routing.ini'
        generator.VISUALIZATION_INI_FILE = Path(temp) / 'visualize.ini'
        with contextlib.redirect_stdout(io.StringIO()):
            generator.generate_ini(duration)
        topology = (Path(temp) / 'topology.ini').read_text()
        # The shared saver enables broad ping statistics. Consumers own their
        # recording policy; retain geometry/routing only from the shared include.
        topology = '\n'.join(line for line in topology.splitlines() if not any(
            key in line for key in ('vector-recording', 'scalar-recording',
                                    'statistic-recording', 'bin-recording')))
        (EXPERIMENT_DIR / 'topology.ini').write_text(topology + '\n')


def replay_lines(store, root):
    relative = os.path.relpath(Path(store).resolve(), root)
    return [f'*.configurator.configLocation = {json.dumps(relative)}',
        '**.loadFiles = true', '*.configurator.kPathSnapshotMode = "load"',
        '*.configurator.kPathCoreSnapshotProfile = ""',
        '*.configurator.kPathsEdgeDisjoint = true', '*.configurator.kPathMaxSharedLinks = -1',
        '# This legacy-named flag enables saved-path TCP routing too; it creates no ping apps.',
        '*.configurator.kPathPingRouting = true', '*.configurator.allowRouteSnapshotOverwrite = false']


def recording_lines(ping=False):
    entries = []
    metrics = ('rtt', 'kPathAvailable') if ping else ('goodput', 'rtt')
    if not ping:
        entries += ['**.tcp.conn-temp.**.statistic-recording = false',
                    '**.tcp.conn-temp.**.vector-recording = false']
    for metric in metrics:
        if ping or metric == 'kPathAvailable':
            scopes = [f'*.userTerminal[{p["source"]}].app[*]' for p in PAIRS]
        elif metric == 'goodput':
            scopes = [f'*.userTerminal[{p["destination"]}].app[0].thread_*' for p in PAIRS]
        else:
            scopes = [f'*.userTerminal[{p["source"]}].tcp.conn-*' for p in PAIRS]
        mode = 'vector' if metric in ('goodput', 'rtt') else 'vector(removeRepeats)'
        for scope in scopes:
            entries += [f'{scope}.{metric}.statistic-recording = true',
                        f'{scope}.{metric}.result-recording-modes = {mode}',
                        f'{scope}.{metric}:{mode}.vector-recording = true']
    return entries + ['**.statistic-recording = false', '**.vector-recording = false',
                      '**.scalar-recording = false', '**.bin-recording = false']


def app_lines(prefix, values):
    return [f'{prefix}.{name} = {value}' for name, value in values.items()]


def probe_lines(source, destination, group, rank, app_index, duration, monitor=False):
    prefix = f'*.userTerminal[{source}].app[{app_index}]'
    values = dict(typename='"KShortestPathPingApp"' if rank else '"PingApp"',
                  destAddr=f'"userTerminal[{destination}]"', count=-1,
                  startTime='0.000002s' if monitor else f'{1 + .001 * app_index + .0001 * source:g}s',
                  stopTime=f'{duration if monitor else duration - 1:g}s',
                  sendInterval='100ms', printPing='false')
    if rank:
        values.update(pathGroup=group, pathIndex=rank, monitorOnly=str(monitor).lower())
    if not monitor:
        values['packetSize'] = '64B'
    return app_lines(prefix, values)


def generate(duration=DURATION, store=DEFAULT_STORE):
    matrix = configurations(duration)
    EXPERIMENT_DIR.mkdir(parents=True, exist_ok=True)
    PING_DIR.mkdir(parents=True, exist_ok=True)
    write_topology(duration)
    transport = ['include topology.ini', '', '[Config LeoTransport]', *replay_lines(store, EXPERIMENT_DIR),
        '**.interfaceType = "leosatellites.linklayer.ppp.PppInterfaceMutable"',
        '**.tcp.advertisedWindow = 200000000', '**.tcp.windowScalingSupport = true',
        '**.tcp.windowScalingFactor = -1', '**.tcp.increasedIWEnabled = true',
        '**.tcp.delayedAcksEnabled = false', '**.tcp.timestampSupport = true',
        '**.tcp.ecnWillingness = false', '**.tcp.nagleEnabled = true',
        '**.tcp.sackSupport = true', '**.tcp.updatedSackEnabled = true',
        '**.tcp.stopOperationTimeout = 4000s', '**.tcp.mss = 1200',
        '**.tcp.initialSsthresh = 4800000', '**.tcp.sendQueueLimit = 4MiB',
        '**.tcp.subflowInterfaceBinding = false', '**.schedulerMode = "defaultCwnd"',
        '**.startAllSubflowsAtBeginning = true', '**.subflowStartTimes = ""',
        '**.additiveIncreasePercent = 0.05', '**.eta = 0.95', '**.alpha = 0.03',
        '**.tcp.pintFeedbackProbability = 1', '**.pintFlowCountBits = 8',
        '**.pintMaxFlowCount = 65535', '**.pintUseAverageRtt = true',
        '**.tcp.pintUseInitialPhase = true', '**.tcp.pintUseInitialPhaseFlowCount = true',
        '**.ppp[*].queue.packetCapacity = 300', '**.ppp[*].queue.pintInitialRtt = 10ms',
        '**.ppp[*].queue.flowCountSketchEnabled = true', '**.ppp[*].queue.flowCardinalityBits = 4096',
        '**.ppp[*].queue.flowSketchSeed = 1337', '**.ppp[*].queue.pintBits = 8',
        '**.ppp[*].queue.pintAutoScaleEncoding = false', '**.ppp[*].queue.pintLogBase = 1.05',
        '**.goodputInterval = 1s', '**.throughputInterval = 1s',
        '**.ipv4.ip.kPathTcpClientPort = 4000', '**.ipv4.ip.kPathTcpServerPort = 1000',
        *recording_lines()]
    for c in matrix:
        single = c['protocol'] == 'OrbSingle'
        tcp, algorithm, _ = PROTOCOLS[c['protocol']]
        transport += ['', f'[Config {c["config"]}]', 'extends = LeoTransport',
            f'seed-set = {c["seed"]}', f'sim-time-limit = {duration:g}s',
            f'output-vector-file = "results/{c["config"]}-#0.vec"',
            f'output-scalar-file = "results/{c["config"]}-#0.sca"',
            f'**.tcp.typename = "{tcp}"', f'**.tcp.tcpAlgorithmClass = "{algorithm}"',
            f'**.numberOfSubflows = {c["k"]}',
            f'**.ipv4.ip.kPathTcpPathGroups = "{"" if single else " ".join(str(p["group"]) for p in PAIRS)}"',
            '**.ipv4.ip.kPathTcpPathGroup = -1', f'**.ipv4.ip.kPathTcpSubflows = {c["k"]}']
        shuffle = c['protocol'] in ('Delta', 'Omega')
        for host in ('satellite', 'groundStation', 'userTerminal'):
            transport += [f'*.{host}[*].ppp[*].queue.typename = "{"MpOrbDeltaQueue" if shuffle else "PintQueue"}"']
            if shuffle:
                transport += [f'*.{host}[*].ppp[*].queue.deltaShuffleFraction = 0.05',
                              f'*.{host}[*].ppp[*].queue.deltaShuffleInterval = 0s']
        for pair, start in zip(PAIRS, c['starts']):
            src, dst = pair['source'], pair['destination']
            transport += [f'*.userTerminal[{src}].numApps = 1',
                          f'*.userTerminal[{dst}].numApps = 1']
            transport += app_lines(f'*.userTerminal[{src}].app[0]', dict(
                typename='"TcpGoodputSessionApp"' if single else '"MpTcpSessionApp"',
                localPort=4000, connectPort=1000, connectAddress=f'"userTerminal[{dst}]"',
                tOpen=f'{start}s', tSend=f'{start}s', tClose=f'{duration+1:g}s',
                sendBytes='1MiB', dataTransferMode='"bytecount"'))
            transport += app_lines(f'*.userTerminal[{dst}].app[0]', dict(
                typename='"TcpSinkApp"' if single else '"MpTcpSinkApp"', localPort=1000,
                serverThreadModuleType='"tcpgoodputapplications.applications.tcpapp.TcpGoodputSinkAppThread"'))
        transport += ['*.userTerminal[*].numApps = 0']
    (EXPERIMENT_DIR / 'experimentLeo.ini').write_text('\n'.join(transport) + '\n')
    ping = ['include ../experimentLeo/topology.ini', '', '[Config LeoPing]',
        *replay_lines(store, PING_DIR), *recording_lines(ping=True)]
    for pair in PAIRS:
        ping += [f'*.userTerminal[{pair["source"]}].numApps = {SHARED.PATH_COUNT + 1}']
        for rank in range(SHARED.PATH_COUNT + 1):
            ping += probe_lines(pair['source'], pair['destination'], pair['group'], rank, rank, duration)
    ping += ['*.userTerminal[*].numApps = 0']
    c = ping_config(duration)
    ping[ping.index('[Config LeoPing]')] = '[Config LeoPingBase]'
    ping += ['', f'[Config {c["config"]}]', 'extends = LeoPingBase', f'seed-set = {c["seed"]}',
            f'sim-time-limit = {duration:g}s', f'output-vector-file = "results/{c["config"]}-#0.vec"',
            f'output-scalar-file = "results/{c["config"]}-#0.sca"']
    (PING_DIR / 'experimentLeoPing.ini').write_text('\n'.join(ping) + '\n')
    manifest = dict(duration=duration, route_store=str(Path(store).resolve()), configurations=matrix,
                    pairs=PAIRS, path_count=SHARED.PATH_COUNT, delay_annotation='raw RTT ms',
                    delay_colours='matched excess RTT ms', min_baseline_match=MIN_BASELINE_MATCH)
    (EXPERIMENT_DIR / 'matrix.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return matrix


def route_directories(store):
    gs = len(SHARED.CITY_COORDINATES) + len(SHARED.load_ground_stations())
    core = 1584 + gs
    values = [len(SHARED.PAIR_DEFINITIONS)]
    for _, _, _, a, b in SHARED.PAIR_DEFINITIONS:
        values += [core + a, core + b]
    value_hash = 1469598103934665603
    for value in values:
        for byte in value.to_bytes(4, 'little'):
            value_hash = ((value_hash ^ byte) * 1099511628211) & ((1 << 64) - 1)
    profile = f'v3-edge-disjoint-mincost-k{SHARED.PATH_COUNT}-rtt{SHARED.MAX_RTT_SPREAD_MS}ms-pairs-{value_hash:016x}'
    primary = Path(store).resolve() / f'1584_550_72_22_53_{gs}_ISL'
    return primary, primary / 'kpaths' / SHARED.K_PATH_SNAPSHOT_SET / profile


def check_routes(store, duration):
    expected = {0, *range(100001, math.ceil(duration * 1e6), 100000)}
    digest = hashlib.sha256()
    core = 1584 + len(SHARED.CITY_COORDINATES) + len(SHARED.load_ground_stations())
    for index, directory_path in enumerate(route_directories(store)):
        present = {}
        for path in directory_path.glob('*.bin'):
            try:
                micros = int(Decimal(path.stem) * 1_000_000)
            except (InvalidOperation, ValueError):
                continue
            if micros in expected:
                present[micros] = path
        missing = expected - present.keys()
        if missing:
            raise FileNotFoundError(f'{directory_path}: missing {len(missing)} route snapshots; '
                f'first missing t={min(missing)/1e6:g}s. Generate shared routes with '
                'leosatellites/simulations/pythonScripts/routingExamples/saveRoutes.py '
                '--mode multiple --policies EdgeDisjoint --sim-time ' + str(duration))
        for micros in sorted(expected):
            path = present[micros]
            with path.open('rb') as f:
                header = f.read(96 if index == 0 else 92)
            if len(header) != (96 if index == 0 else 92) or path.stat().st_size % 4:
                raise ValueError(f'Truncated or unaligned route snapshot: {path}')
            words = struct.unpack('<' + 'i' * (len(header)//4), header)
            if words[:3] != ((0x4c454f33, 3, 24) if index == 0 else (0x4c454f4b, 3, 23)):
                raise ValueError(f'Regenerate incompatible route snapshot: {path}')
            timestamp = struct.unpack_from('<q', header, 40 if index == 0 else 16)[0]
            if timestamp != micros:
                raise ValueError(f'Route snapshot timestamp mismatch: {path}')
            if index == 1:
                spread = struct.unpack_from('<d', header, 44)[0]
                if (words[6] != core or words[7] != core + len(SHARED.TERMINAL_DEFINITIONS) or
                    words[8] != SHARED.PATH_COUNT or words[9] != 1 or words[10] != -1 or
                    spread != SHARED.MAX_RTT_SPREAD_MS or words[16] != 2 or
                    words[13] != len(SHARED.PAIR_DEFINITIONS)):
                    raise ValueError(f'Wrong edge-disjoint route catalog: {path}')
            digest.update(json.dumps([str(path), stat(path)]).encode())
    return digest.hexdigest()
