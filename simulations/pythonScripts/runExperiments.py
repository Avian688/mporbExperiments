"""Generate, simulate, export, extract and plot the four MpORB experiments."""
import argparse
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from experiments import SIM_DIR, SAMPLES_DIR, REPO_DIR, PROTOCOLS, METRICS, recorded_metrics, directory, generate

PROJECTS = ('inet4.5', 'tcpGoodputApplications', 'tcpPaced', 'cubic', 'orbtcp', 'mptcp', 'mporb')
ACTIVE = set()
LOCK = threading.Lock()
STOP = threading.Event()


def tool(name):
    return os.environ.get(name.upper(), str(REPO_DIR / 'bin' / name))


def libraries():
    return [SAMPLES_DIR / p / 'src' / ('INET' if p == 'inet4.5' else p) for p in PROJECTS]


def command(config):
    ned = [SIM_DIR]
    ned += [SAMPLES_DIR / p / 'src' for p in PROJECTS]
    return [tool('opp_run'), '-u', 'Cmdenv', '-r', '0', '-m',
        '-f', f'experiment{config["experiment"]}_{config["protocol"].lower()}.ini',
        '-c', config['config'], '-n', ':'.join(str(p) for p in ned),
        f'--image-path={SAMPLES_DIR / "inet4.5/images"}',
        f'--sim-time-limit={config["sim_time"]:g}s',
        *[part for library in libraries() for part in ('-l', str(library))]]


def result(config, suffix):
    return directory(config['experiment']) / 'results' / f'{config["config"]}-#0{suffix}'


def raw_path(config):
    return directory(config['experiment']) / 'csvs/raw' / f'{config["config"]}.csv'


def stat(path):
    info = path.stat()
    return [info.st_size, info.st_mtime_ns]


def fingerprint(config):
    root = directory(config['experiment'])
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode())
    for path in (root / f'experiment{config["experiment"]}_{config["protocol"].lower()}.ini',
                 SIM_DIR / 'networks/twoPathDumbbell.ned', root / config['scenario']):
        digest.update(path.read_bytes())
    conditions = (root / config['scenario']).with_suffix('.json')
    if conditions.is_file():
        digest.update(conditions.read_bytes())
    for library in libraries():
        path = next((Path(str(library.parent / ('lib' + library.name)) + ext)
                     for ext in ('.dylib', '.so') if Path(str(library.parent / ('lib' + library.name)) + ext).is_file()), None)
        if path is None:
            raise FileNotFoundError(f'Build the library {library} before running')
        digest.update(json.dumps([str(path), stat(path)]).encode())
    return digest.hexdigest()


def completion(config):
    path = result(config, '.complete.json')
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    if data.get('config') != config or set(data.get('outputs', {})) != {'.vec', '.sca'}:
        return None
    for suffix, saved in data['outputs'].items():
        if not result(config, suffix).is_file() or stat(result(config, suffix)) != saved:
            return None
    return data


