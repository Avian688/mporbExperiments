#!/usr/bin/env python3
"""1: unloaded ping; 2: five-flow traffic; 3: extract; 4: heatmaps. No build."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import leo
from runExperiments import logged, parallel, tool, STOP, ACTIVE, LOCK, terminate


def libraries(config=None):
    projects = leo.PROJECTS[:3] if config and config['protocol']=='Ping' else leo.PROJECTS
    return [leo.SAMPLES_DIR / project / 'src' / ('INET' if project == 'inet4.5' else project)
            for project in projects]


def command(config):
    ned = [leo.SIM_DIR, leo.SAMPLES_DIR / 'leosatellites/simulations',
           leo.SAMPLES_DIR / 'os3/simulations']
    ned += [leo.SAMPLES_DIR / p / 'src' for p in leo.PROJECTS]
    return [tool('opp_run'), '-u', 'Cmdenv', '-r', '0', '-m', '-f', leo.ini_file(config).name,
            '-c', config['config'], '-n', ':'.join(map(str,ned)),
            f'--image-path={leo.SAMPLES_DIR / "inet4.5/images"}',
            *[part for lib in libraries(config) for part in ('-l', str(lib))]]


def completion(config):
    path = leo.result(config, '.complete.json')
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    if data.get('config') != config:
        return None
    for suffix in ('.vec','.sca'):
        p = leo.result(config,suffix)
        if not p.is_file() or data.get('outputs',{}).get(suffix) != leo.stat(p):
            return None
    return data


def fingerprint(config, route_hash):
    digest = hashlib.sha256(json.dumps(config,sort_keys=True).encode())
    digest.update(route_hash.encode())
    for path in [leo.ini_file(config), leo.EXPERIMENT_DIR / 'topology.ini']:
        digest.update(path.read_bytes())
    for lib in libraries(config):
        path = next((p for ext in ('.dylib','.so')
                     if (p := lib.parent / ('lib'+lib.name+ext)).is_file()),None)
        if path is None:
            raise FileNotFoundError(f'Missing built library: {lib}. Build dependencies before simulation.')
        digest.update(json.dumps([str(path),leo.stat(path)]).encode())
    return digest.hexdigest()


def simulate(job):
    config,args,route_hash = job
    expected = fingerprint(config,route_hash)
    previous = completion(config)
    if not args.rerun and previous and previous['fingerprint'] == expected:
        return config['config']
    leo.result(config,'.vec').parent.mkdir(parents=True,exist_ok=True)
    for attempt in range(args.retries+1):
        if STOP.is_set():
            raise RuntimeError('Cancelled')
        # Fixed output names: retries and reruns replace this configuration only.
        for suffix in ('.vec','.vci','.sca','.complete.json'):
            leo.result(config,suffix).unlink(missing_ok=True)
        log = leo.SIM_DIR / 'logs' / leo.directory(config).name / f'{config["config"]}.attempt{attempt+1}.log'
        if logged(command(config),leo.directory(config),log,args.sim_timeout_seconds):
            if all(leo.result(config,s).is_file() and leo.result(config,s).stat().st_size for s in ('.vec','.sca')):
                leo.result(config,'.complete.json').write_text(json.dumps(dict(config=config,
                    fingerprint=expected,outputs={s:leo.stat(leo.result(config,s)) for s in ('.vec','.sca')}),indent=2)+'\n')
                return config['config']
    raise RuntimeError(f'{config["config"]} failed; inspect {log}')


def ensure_index(config):
    from vectors import Vectors
    if completion(config) is None:
        raise RuntimeError(f'{config["config"]}: no successful matching simulation')
    try:
        Vectors(leo.result(config,'.vec'))
    except (FileNotFoundError,ValueError):
        log = leo.SIM_DIR / 'logs' / leo.directory(config).name / f'{config["config"]}.index.log'
        if not logged([tool('opp_scavetool'),'index',str(leo.result(config,'.vec'))],
                      leo.directory(config),log,1800):
            raise RuntimeError(f'Indexing failed: {log}')
        Vectors(leo.result(config,'.vec'))


def prepare_ping(duration,route_hash=None):
    import analyzeExperimentLeo as analysis
    config = leo.ping_config(duration)
    ensure_index(config)
    if route_hash is not None and completion(config)['fingerprint'] != fingerprint(config,route_hash):
        raise ValueError('Ping baseline uses different routes or inputs; run step 1 again')
    try:
        return analysis.checked_summary(config)
    except (FileNotFoundError,ValueError,KeyError):
        return analysis.extract_ping(config)


def extract(config):
    import analyzeExperimentLeo as analysis
    ensure_index(config)
    analysis.extract(config)
    return config['config']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cores',type=int,default=int(os.environ.get('EXPERIMENT_CORES','2')))
    parser.add_argument('--runs',type=int,choices=range(1,6),default=5,help='Use the first N start-time patterns; simulation seed stays fixed')
    parser.add_argument('--ks',nargs='+',type=int,choices=leo.KS,default=list(leo.KS))
    parser.add_argument('--protocols',nargs='+',choices=tuple(leo.PROTOCOLS),default=list(leo.PROTOCOLS))
    parser.add_argument('--sim-time',type=float,help='Default 300s, or the existing manifest duration with --skip-generate')
    parser.add_argument('--route-store',type=Path,help='Default: leosatellites/simulations/leoSaves')
    parser.add_argument('--start-step',type=int,choices=range(1,5),default=1)
    parser.add_argument('--end-step',type=int,choices=range(1,5),default=4)
    parser.add_argument('--retries',type=int,default=1)
    parser.add_argument('--sim-timeout-seconds',type=float,default=12*3600,
                        help='Wall-clock limit per simulation attempt, including the ping prepass (default: 12 hours)')
    parser.add_argument('--rerun',action='store_true',help='Replace selected outputs; normally matching completed simulations are reused')
    parser.add_argument('--skip-generate',action='store_true')
    parser.add_argument('--check-routes',action='store_true',help='Validate saved snapshot headers and time coverage; never simulate')
    parser.add_argument('--dry-run',action='store_true',help='List the ordered work without launching simulations')
    parser.add_argument('--ping-only',action='store_true')
    args = parser.parse_args(argv)
    if args.cores<1 or args.retries<0 or args.start_step>args.end_step or not math.isfinite(args.sim_timeout_seconds) or args.sim_timeout_seconds<=0:
        parser.error('Invalid worker count, retry count, steps or timeout')
    manifest = json.loads((leo.EXPERIMENT_DIR/'matrix.json').read_text()) if args.skip_generate else {}
    args.sim_time = args.sim_time if args.sim_time is not None else manifest.get('duration',leo.DURATION)
    args.route_store = args.route_store or Path(manifest.get('route_store',leo.DEFAULT_STORE))
    matrix = manifest['configurations'] if args.skip_generate else leo.generate(args.sim_time,args.route_store)
    selected = [c for c in matrix if c['protocol'] in args.protocols and c['run']<=args.runs and
                (c['protocol']=='OrbSingle' or c['k'] in args.ks)]
    if not selected:
        parser.error('No traffic configurations selected')
    if any(c['sim_time']!=args.sim_time for c in selected):
        parser.error('--sim-time differs from the existing manifest; regenerate inputs')
    if args.check_routes:
        leo.check_routes(args.route_store,args.sim_time)
        print('Primary and EdgeDisjoint snapshot headers/time coverage match; payload validation is performed by the simulator.')
        return
    if args.dry_run:
        print('Step 1: one unloaded ping prepass (ordinary shortest path + all 10 saved edge-disjoint ranks for each pair)')
        print('Main runs contain TCP applications only. Fixed simulation seed; run differences are start times.')
        if not args.ping_only:
            print(f'Step 2: {len(selected)} traffic runs, five simultaneous connections each')
            for c in selected:
                print(c['config'])
            print('Step 3: extract directly from vectors; step 4: goodput/RTT heatmaps for each K')
        return
    route_hash = None
    if args.start_step<=2:
        route_hash = leo.check_routes(args.route_store,args.sim_time)
    if args.start_step<=1<=args.end_step:
        print('ExperimentLeoPing: simulate unloaded baseline',flush=True)
        simulate((leo.ping_config(args.sim_time),args,route_hash))
        prepare_ping(args.sim_time,route_hash)
    if args.ping_only:
        return
    if args.start_step<=2<=args.end_step:
        # Fail before the expensive traffic runs if the prepass is absent/stale.
        prepare_ping(args.sim_time,route_hash)
        print(f'ExperimentLeo: simulate {len(selected)} five-flow runs',flush=True)
        parallel(simulate,[(c,args,route_hash) for c in selected],args.cores)
    if args.start_step<=3<=args.end_step:
        prepare_ping(args.sim_time)
        print('ExperimentLeo: extract endpoint results without bulk CSV copies',flush=True)
        parallel(extract,selected,args.cores)
    if args.start_step<=4<=args.end_step:
        from analyzeExperimentLeo import plot
        print('ExperimentLeo: plot one goodput and one RTT heatmap per K',flush=True)
        plot(selected)


def run(argv=None):
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,interrupted)
    try:
        main(argv)
    except KeyboardInterrupt:
        STOP.set()
        with LOCK:
            for process in list(ACTIVE):
                terminate(process)
        raise SystemExit(130)
    except (OSError,ValueError,RuntimeError) as error:
        print(error,file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':
    run()
