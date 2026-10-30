"""Two-path responsiveness, inter-connection RTT, and intra-connection RTT tests."""
import argparse
import json
import math
import random
from pathlib import Path
import xml.etree.ElementTree as ET

PROJECT_DIR = Path(__file__).resolve().parents[2]
SIM_DIR = PROJECT_DIR / 'simulations'
SAMPLES_DIR = PROJECT_DIR.parent
REPO_DIR = SAMPLES_DIR.parent
PROTOCOLS = {
    'Delta': ('MpOrb', 'MpOrbDelta', 'Delta', '#882255'),
    'Omega': ('MpOrb', 'MpOrbOmega', 'Omega', '#E69F00'),
    'Alpha': ('MpOrb', 'MpOrbSemiCoupledAlpha', 'Alpha', '#0072B2'),
    'CubicUncoupled': ('MpTcp', 'MpTcpMetaCubic', 'CUBIC uncoupled', '#D55E00'),
}
SHUFFLE = {'Delta', 'Omega'}
RUN_COUNTS = {1: 50, 2: 50, 3: 5, 4: 5}
INTRA_RTTS = tuple(range(20, 201, 20))
INTER_RTTS = tuple(sorted({50, *INTRA_RTTS}))
DURATION = 300
MSS = 1200
# Match the enabled vector set in OrbTCP experiments 1--4, plus two MPTCP essentials.
TCP_METRICS = ('cwnd', 'rtt', 'srtt', 'throughput', 'mbytesInFlight', 'retransmissionRate')
QUEUE_METRICS = ('queueLength', 'bandwidth')
MULTIPATH_METRICS = ('holBlockedBytes', 'metaReinjectedBytes')
METRICS = ('goodput', *TCP_METRICS, *QUEUE_METRICS, *MULTIPATH_METRICS)


def run_numbers(number, limit=None):
    count = RUN_COUNTS[number]
    return range(1, min(count, limit if limit is not None else count) + 1)


def recorded_metrics(number):
    # OrbTCP experiment 4 does not enable the retransmission-rate vector.
    return tuple(metric for metric in METRICS if number != 4 or metric != 'retransmissionRate')


def metric_scopes(metric):
    if metric == 'goodput':
        return ('*.server[*].app[0].thread_*',)
    if metric in QUEUE_METRICS:
        return ('*.left[*].ppp[0].queue', '*.right[*].ppp[0].queue')
    if metric == 'holBlockedBytes':
        return ('*.server[*].tcp.conn-*',)
    if metric == 'metaReinjectedBytes':
        return ('*.client[*].tcp.conn-*',)
    return ('*.client[*].tcp.conn-*', '*.server[*].tcp.conn-*')


def directory(number):
    return SIM_DIR / 'experiments' / f'experiment{number}'


def cases(number):
    if number in (1, 2):
        return [('Responsiveness' if number == 1 else 'ResponsivenessLoss', None, None)]
    if number == 3:
        return [(f'InterRtt_R{rtt}', rtt, None) for rtt in INTER_RTTS]
    if number == 4:
        return [(f'IntraRtt_R{rtt}_{layout}', rtt, layout)
                for rtt in INTRA_RTTS for layout in (('Aligned',) if rtt == 20 else ('Aligned', 'Mirrored'))]
    raise ValueError('Only experiments 1, 2, 3, and 4 are defined')


def configurations(number):
    result = []
    for protocol, (tcp, algorithm, _, _) in PROTOCOLS.items():
        for case, rtt, layout in cases(number):
            for run in run_numbers(number):
                rng = random.Random(7999 + run)
                starts = [round(rng.uniform(1, 2), 6) for _ in range(1 if number <= 2 else 2)]
                rtts = None if number <= 2 else ([[50, 50], [rtt, rtt]] if number == 3 else
                    [[20, 20], [20, rtt] if layout == 'Aligned' else [rtt, 20]])
                result.append(dict(config=f'{protocol}_{case}_Run{run}', experiment=number,
                    protocol=protocol, tcp=tcp, algorithm=algorithm, case=case, run=run,
                    seed=7999 + run, starts=starts, rtt_ms=rtt, layout=layout,
                    path_rtts_ms=rtts, connections=len(starts), subflows=2,
                    sim_time=DURATION, measurement_start=10, measurement_end=DURATION - 1,
                    scenario=f'scenarios/run{run}.xml' if number <= 2 else f'scenarios/{case}.xml'))
    return result


def path_conditions(run):
    """Identical independent path draws in experiments 1 and 2, and across CCs."""
    records = []
    for path in range(2):
        rng = random.Random(100000 * run + path)
        for time in range(0, DURATION, 15):
            bandwidth = rng.randint(50, 100)
            rtt = rng.randint(1, 100)
            loss = round(rng.uniform(0, .01), 4)
            outage = rng.randint(45, 120) / 1000 if time else 0.
            records.append(dict(path=path, time=time, up_time=round(time + outage, 6),
                bandwidth_mbps=bandwidth, rtt_ms=rtt, per=loss, outage_seconds=outage))
    return sorted(records, key=lambda row: (row['time'], row['path']))


