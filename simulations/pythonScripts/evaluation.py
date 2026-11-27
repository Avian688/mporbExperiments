"""Generate paired MPOrb evaluation runs and their hard-handover schedules."""

import json
import math
import random
import xml.etree.ElementTree as ET

from experiments import MSS, PROTOCOLS as ORIGINAL_PROTOCOLS, SIM_DIR, general, set_channel, write_scenario


ROOT = SIM_DIR / 'experiments' / 'evaluation'
NED = SIM_DIR.parent / 'src' / 'evaluation' / 'EvaluationDumbbell.ned'
PROTOCOLS = {**ORIGINAL_PROTOCOLS,
             'OLIA': ('MpTcp', 'MpTcpOlia', 'OLIA', '#CC79A7'),
             'BALIA': ('MpTcp', 'MpTcpBalia', 'BALIA', '#65732D')}
MAIN = ('Omega', 'OLIA', 'BALIA', 'CubicUncoupled', 'OrbUncoupled')
MECHANISMS = {'A': ('Delta', 'Omega'), 'B': ('OrbUncoupled', 'Alpha', 'Delta', 'Omega')}
DURATION = 300
TRIALS = 5
RELEASE = 60
RTTS = tuple(range(20, 201, 20))
LINK_RATES = (20, 40, 60, 80, 100)
SWEEPS = {'A': (0, 20, 40, 60, 80, 95), 'B': LINK_RATES, '1': LINK_RATES,
          '2': (1, 2, 3, 4, 5), '3': RTTS, '4': RTTS,
          '5': (3, 5, 10, 15, 20), '6': (1, 2, 4, 6, 8)}
SWEEP_LABELS = {'A': 'Background UDP payload offered (Mb/s)', '1': 'Link 1 capacity (Mb/s)',
                '2': 'Subflows per B and C', '3': 'Connection B base RTT (ms)',
                '4': 'Common base RTT (ms)', '5': 'Competing connections',
                '6': 'Additional connections'}
ACCESS_MBPS = 10_000


def directory(experiment):
    return ROOT / f'experiment{experiment}'


def fair_rates(k):
    """Progressive filling when A can use only the first of K 100 Mb/s links."""
    equal_share = 100 * k / 3
    a = min(100, equal_share)
    return (a, (100 * k - a) / 2, (100 * k - a) / 2)


def scenario(experiment, value, trial, duration=DURATION):
    # Independent streams keep route/outage draws distinct from start-time variation.
    starts_rng = random.Random(7999 + trial)
    routes_rng = random.Random(20_000 + trial)
    handover_rng = random.Random(30_000 + trial)
    join = leave = None
    ideal = None
    if experiment == 'A':
        capacities, paths, rtts = [100, 100], [[0, 1]], [50]
    elif experiment in ('1', 'B'):
        capacities, paths, rtts = [value, 100, 100], [[0, 1], [1, 2], [1, 2]], [50] * 3
        ideal = [(value + 200) / 3] * 3
    elif experiment == '2':
        capacities, paths, rtts = [100] * value, [[0], list(range(value)), list(range(value))], [50] * 3
        ideal = list(fair_rates(value))
    elif experiment in ('3', '4'):
        capacities, paths = [100, 100], [[0, 1], [0, 1]]
        rtts = [20, value] if experiment == '3' else [value, value]
    elif experiment == '5':
        capacities, rtts = [100] * 3, [50] * value
        paths = [routes_rng.sample(range(3), 2) for _ in rtts]
    else:
        capacities, rtts = [100, 100], [50] * (2 + value)
        paths = [[0, 1] for _ in rtts]
        offset = starts_rng.uniform(-0.5, 0.5)
        join, leave = round(100 + offset, 6), round(200 + offset, 6)

    foreground = len(paths)
    starts = [round(starts_rng.uniform(1, 2), 6) for _ in paths]
    if join is not None:
        starts[2:] = [join] * (foreground - 2)
    if experiment == 'A' and value:
        paths = paths + [[0]]
        rtts = rtts + [50]

    events = []
    for time in range(15, math.ceil(duration), 15) if experiment not in MECHANISMS else ():
        updated = [routes_rng.sample(range(3), 2) for _ in rtts] if experiment == '5' else paths
        outage = handover_rng.randint(45, 120) / 1000
        events.append(dict(down=time, up=round(time + outage, 6), paths=updated))

    return dict(experiment=experiment, value=value, trial=trial, duration=duration, capacities=capacities,
                paths=paths, rtts=rtts, starts=starts, events=events, foreground=foreground,
                join=join, leave=leave, ideal=ideal, background=value if experiment == 'A' else 0,
                release=RELEASE if experiment == 'A' else None,
                subflow_metrics=experiment in ('A', 'B', '1'))


