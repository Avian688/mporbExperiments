#!/usr/bin/env python3
"""Display saved ExperimentLeo paths without running transport experiments."""
import argparse
import json
import math
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import leo
from runExperiments import tool

VIEW_DIR = leo.SIM_DIR/'experiments/experimentLeoView'
VIEW_INI = VIEW_DIR/'visualize.ini'
CONFIG = 'ExperimentLeoView'


def resolve_pairs(values):
    tokens = [part for value in values for part in value.split(',')]
    if [token.lower() for token in tokens]==['all']:
        return list(leo.PAIRS)
    selected = []
    for token in tokens:
        normalized = ''.join(c for c in token.lower() if c.isalnum())
        match = next((pair for i,pair in enumerate(leo.PAIRS,1)
            if normalized in (str(i),''.join(c for c in pair['pair'].lower() if c.isalnum()))),None)
        if match is None:
            raise ValueError(f'Unknown pair {token!r}; use --list-pairs, names, numbers 1–5, or all')
        if match not in selected:
            selected.append(match)
    if not selected:
        raise ValueError('Select at least one terminal pair')
    return selected


def generate_ini(duration, path=None):
    # Use a fresh generator module with output paths redirected into this project.
    # Nothing in leosatellites, including its generated INIs, is rewritten.
    path = VIEW_INI if path is None else Path(path)
    shared = leo.shared_definitions()
    shared.EXPERIMENT_DIR = path.parent
    shared.VISUALIZATION_INI_FILE = path
    shared.generate_visualization_ini(shared.load_ground_stations(),duration)
    with path.open('a') as handle:
        handle.write('\n[Config '+CONFIG+']\nextends = View_EdgeDisjoint\n'
            'network = mporbexperiments.visualizer.ExperimentLeoOsg\n')


def command(args, pairs):
    routing,_ = leo.route_directories(args.route_store)
    ned = [leo.SIM_DIR,leo.PROJECT_DIR/'src',
           leo.SAMPLES_DIR/'leosatellites/simulations',leo.SAMPLES_DIR/'leosatellites/src',
           leo.SAMPLES_DIR/'os3/src',leo.SAMPLES_DIR/'inet4.5/src']
    time = args.time if args.time is not None else -1
    cmd = [tool('opp_run'),'-u','Qtenv','-r','0','-m','-f',VIEW_INI.name,'-c',CONFIG,
        '-n',':'.join(map(str,ned)),f'--image-path={leo.SAMPLES_DIR / "inet4.5/images"}',
        f'--sim-time-limit={args.sim_time:g}s',
        f'--*.pathVisualizer.routingDirectory={json.dumps(str(routing.resolve()))}',
        f'--*.pathVisualizer.earthTexture={json.dumps(str(args.earth_texture.resolve()))}',
        f'--*.pathVisualizer.pathCount={leo.SHARED.PATH_COUNT}',
        f'--*.pathVisualizer.displayedK={args.k}',
        '--*.pathVisualizer.selectedPairIndices='+json.dumps(' '.join(str(p['group']) for p in pairs)),
        f'--*.pathVisualizer.snapshotTime={time}s',
        f'--*.pathVisualizer.highlightAccessLinks={str(not args.core_only_overlap).lower()}',
        f'--*.pathVisualizer.showInterSatelliteLinks={str(args.show_isls).lower()}',
        '-l',str(leo.REPO_DIR/'lib/oppqtenv-osg')]
    libraries = [leo.SAMPLES_DIR/'inet4.5/src/INET',leo.SAMPLES_DIR/'os3/src/os3',
                 leo.SAMPLES_DIR/'leosatellites/src/leosatellites',leo.PROJECT_DIR/'src/mporbExperiments']
    for library in libraries:
        cmd += ['-l',str(library)]
    return cmd


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pairs',nargs='+',default=['all'],help='ExperimentLeo names or numbers 1–5; default all')
    parser.add_argument('--list-pairs',action='store_true')
    parser.add_argument('--k',type=int,choices=range(1,leo.SHARED.PATH_COUNT+1),default=5)
    timing = parser.add_mutually_exclusive_group()
    timing.add_argument('--time',type=float,help='Optional: freeze at this saved time in seconds')
    timing.add_argument('--animate',action='store_true',help='Replay the full duration using Qtenv Run/Pause (default)')
    parser.add_argument('--sim-time',type=float,help='Replay duration; defaults to the experiment manifest or 300s')
    parser.add_argument('--route-store',type=Path)
    parser.add_argument('--earth-texture',type=Path,default=leo.SAMPLES_DIR/'osg-satellites/earth.jpg')
    parser.add_argument('--show-isls',action='store_true',help='Show the background ISL mesh')
    parser.add_argument('--core-only-overlap',action='store_true',help='Exclude shared terminal access from overlap highlighting')
    parser.add_argument('--skip-generate',action='store_true',help='Reuse this project\'s existing viewer INI')
    parser.add_argument('--dry-run',action='store_true',help='Print the launch command without writing files or opening Qtenv')
    args = parser.parse_args(argv)
    if args.list_pairs:
        for i,pair in enumerate(leo.PAIRS,1):
            print(f'{i}: {pair["pair"]} (UT{pair["source"]} -> UT{pair["destination"]})')
        return 0
    manifest_path = leo.EXPERIMENT_DIR/'matrix.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    args.sim_time = args.sim_time if args.sim_time is not None else manifest.get('duration',leo.DURATION)
    args.route_store = args.route_store or Path(manifest.get('route_store',leo.DEFAULT_STORE))
    if (not math.isfinite(args.sim_time) or args.sim_time<=0 or
            (args.time is not None and (not math.isfinite(args.time) or args.time<0))):
        parser.error('Duration must be positive and snapshot time must be finite and nonnegative')
    try:
        pairs = resolve_pairs(args.pairs)
    except ValueError as error:
        parser.error(str(error))
    cmd = command(args,pairs)
    mode = f'frozen at {args.time:g}s' if args.time is not None else f'animated replay, {args.sim_time:g}s'
    print(f'ExperimentLeo viewer: {mode}, K={args.k}, pairs='+', '.join(p['pair'] for p in pairs),flush=True)
    if args.dry_run:
        print('Working directory:',VIEW_DIR)
        print(shlex.join(cmd))
        return 0
    _,catalog = leo.route_directories(args.route_store)
    if not catalog.is_dir() or not any(catalog.glob('*.bin')):
        raise FileNotFoundError(f'Missing saved EdgeDisjoint catalog: {catalog}')
    library = leo.PROJECT_DIR/'src/libmporbExperiments'
    if not any(library.with_suffix(ext).is_file() for ext in ('.dylib','.so')):
        raise FileNotFoundError('Build the local viewer first: from mporbExperiments, run make makefiles && make MODE=release')
    if not args.earth_texture.is_file():
        raise FileNotFoundError(f'Missing Earth texture: {args.earth_texture}')
    if not args.skip_generate:
        generate_ini(args.sim_time)
    if not VIEW_INI.is_file():
        raise FileNotFoundError(f'Missing viewer INI: {VIEW_INI}; run without --skip-generate')
    return subprocess.call(cmd,cwd=VIEW_DIR)


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (OSError,ValueError) as error:
        print(error,file=sys.stderr)
        raise SystemExit(1)