def set_channel(event, module, gate, parameter, value):
    ET.SubElement(event, 'set-channel-param', **{'src-module': module,
        'src-gate': f'pppg$o[{gate}]', 'par': parameter, 'value': str(value)})


def access_delays(event, connection, path, delay):
    for module, gate in ((f'client[{connection}]', path), (f'left[{path}]', connection + 1),
                         (f'server[{connection}]', path), (f'right[{path}]', connection + 1)):
        set_channel(event, module, gate, 'delay', f'{delay:.12g}ms')


def write_scenario(path, scenario):
    scenario[:] = sorted(scenario, key=lambda event: float(event.attrib['t'].removesuffix('s')))
    ET.indent(scenario)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(scenario).write(path, encoding='unicode')


def responsiveness_scenario(number, run, root):
    scenario = ET.Element('scenario')
    records = path_conditions(run)
    for record in records:
        path, time = record['path'], record['time']
        bandwidth = f'{record["bandwidth_mbps"]}Mbps'
        delay = f'{record["rtt_ms"] / 6:.12g}ms'
        per = record['per'] if number == 2 else 0
        if time:
            down = ET.SubElement(scenario, 'at', t=f'{time}s')
            for router in ('left', 'right'):
                ET.SubElement(down, 'disconnect', **{'src-module': f'{router}[{path}]', 'src-gate': 'pppg$o[0]'})
                ET.SubElement(down, 'crash', module=f'{router}[{path}].ppp[0]')
        up = ET.SubElement(scenario, 'at', t=f'{record["up_time"]:g}s')
        if time:
            for source, destination in (('left', 'right'), ('right', 'left')):
                connect = ET.SubElement(up, 'connect', **{'src-module': f'{source}[{path}]',
                    'src-gate': 'pppg$o[0]', 'dest-module': f'{destination}[{path}]',
                    'dest-gate': 'pppg$i[0]', 'channel-type': 'ned.DatarateChannel'})
                for name, value in (('datarate', bandwidth), ('delay', delay), ('per', str(per if source == 'left' else 0))):
                    ET.SubElement(connect, 'param', name=name, value=value)
        else:
            for router in ('left', 'right'):
                set_channel(up, f'{router}[{path}]', 0, 'datarate', bandwidth)
                set_channel(up, f'{router}[{path}]', 0, 'delay', delay)
                set_channel(up, f'{router}[{path}]', 0, 'per', per if router == 'left' else 0)
        access_delays(up, 0, path, record['rtt_ms'] / 6)
        if time:
            for router in ('left', 'right'):
                ET.SubElement(up, 'start', module=f'{router}[{path}].ppp[0]')
            ET.SubElement(up, 'update', module='configurator')
    write_scenario(root / 'scenarios' / f'run{run}.xml', scenario)
    actual = [dict(row, per=row['per'] if number == 2 else 0) for row in records]
    (root / 'scenarios' / f'run{run}.json').write_text(json.dumps(actual, indent=2) + '\n')


def rtt_scenario(config, root):
    scenario = ET.Element('scenario')
    event = ET.SubElement(scenario, 'at', t='0s')
    for connection, path_rtts in enumerate(config['path_rtts_ms']):
        for path, rtt in enumerate(path_rtts):
            # Core contributes 1 ms RTT. Four access directions supply the rest.
            access_delays(event, connection, path, (rtt - 1) / 4)
    write_scenario(root / config['scenario'], scenario)