def connect(event, source, source_gate, destination, destination_gate, rate, delay):
    node = ET.SubElement(event, 'connect', **{'src-module': source, 'src-gate': f'pppg$o[{source_gate}]',
        'dest-module': destination, 'dest-gate': f'pppg$i[{destination_gate}]',
        'channel-type': 'ned.DatarateChannel'})
    ET.SubElement(node, 'param', name='datarate', value=f'{rate}Mbps')
    ET.SubElement(node, 'param', name='delay', value=f'{delay:.12g}ms')


def disconnect(event, node, gate):
    ET.SubElement(event, 'disconnect', **{'src-module': node, 'src-gate': f'pppg$o[{gate}]'})


def write_routes(path, spec):
    script = ET.Element('scenario')
    initial = ET.SubElement(script, 'at', t='0s')
    for lane, rate in enumerate(spec['capacities']):
        for router in ('left', 'right'):
            set_channel(initial, f'{router}[{lane}]', 0, 'datarate', f'{rate}Mbps')
    previous = spec['paths']
    slots = max(map(len, previous))
    if spec['leave'] is not None and spec['leave'] < spec['duration']:
        departure = ET.SubElement(script, 'at', t=f'{spec["leave"]}s')
        for c in range(2, spec['foreground']):
            # Stop the departing source, including any data still queued below the app.
            ET.SubElement(departure, 'crash', module=f'client[{c}]')
    for event in spec['events']:
        down = ET.SubElement(script, 'at', t=f'{event["down"]}s')
        up = ET.SubElement(script, 'at', t=f'{event["up"]}s')
        for lane, rate in enumerate(spec['capacities']):
            left, right = f'left[{lane}]', f'right[{lane}]'
            for router in (left, right):
                disconnect(down, router, 0)
                ET.SubElement(down, 'crash', module=f'{router}.ppp[0]')
            connect(up, left, 0, right, 0, rate, .5)
            connect(up, right, 0, left, 0, rate, .5)

        rewired = []
        for c, (old, new) in enumerate(zip(previous, event['paths'], strict=True)):
            for rank, (old_lane, new_lane) in enumerate(zip(old, new, strict=True)):
                if old_lane != new_lane:
                    rewired.append((c, rank, old_lane, new_lane))
        # Disconnect every moving rank first so swaps have vacant destination gates.
        for c, rank, old, _ in rewired:
            gate = 1 + c * slots + rank
            for host, side in (('client', 'left'), ('server', 'right')):
                disconnect(down, f'{host}[{c}]', rank)
                disconnect(down, f'{side}[{old}]', gate)
        for c, rank, _, new in rewired:
            gate = 1 + c * slots + rank
            delay = (spec['rtts'][c] - 1) / 4
            for host, side in (('client', 'left'), ('server', 'right')):
                host, router = f'{host}[{c}]', f'{side}[{new}]'
                connect(up, host, rank, router, gate, ACCESS_MBPS, delay)
                connect(up, router, gate, host, rank, ACCESS_MBPS, delay)
        for lane in range(len(spec['capacities'])):
            for router in ('left', 'right'):
                ET.SubElement(up, 'start', module=f'{router}[{lane}].ppp[0]')
        if rewired:
            ET.SubElement(up, 'update', module='configurator')
        previous = event['paths']
    write_scenario(path, script)


def base_config():
    # Reuse the existing OrbCC-aligned transport and scheduler parameters.
    excluded = ('network ', '*.numberOfConnections ', 'sim-time-limit ', '*.client[*].app',
                '*.server[*].app', '*.client[*].numApps', '*.server[*].numApps')
    return [line for line in general(3) if not line.startswith(excluded)] + [
        'network = mporbexperiments.evaluation.EvaluationDumbbell',
        '**.statistic-recording = false', '**.vector-recording = false',
        '**.scalar-recording = false', '**.bin-recording = false', '**.param-recording = false',
    ]


def case_name(spec, controller):
    return f'{controller}_V{spec["value"]}_Run{spec["trial"]}'