def terminate(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        except ProcessLookupError:
            pass


def logged(command_line, cwd, log_path, timeout):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('w') as handle:
        handle.write(json.dumps(command_line) + '\n')
        handle.flush()
        with LOCK:
            if STOP.is_set():
                return False
            process = subprocess.Popen(command_line, cwd=cwd, stdout=handle,
                stderr=subprocess.STDOUT, start_new_session=True)
            ACTIVE.add(process)
        try:
            code = process.wait(timeout=timeout)
            handle.write(f'\nExit code: {code}\n')
            return code == 0
        except subprocess.TimeoutExpired:
            terminate(process)
            handle.write('\nTIMEOUT\n')
            return False
        except BaseException:
            terminate(process)
            raise
        finally:
            with LOCK:
                ACTIVE.discard(process)


def simulate(job):
    config, args = job
    expected = fingerprint(config)
    previous = completion(config)
    if not args.rerun and previous and previous.get('fingerprint') == expected:
        return config['config']
    result(config, '.vec').parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(args.retries + 1):
        if STOP.is_set():
            raise RuntimeError('Cancelled')
        for suffix in ('.vec', '.vci', '.sca', '.complete.json'):
            result(config, suffix).unlink(missing_ok=True)
        log = SIM_DIR / 'logs' / f'experiment{config["experiment"]}' / f'{config["config"]}.attempt{attempt+1}.log'
        if not logged(command(config), directory(config['experiment']), log, args.sim_timeout_seconds):
            continue
        if all(result(config, suffix).is_file() and result(config, suffix).stat().st_size > 0 for suffix in ('.vec', '.sca')):
            result(config, '.complete.json').write_text(json.dumps(dict(config=config, fingerprint=expected,
                outputs={suffix: stat(result(config, suffix)) for suffix in ('.vec', '.sca')}), indent=2) + '\n')
            return config['config']
    raise RuntimeError(f'{config["config"]} failed; inspect {log}')


def export(config):
    if completion(config) is None:
        raise RuntimeError(f'{config["config"]}: no successful result; run step 1')
    target = raw_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [tool('opp_scavetool'), 'export', '-F', 'CSV-R', '-o', str(target),
           str(result(config, '.vec')), str(result(config, '.sca'))]
    log = SIM_DIR / 'logs' / f'experiment{config["experiment"]}' / f'{config["config"]}.export.log'
    if not logged(cmd, directory(config['experiment']), log, 1800):
        raise RuntimeError(f'Export failed for {config["config"]}; inspect {log}')
    return config['config']


def parallel(function, jobs, cores, processes=False):
    pool_type = ProcessPoolExecutor if processes else ThreadPoolExecutor
    with pool_type(max_workers=min(cores, len(jobs))) as pool:
        try:
            futures = [pool.submit(function, job) for job in jobs]
            output = []
            for index, future in enumerate(as_completed(futures), 1):
                output.append(future.result())
                print(f'{function.__name__}: {index}/{len(jobs)}', flush=True)
            return output
        except BaseException:
            STOP.set()
            with LOCK:
                active = list(ACTIVE)
            for process in active:
                terminate(process)
            for future in futures:
                future.cancel()
            raise


def main(default_experiment=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiments', nargs='+', type=int, choices=range(1, 5), default=[default_experiment] if default_experiment else [1, 2, 3, 4])
    parser.add_argument('--protocols', nargs='+', choices=tuple(PROTOCOLS), default=list(PROTOCOLS))
    parser.add_argument('--runs', type=int, choices=range(1, 6), default=5, help='Run seeds 1..N (default five)')
    parser.add_argument('--configs', nargs='+', help='Exact configuration names')
    parser.add_argument('--rtts', nargs='+', type=int, help='Restrict RTT-sweep values in ms')
    parser.add_argument('--cores', type=int, default=int(os.environ.get('EXPERIMENT_CORES', '2')))
    parser.add_argument('--sim-time', type=float, default=300, help='Shorter horizons support smoke checks; default 300 seconds')
    parser.add_argument('--sim-timeout-seconds', type=float, default=5 * 3600)
    parser.add_argument('--retries', type=int, default=1)
    parser.add_argument('--start-step', type=int, choices=range(1, 5), default=1)
    parser.add_argument('--end-step', type=int, choices=range(1, 5), default=4)
    parser.add_argument('--skip-generate', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--rerun', action='store_true')
    parser.add_argument('--plots', nargs='+', choices=('summary', 'all', *METRICS), default=['summary'],
                        help='Summary comparisons or selected individual metric plots; all includes both')
    args = parser.parse_args()
    if (args.cores < 1 or args.retries < 0 or args.start_step > args.end_step or
        not math.isfinite(args.sim_time) or not 0 < args.sim_time <= 300 or
        not math.isfinite(args.sim_timeout_seconds) or args.sim_timeout_seconds <= 0):
        parser.error('Invalid worker count, steps, retries, duration or timeout')
    configs = []
    for number in args.experiments:
        matrix = generate(number) if args.start_step == 1 and not args.skip_generate else json.loads((directory(number) / 'matrix.json').read_text())
        configs += [c for c in matrix if c['protocol'] in args.protocols and c['run'] <= args.runs
                    and (not args.rtts or c['rtt_ms'] is None or c['rtt_ms'] in args.rtts)]
    if args.configs:
        unknown = set(args.configs) - {c['config'] for c in configs}
        if unknown:
            parser.error(f'Unknown selected configurations: {sorted(unknown)}')
        configs = [c for c in configs if c['config'] in args.configs]
    if not configs:
        parser.error('No configurations selected')
    for config in configs:
        config['sim_time'] = args.sim_time
        config['measurement_end'] = args.sim_time - 1
    print(f'Selected {len(configs)} simulations')
    if args.dry_run:
        for config in configs:
            print(f'Experiment {config["experiment"]}: {config["config"]}')
        return
    if args.end_step >= 3:
        if args.sim_time <= 11:
            parser.error('Extraction needs sim-time > 11s; use --end-step 1 for shorter smoke checks')
        for package in ('numpy', 'matplotlib') if args.end_step >= 4 else ('numpy',):
            if importlib.util.find_spec(package) is None:
                parser.error(f'Missing {package}; activate the workspace Python environment')
    if args.start_step <= 1 <= args.end_step:
        parallel(simulate, [(c, args) for c in configs], args.cores)
    if args.start_step <= 2 <= args.end_step:
        parallel(export, configs, args.cores)
    if args.start_step <= 3 <= args.end_step:
        for config in configs:
            previous = completion(config)
            raw = raw_path(config)
            if previous is None or not raw.is_file() or raw.stat().st_mtime_ns < max(s[1] for s in previous['outputs'].values()):
                raise RuntimeError(f'{config["config"]}: missing or stale export; run step 2')
        from analyze import extract, write_summaries
        from extractionResources import extraction_workers
        summaries = parallel(extract, configs, extraction_workers(args.cores, len(configs)), processes=True)
        write_summaries(summaries)
    if args.start_step <= 4 <= args.end_step:
        from analyze import plot
        from plotMetrics import plot_metrics
        if 'summary' in args.plots or 'all' in args.plots:
            plot(configs)
        for number in args.experiments:
            selected = [c for c in configs if c['experiment'] == number]
            if selected:
                metrics = list(recorded_metrics(number)) if 'all' in args.plots else [m for m in args.plots if m != 'summary']
                if metrics:
                    plot_metrics(selected, metrics)


def run(default_experiment=None):
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main(default_experiment)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    run()