def general(number):
    return [
        '# Generated by pythonScripts/experiments.py', '[General]',
        'network = mporbexperiments.simulations.networks.twoPathDumbbell',
        f'*.numberOfConnections = {1 if number <= 2 else 2}',
        f'sim-time-limit = {DURATION}s', 'record-eventlog = false',
        'cmdenv-express-mode = true', 'cmdenv-event-banners = false', '**.cmdenv-log-level = off',
        '*.configurator.config = xml("<config><interface hosts=\'**\' address=\'10.x.x.x\' netmask=\'255.x.x.x\'/><autoroute metric=\'hopCount\'/></config>")',
        '*.configurator.addDefaultRoutes = false', '*.configurator.addSubnetRoutes = false',
        '*.configurator.optimizeRoutes = false', '*.client[*].forwarding = false', '*.server[*].forwarding = false',
        '**.tcp.subflowInterfaceBinding = true', '**.numberOfSubflows = 2',
        '**.schedulerMode = "defaultCwnd"', '**.startAllSubflowsAtBeginning = true', '**.subflowStartTimes = ""',
        '**.tcp.advertisedWindow = 200000000', '**.tcp.windowScalingSupport = true',
        '**.tcp.windowScalingFactor = -1', '**.tcp.increasedIWEnabled = true',
        '**.tcp.delayedAcksEnabled = false', '**.tcp.timestampSupport = true', '**.tcp.ecnWillingness = false',
        '**.tcp.nagleEnabled = true', '**.tcp.sackSupport = true', '**.tcp.updatedSackEnabled = true',
        '**.tcp.stopOperationTimeout = 4000s', f'**.tcp.mss = {MSS}', '**.tcp.sendQueueLimit = 4MiB',
        f'**.tcp.initialSsthresh = {400 * MSS}',
        '**.additiveIncreasePercent = 0.05', '**.eta = 0.95', '**.alpha = 0.03',
        '**.pintBits = 0', '**.pintFlowCountBits = 0', '**.flowCountSketchEnabled = false',
        '**.pintFeedbackProbability = 1', '**.pintSeparateQueueingDelay = true', '**.fixedAvgRTTVal = 0s',
        '**.goodputInterval = 100ms', '**.throughputInterval = 100ms',
        '*.client[*].numApps = 1', '*.server[*].numApps = 1',
        '*.client[*].app[0].typename = "MpTcpSessionApp"', '*.server[*].app[0].typename = "MpTcpSinkApp"',
        '*.client[*].app[0].localPort = 4000', '*.client[*].app[0].connectPort = 1000',
        '*.server[*].app[0].localPort = 1000', '*.client[*].app[0].sendBytes = 1MiB',
        '*.client[*].app[0].dataTransferMode = "bytecount"', f'*.client[*].app[0].tClose = {DURATION + 1}s',
        '*.server[*].app[0].serverThreadModuleType = "tcpgoodputapplications.applications.tcpapp.TcpGoodputSinkAppThread"',
    ]


def recording(number):
    # First matching rule wins. Preserve original connection/thread IDs and
    # periodic rate samples; other vectors can suppress repeated values.
    lines = ['**.tcp.conn-temp.**.statistic-recording = false']
    for metric in recorded_metrics(number):
        mode = 'vector' if metric in ('goodput', 'throughput') else 'vector(removeRepeats)'
        for scope in metric_scopes(metric):
            lines += [f'{scope}.{metric}.statistic-recording = true',
                      f'{scope}.{metric}.result-recording-modes = {mode}',
                      f'{scope}.{metric}:{mode}.vector-recording = true']
    return lines + ['*.server[*].app[0].bytesRcvd.scalar-recording = true',
                   '**.statistic-recording = false', '**.vector-recording = false',
                   '**.scalar-recording = false', '**.bin-recording = false']


def generate(number):
    configs = configurations(number)
    root = directory(number)
    root.mkdir(parents=True, exist_ok=True)
    if number <= 2:
        for run in run_numbers(number):
            responsiveness_scenario(number, run, root)
    else:
        for config in configs[::len(run_numbers(number))]:
            rtt_scenario(config, root)
    # One fixed average BDP for responsiveness, and 100 Mbps/50 ms for RTT tests.
    queue_packets = math.ceil((75e6 * .0505 if number <= 2 else 100e6 * .05) / (8 * MSS))
    for protocol in PROTOCOLS:
        tcp, algorithm, _, _ = PROTOCOLS[protocol]
        queue_type = 'MpOrbDeltaQueue' if protocol in SHUFFLE else 'PintQueue'
        lines = general(number) + [f'**.tcp.typename = "{tcp}"', f'**.tcp.tcpAlgorithmClass = "{algorithm}"']
        for path in range(2):
            queue = f'*.left[{path}].ppp[0].queue'
            lines += [f'{queue}.typename = "{queue_type}"', f'{queue}.packetCapacity = {queue_packets}']
            if protocol in SHUFFLE:
                lines += [f'{queue}.deltaShuffleFraction = 0.05', f'{queue}.deltaShuffleInterval = 0s']
        lines += ['**.ppp[*].queue.typename = "DropTailQueue"', '**.ppp[*].queue.packetCapacity = 1000']
        lines += recording(number)
        for config in (c for c in configs if c['protocol'] == protocol):
            name = config['config']
            lines += ['', f'[Config {name}]', f'seed-set = {config["seed"]}',
                f'output-vector-file = "results/{name}-#0.vec"', f'output-scalar-file = "results/{name}-#0.sca"',
                f'*.scenarioManager.script = xmldoc("{config["scenario"]}")']
            for connection, start in enumerate(config['starts']):
                lines += [f'*.client[{connection}].app[0].connectAddress = "server[{connection}]>right[0]"',
                    f'*.client[{connection}].tcp.subflowRemoteAddresses = "server[{connection}]>right[0] server[{connection}]>right[1]"',
                    f'*.client[{connection}].app[0].tOpen = {start}s',
                    f'*.client[{connection}].app[0].tSend = {start}s']
        (root / f'experiment{number}_{protocol.lower()}.ini').write_text('\n'.join(lines) + '\n')
    (root / 'matrix.json').write_text(json.dumps(configs, indent=2) + '\n')
    print(f'Experiment {number}: generated {len(configs)} runs')
    return configs


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiments', nargs='+', type=int, choices=range(1, 5), default=[1, 2, 3, 4])
    for number in parser.parse_args().experiments:
        generate(number)
