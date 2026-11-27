#!/usr/bin/env python3
"""Generate, run and plot the numbered MPOrb evaluation and mechanism tests."""

import argparse
import hashlib
import json
import math
import os
import signal

from evaluation import (
    DURATION, MAIN, MECHANISMS, NED, PROTOCOLS, SWEEPS, TRIALS,
    base_config, case_name, config_lines, directory, generate, scenario,
)
from runExperiments import PROJECTS, SAMPLES_DIR, SIM_DIR, libraries, logged, parallel, tool


def build_matrix(args, experiment):
    protocols = MECHANISMS.get(experiment, MAIN)
    matrix = []
    for value in SWEEPS[experiment]:
        if args.values and value not in args.values:
            continue
        for trial in range(1, args.trials + 1):
            spec = scenario(experiment, value, trial, args.duration)
            for controller in protocols:
                if args.protocols and controller not in args.protocols:
                    continue
                matrix.append(dict(spec, controller=controller, config=case_name(spec, controller),
                                   scenario=f'scenarios/V{value}_Run{trial}.xml'))
    return matrix


def command(config, binaries):
    ned = (SIM_DIR, SIM_DIR.parent / 'src', *(SAMPLES_DIR / p / 'src' for p in PROJECTS))
    return [tool('opp_run'), '-u', 'Cmdenv', '-r', '0', '-m', '-f', 'evaluation.ini',
            '-c', config['config'], '-n', os.pathsep.join(map(str, ned)),
            '-x', 'mporbexperiments.visualizer;mporbexperiments.workload',
            *[argument for library in binaries for argument in ('-l', str(library))]]


def library_inputs():
    binaries = (*libraries(), SIM_DIR.parent / 'src/mporbExperiments')
    signatures = {}
    for library in binaries:
        path = next((path for extension in ('.so', '.dylib')
                     if (path := library.with_name(f'lib{library.name}{extension}')).is_file()), None)
        if path is None:
            raise FileNotFoundError(f'Build the release library for {library}')
        stat = path.stat()
        signatures[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return binaries, signatures


def simulate(job):
    root, spec, binaries, signatures, timeout, rerun = job
    name = spec['config']
    output = root / 'results' / f'{name}.csv'
    staged = output.with_suffix('.staged.csv')
    marker = output.with_suffix('.complete.json')
    digest = hashlib.sha256(json.dumps([spec, signatures, base_config(),
                                        config_lines(spec, spec['controller'])], sort_keys=True).encode())
    for path in (NED, root / spec['scenario']):
        digest.update(path.read_bytes())
    fingerprint = digest.hexdigest()
    if not rerun and output.is_file() and marker.is_file():
        previous = json.loads(marker.read_text(encoding='utf-8'))
        if previous == {'fingerprint': fingerprint, 'bytes': output.stat().st_size}:
            return name
    staged.unlink(missing_ok=True)
    log = SIM_DIR / 'logs' / 'evaluation' / root.parent.name / root.name / f'{name}.log'
    if not logged(command(spec, binaries), root, log, timeout) or not staged.is_file():
        raise RuntimeError(f'{name} failed; inspect {log}')
    for extension in ('.vec', '.vci', '.sca'):
        staged.with_suffix(extension).unlink(missing_ok=True)
    staged.replace(output)
    temporary = marker.with_suffix('.tmp')
    temporary.write_text(json.dumps({'fingerprint': fingerprint, 'bytes': output.stat().st_size}) + '\n')
    temporary.replace(marker)
    return name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiments', nargs='+', choices=tuple(SWEEPS), default=tuple(SWEEPS))
    parser.add_argument('--protocols', nargs='+', choices=tuple(PROTOCOLS))
    parser.add_argument('--values', nargs='+', type=int, help='Limit the sweep to these values')
    parser.add_argument('--trials', type=int, default=TRIALS)
    parser.add_argument('--cores', type=int, default=1)
    parser.add_argument('--duration', type=float, default=DURATION)
    parser.add_argument('--timeout', type=float, default=7200, help='Wall-clock seconds per simulation')
    parser.add_argument('--step', choices=('all', 'generate', 'simulate', 'plot'), default='all')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--rerun', action='store_true')
    args = parser.parse_args()
    if args.trials < 1 or args.cores < 1 or not math.isfinite(args.duration) or args.duration <= 2:
        parser.error('Use positive trials and cores and a duration above 2 s')
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('Use a finite positive timeout')
    if args.duration < 211 and '6' in args.experiments and args.step in ('all', 'plot') and not args.dry_run:
        parser.error('Use at least 211 s to plot arrivals and departures')
    if args.duration < 65 and 'A' in args.experiments and args.step in ('all', 'plot') and not args.dry_run:
        parser.error('Use at least 65 s to plot subflow recovery')
    matrices = {experiment: build_matrix(args, experiment) for experiment in dict.fromkeys(args.experiments)}
    if not any(matrices.values()):
        parser.error('No matching experiment configurations')
    print(f'{sum(map(len, matrices.values()))} evaluation runs selected', flush=True)
    if args.step in ('all', 'simulate') and not args.dry_run:
        binaries, signatures = library_inputs()
    for experiment, matrix in matrices.items():
        if not matrix:
            continue
        root = directory(experiment) / f'{args.duration:g}s'
        print(f'Experiment {experiment}: {len(matrix)} runs, {root}', flush=True)
        if args.dry_run:
            for spec in matrix:
                print(spec['config'])
            continue
        if args.step != 'plot':
            generate(root, matrix)
        if args.step in ('all', 'simulate'):
            parallel(simulate, [(root, spec, binaries, signatures, args.timeout, args.rerun) for spec in matrix],
                     args.cores)
        if args.step in ('all', 'plot'):
            from plotEvaluation import plot
            plot(root, matrix)


def interrupted(*_):
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('Evaluation cancelled')
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error))
