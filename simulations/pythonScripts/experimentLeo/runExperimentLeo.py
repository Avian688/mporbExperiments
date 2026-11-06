#!/usr/bin/env python3
"""1: unloaded ping; 2: five-flow traffic; 3: extract; 4: heatmaps/run plots. No build."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import leo
from runExperiments import logged, parallel, tool, STOP, ACTIVE, LOCK, terminate


def libraries(config=None):
    projects = leo.PROJECTS[:3] if config and config['protocol'] in ('Ping','Capture') else leo.PROJECTS
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
    for path in [leo.ini_file(config), leo.topology_file(config)]:
        digest.update(path.read_bytes())
    if config.get('replay_start') and config['protocol'] != 'Capture':
        digest.update(leo.checkpoint_file(config).read_bytes())
    for lib in libraries(config):
        path = next((p for ext in ('.dylib','.so')
                     if (p := lib.parent / ('lib'+lib.name+ext)).is_file()),None)
        if path is None:
            raise FileNotFoundError(f'Missing built library: {lib}. Build dependencies before simulation.')
        digest.update(json.dumps([str(path),leo.stat(path)]).encode())
    return digest.hexdigest()


def prepare_replay(args, replay_start):
    config = dict(config=leo.CAPTURE_CONFIG, protocol='Capture', replay_start=replay_start)
    path = leo.checkpoint_file(config)
    receipt = path.with_suffix('.json')
    route_hash = leo.check_routes(args.route_store, leo.capture_end(replay_start), primary_only=True)
    expected = fingerprint(config, route_hash)
    try:
        saved = json.loads(receipt.read_text())
        if (saved['fingerprint'] == expected and
                saved['checkpoint_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()):
            print(f'Reuse replay checkpoint: {path}', flush=True)
            return
    except (OSError, ValueError, KeyError):
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    root = leo.directory(config)
    print(f'Capture topology at original {replay_start:g}s (one unloaded run)', flush=True)
    log = leo.SIM_DIR / 'logs' / root.name / 'capture-replay.log'
    with tempfile.TemporaryDirectory(prefix='capture-', dir=path.parent) as tmp:
        candidate = Path(tmp) / path.name
        options = [*command(config), f'--*.channelConstructor.replayStateFile={json.dumps(str(candidate.resolve()))}']
        if not logged(options, root, log, args.sim_timeout_seconds) or not candidate.is_file() or not candidate.stat().st_size:
            raise RuntimeError(f'Replay capture failed; inspect {log}')
        staged_receipt = Path(tmp) / receipt.name
        staged_receipt.write_text(json.dumps(dict(fingerprint=expected,
            checkpoint_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest()), indent=2)+'\n')
        os.replace(candidate, path)
        os.replace(staged_receipt, receipt)
    print(f'Saved replay checkpoint: {path}', flush=True)


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
        return Vectors(leo.result(config,'.vec'))
    except (FileNotFoundError,ValueError):
        log = leo.SIM_DIR / 'logs' / leo.directory(config).name / f'{config["config"]}.index.log'
        if not logged([tool('opp_scavetool'),'index',str(leo.result(config,'.vec'))],
                      leo.directory(config),log,1800):
            raise RuntimeError(f'Indexing failed: {log}')
        return Vectors(leo.result(config,'.vec'))


def prepare_ping(duration,route_hash=None,*,replay_start=0):
    import analyzeExperimentLeo as analysis
    config = leo.ping_config(duration, replay_start)
    ensure_index(config)
    if route_hash is not None and completion(config)['fingerprint'] != fingerprint(config,route_hash):
        raise ValueError('Ping baseline uses different routes or inputs; run step 1 again')
    try:
        return analysis.checked_summary(config)
    except (FileNotFoundError,ValueError,KeyError):
        return analysis.extract_ping(config)


def extract(config):
    import analyzeExperimentLeo as analysis
    from terminalVectors import CsvVectors, export_vectors
    vectors = ensure_index(config)
    export_vectors(config,vectors)
    # Summaries use the full CSV samples, never the compact plotting series.
    analysis.extract(config,vectors=CsvVectors(leo.vector_dir(config),config))
    return config['config']


def main(argv=None, *, replay_start=0, default_duration=leo.DURATION):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cores',type=int,default=int(os.environ.get('EXPERIMENT_CORES','2')))
    parser.add_argument('--runs',type=int,choices=range(1,6),default=5,help='Use the first N start-time patterns; simulation seed stays fixed')
    parser.add_argument('--ks',nargs='+',type=int,choices=leo.KS,default=list(leo.KS))
    parser.add_argument('--protocols',nargs='+',choices=tuple(leo.PROTOCOLS),default=list(leo.PROTOCOLS))
    parser.add_argument('--configs',nargs='+',help='Restrict to exact configuration names')
    parser.add_argument('--plots',nargs='+',choices=('summary','all',*leo.USER_METRICS),default=['all'],
                        help='Default all: heatmaps and individual endpoint plots; summary: heatmaps only')
    parser.add_argument('--sim-time',type=float,
                        help=f'Default {default_duration:g}s, or the existing manifest duration with --skip-generate')
    parser.add_argument('--route-store',type=Path,help='Default: leosatellites/simulations/leoSaves')
    parser.add_argument('--start-step',type=int,choices=range(1,5),default=1)
    parser.add_argument('--end-step',type=int,choices=range(1,5),default=4)
    parser.add_argument('--retries',type=int,default=1)
    parser.add_argument('--sim-timeout-seconds',type=float,default=12*3600,
                        help='Wall-clock limit per simulation attempt, including the ping prepass (default: 12 hours)')
    parser.add_argument('--rerun',action='store_true',help='Replace selected outputs; normally matching completed simulations are reused')
    parser.add_argument('--skip-generate',action='store_true')
    parser.add_argument('--skip-link-sharing',action='store_true',
                        help='Skip saved-route link-sharing reports during extraction')
    parser.add_argument('--check-routes',action='store_true',help='Validate saved snapshot headers and time coverage; never simulate')
    parser.add_argument('--dry-run',action='store_true',help='List the ordered work without launching simulations')
    parser.add_argument('--ping-only',action='store_true')
    if replay_start:
        parser.add_argument('--capture-only',action='store_true',
                            help='Prepare the topology checkpoint and exit before ping and traffic')
    args = parser.parse_args(argv)
    if args.cores<1 or args.retries<0 or args.start_step>args.end_step or not math.isfinite(args.sim_timeout_seconds) or args.sim_timeout_seconds<=0:
        parser.error('Invalid worker count, retry count, steps or timeout')
    manifest_path = leo.experiment_directory(replay_start)/'matrix.json'
    manifest = json.loads(manifest_path.read_text()) if args.skip_generate else {}
    if args.skip_generate and manifest.get('replay_start',0) != replay_start:
        parser.error('Existing manifest uses a different replay start')
    args.sim_time = args.sim_time if args.sim_time is not None else manifest.get('duration',default_duration)
    if replay_start and args.skip_generate and args.route_store is not None and (
            args.route_store.resolve() != Path(manifest['route_store']).resolve()):
        parser.error('--route-store differs from the generated INIs; regenerate inputs')
    args.route_store = args.route_store or Path(manifest.get('route_store',leo.DEFAULT_STORE))
    capture_only = getattr(args,'capture_only',False)
    matrix = (manifest['configurations'] if args.skip_generate else
              leo.configurations(args.sim_time,replay_start) if replay_start and args.dry_run else
              leo.generate(args.sim_time,args.route_store,replay_start))
    selected = [c for c in matrix if c['protocol'] in args.protocols and c['run']<=args.runs and
                (c['protocol']=='OrbSingle' or c['k'] in args.ks)]
    if args.configs:
        unknown = set(args.configs)-{c['config'] for c in selected}
        if unknown:
            parser.error(f'Unknown selected configurations: {sorted(unknown)}')
        selected = [c for c in selected if c['config'] in args.configs]
    if not selected:
        parser.error('No traffic configurations selected')
    if any(c['sim_time']!=args.sim_time or c.get('replay_start',0)!=replay_start for c in selected):
        parser.error('--sim-time differs from the existing manifest; regenerate inputs')
    if args.check_routes:
        if capture_only:
            leo.check_routes(args.route_store,leo.capture_end(replay_start),primary_only=True)
            print('Primary snapshot headers and time coverage match for checkpoint capture.')
        else:
            leo.check_routes(args.route_store,replay_start+args.sim_time)
            print('Primary and EdgeDisjoint snapshot headers/time coverage match; payload validation is performed by the simulator.')
        return
    if args.dry_run:
        if replay_start:
            print(f'Local 0–{args.sim_time:g}s replays original {replay_start:g}–{replay_start+args.sim_time:g}s')
            if capture_only or args.start_step<=2:
                print('Prepare or reuse one topology checkpoint')
        if capture_only:
            return
        if args.start_step<=1<=args.end_step:
            print('Step 1: one unloaded ping prepass (ordinary shortest path + all 10 saved edge-disjoint ranks for each pair)')
        if not args.ping_only:
            print(f'Selected {len(selected)} traffic runs, five simultaneous connections each')
            print('Main runs contain TCP applications only. Fixed simulation seed; run differences are start times.')
            for c in selected:
                print(c['config'])
            if args.start_step<=2<=args.end_step:
                print('Step 2: simulate selected traffic runs')
            if args.start_step<=3<=args.end_step:
                print('Step 3: extract terminal vector CSVs and summaries')
                if not args.skip_link_sharing:
                    print('Step 3: export saved-route link-sharing reports')
            if args.start_step<=4<=args.end_step:
                print('Step 4: plot '+', '.join(args.plots))
        return
    if capture_only:
        prepare_replay(args,replay_start)
        return
    route_hash = None
    if args.start_step<=2:
        route_hash = leo.check_routes(args.route_store,replay_start+args.sim_time)
        if replay_start:
            prepare_replay(args,replay_start)
    experiment = leo.experiment_directory(replay_start).name
    ping_config = leo.ping_config(args.sim_time,replay_start)
    ping_name = leo.directory(ping_config).name
    if args.start_step<=1<=args.end_step:
        print(f'{ping_name}: simulate unloaded baseline',flush=True)
        simulate((ping_config,args,route_hash))
        prepare_ping(args.sim_time,route_hash,replay_start=replay_start)
    if args.ping_only:
        return
    if args.start_step<=2<=args.end_step:
        # Fail before the expensive traffic runs if the prepass is absent/stale.
        prepare_ping(args.sim_time,route_hash,replay_start=replay_start)
        print(f'{experiment}: simulate {len(selected)} five-flow runs',flush=True)
        parallel(simulate,[(c,args,route_hash) for c in selected],args.cores)
    if args.start_step<=3<=args.end_step:
        print(f'{experiment}: check/extract unloaded ping baseline',flush=True)
        prepare_ping(args.sim_time,replay_start=replay_start)
        print(f'{experiment}: extract user-terminal vectors to CSVs '
              f'({min(args.cores,len(selected))} worker processes)',flush=True)
        # Sample parsing is CPU-bound Python; threads cannot use multiple cores.
        parallel(extract,selected,args.cores,processes=True)
        if not args.skip_link_sharing:
            from exportLinkSharing import export_reports
            export_reports(selected,args.route_store,args.cores)
    if args.start_step<=4<=args.end_step:
        if 'summary' in args.plots or 'all' in args.plots:
            from analyzeExperimentLeo import plot
            print(f'{experiment}: plot one goodput and one RTT heatmap per K',flush=True)
            plot(selected)
        metrics = list(leo.USER_METRICS) if 'all' in args.plots else [m for m in args.plots if m!='summary']
        if metrics:
            from plotExperimentLeo import plot_metrics
            print(f'{experiment}: plot individual user-terminal metrics from CSVs',flush=True)
            plot_metrics(selected,metrics,cores=args.cores)


def run(argv=None, **settings):
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,interrupted)
    try:
        main(argv, **settings)
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