def config_lines(spec, controller):
    tcp, algorithm, _, _ = PROTOCOLS[controller]
    name = spec['config']
    paths, rtts = spec['paths'], spec['rtts']
    lines = ['', f'[Config {name}]', 'seed-set = 1', f'sim-time-limit = {spec["duration"]:g}s',
             f'*.numConnections = {len(paths)}', f'*.bottlenecks = {len(spec["capacities"])}',
             f'*.rankSlots = {max(map(len, paths))}', f'*.paths = {json.dumps(paths)}',
             f'*.rttMs = {json.dumps(rtts)}', f'*.recorder.numConnections = {spec["foreground"]}',
             f'*.recorder.firstConnectionRanks = {len(paths[0]) if spec["subflow_metrics"] else 0}',
             f'*.recorder.outputFile = "results/{name}.staged.csv"',
             f'output-vector-file = "results/{name}.staged.vec"',
             f'output-scalar-file = "results/{name}.staged.sca"',
             f'*.scenarioManager.script = xmldoc("{spec["scenario"]}")',
             f'**.tcp.typename = "{tcp}"', f'**.tcp.tcpAlgorithmClass = "{algorithm}"']
    shuffle = controller in ('Delta', 'Omega')
    queue_type = 'MpOrbDeltaQueue' if shuffle else ('DropTailQueue' if tcp == 'MpTcp' else 'PintQueue')
    reference_rtt = sum(rtts[:spec['foreground']]) / spec['foreground']
    for lane, capacity in enumerate(spec['capacities']):
        packets = math.ceil(capacity * 1000 * reference_rtt / (8 * MSS))
        for side in ('left', 'right'):
            queue = f'*.{side}[{lane}].ppp[0].queue'
            lines += [f'{queue}.typename = "{queue_type if side == "left" else "DropTailQueue"}"',
                      f'{queue}.packetCapacity = {packets}']
            if shuffle and side == 'left':
                lines += [f'{queue}.deltaShuffleFraction = 0.05', f'{queue}.deltaShuffleInterval = 0s']
    lines += ['**.ppp[*].queue.typename = "DropTailQueue"', '**.ppp[*].queue.packetCapacity = 1000',
              '*.client[*].numApps = 1', '*.server[*].numApps = 1']
    for c, start in enumerate(spec['starts']):
        ranks = len(paths[c])
        client, server = f'*.client[{c}]', f'*.server[{c}]'
        source, sink = f'{client}.app[0]', f'{server}.app[0]'
        stop = spec['leave'] if spec['leave'] is not None and c >= 2 else spec['duration'] + 1
        lines += [f'{source}.typename = "MpTcpSessionApp"', f'{sink}.typename = "MpTcpSinkApp"',
                  f'{source}.connectAddress = "server[{c}]%ppp0"', f'{source}.connectPort = 1000',
                  f'{source}.localPort = 4000', f'{sink}.localPort = 1000',
                  f'{source}.tOpen = {start}s', f'{source}.tSend = {start}s', f'{source}.tClose = {stop:.12g}s',
                  f'{source}.abortAtClose = {"true" if stop <= spec["duration"] else "false"}',
                  f'{source}.sendBytes = 1MiB', f'{source}.keepBacklogged = true',
                  f'{source}.dataTransferMode = "bytecount"',
                  f'{sink}.serverThreadModuleType = "tcpgoodputapplications.applications.tcpapp.TcpGoodputSinkAppThread"',
                  f'{client}.tcp.**.numberOfSubflows = {ranks}', f'{server}.tcp.**.numberOfSubflows = {ranks}',
                  f'{source}.numberOfSubflows = {ranks}', f'{sink}.numberOfSubflows = {ranks}',
                  f'{client}.tcp.subflowRemoteAddresses = "{" ".join(f"server[{c}]%ppp{r}" for r in range(ranks))}"']
    if rate := spec['background']:
        c = spec['foreground']
        source, sink = f'*.client[{c}].app[0]', f'*.server[{c}].app[0]'
        lines += [f'{source}.typename = "UdpBasicApp"', f'{sink}.typename = "UdpSink"',
                  f'{source}.destAddresses = "server[{c}]"', f'{source}.destPort = 5000', f'{sink}.localPort = 5000',
                  f'{source}.messageLength = {MSS}B', f'{source}.sendInterval = {8 * MSS / (rate * 1e6):.12g}s',
                  f'{source}.startTime = 0.5s', f'{source}.stopTime = {spec["release"]}s']
    return lines


def generate(root, specs):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'results').mkdir(exist_ok=True)
    lines = base_config()
    paths = set()
    for spec in specs:
        relative = spec['scenario']
        if relative not in paths:
            write_routes(root / relative, spec)
            paths.add(relative)
        lines.extend(config_lines(spec, spec['controller']))
    (root / 'evaluation.ini').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    (root / 'matrix.json').write_text(json.dumps(specs, indent=2) + '\n', encoding='utf-8')
