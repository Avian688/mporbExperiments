import configparser
from contextlib import ExitStack, redirect_stdout
import csv
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

SCRIPTS = Path(__file__).resolve().parents[1] / 'simulations/pythonScripts'
sys.path.insert(0, str(SCRIPTS))
import experiments as e
import analyze as a
import extractionResources as resources
import plotAggregates as aggregates
import plotMetrics as metrics
import runExperiments as runner
from plotMetrics import read_series


class ExperimentsTests(unittest.TestCase):
    def test_matrix_has_only_requested_ccs_and_two_subflows(self):
        self.assertEqual(tuple(e.PROTOCOLS), ('Delta', 'Omega', 'Alpha', 'OrbUncoupled', 'CubicUncoupled'))
        self.assertEqual(e.PROTOCOLS['OrbUncoupled'][:3], ('MpOrb', 'MpOrbUncoupled', 'OrbCC uncoupled'))
        self.assertNotIn('OrbUncoupled', e.SHUFFLE)
        for number, expected in ((1, 250), (2, 250), (3, 275), (4, 475)):
            configs = e.configurations(number)
            self.assertEqual(len(configs), expected)
            self.assertEqual(len({c['config'] for c in configs}), expected)
            self.assertTrue(all(c['subflows'] == 2 for c in configs))
            self.assertEqual({c['connections'] for c in configs}, {1 if number <= 2 else 2})
            expected_runs = set(range(1, 51 if number <= 2 else 6))
            self.assertEqual(set(e.run_numbers(number)), expected_runs)
            self.assertEqual(set(e.run_numbers(number, 50)), expected_runs)
            self.assertEqual(set(e.run_numbers(number, 2)), {1, 2})
            for protocol, case in {(c['protocol'], c['case']) for c in configs}:
                self.assertEqual({c['run'] for c in configs if c['protocol'] == protocol and c['case'] == case}, expected_runs)
            self.assertTrue(all(c['seed'] == 7999 + c['run'] for c in configs))
        inter = e.configurations(3)
        self.assertTrue(any(c['path_rtts_ms'] == [[50, 50], [50, 50]] for c in inter))
        intra = e.configurations(4)
        self.assertEqual(len([c for c in intra if c['rtt_ms'] == 20]), 25)
        self.assertTrue(any(c['path_rtts_ms'] == [[20, 20], [200, 20]] for c in intra))
        for case in {c['case'] for c in inter}:
            starts = [c['starts'] for c in inter if c['case'] == case and c['run'] == 1]
            self.assertTrue(all(s == starts[0] for s in starts))

    def test_random_paths_are_independent_and_reproducible(self):
        records = e.path_conditions(1)
        self.assertEqual(records, e.path_conditions(1))
        self.assertNotEqual(records, e.path_conditions(2))
        for path in range(2):
            rows = [r for r in records if r['path'] == path]
            self.assertEqual([r['time'] for r in rows], list(range(0, 300, 15)))
            self.assertTrue(all(50 <= r['bandwidth_mbps'] <= 100 and 1 <= r['rtt_ms'] <= 100 for r in rows))
            self.assertTrue(all(.045 <= r['outage_seconds'] <= .120 for r in rows[1:]))
        self.assertNotEqual([r['rtt_ms'] for r in records if r['path'] == 0],
                            [r['rtt_ms'] for r in records if r['path'] == 1])

    def test_responsiveness_reconnect_and_loss_pairing(self):
        with tempfile.TemporaryDirectory() as temp:
            roots = [Path(temp) / str(n) for n in (1, 2)]
            for n, root in zip((1, 2), roots):
                (root / 'scenarios').mkdir(parents=True)
                e.responsiveness_scenario(n, 1, root)
                tree = ET.parse(root / 'scenarios/run1.xml')
                self.assertEqual(len(tree.findall('.//disconnect')), 76)
                self.assertEqual(len(tree.findall('.//connect')), 76)
                self.assertTrue(all(node.attrib['module'].endswith('.ppp[0]') for node in tree.findall('.//crash')))
                if n == 1:
                    self.assertTrue(all(node.attrib['value'] == '0' for node in tree.findall('.//param[@name="per"]')))
            records = [json.loads((root / 'scenarios/run1.json').read_text()) for root in roots]
            self.assertTrue(all(row['per'] == 0 for row in records[0]))
            self.assertTrue(any(row['per'] > 0 for row in records[1]))
            for first, second in zip(*records):
                self.assertEqual({k: v for k, v in first.items() if k != 'per'},
                                 {k: v for k, v in second.items() if k != 'per'})

    def test_generation_covers_fifty_response_trials_and_pairs_conditions(self):
        with tempfile.TemporaryDirectory() as temp:
            roots = {n: Path(temp) / str(n) for n in (1, 2)}
            for number, root in roots.items():
                with patch.object(e, 'directory', return_value=root):
                    configs = e.generate(number)
                self.assertEqual(len(configs), 250)
                self.assertEqual(len(list((root / 'scenarios').glob('run*.xml'))), 50)
                self.assertEqual(len(list((root / 'scenarios').glob('run*.json'))), 50)
                for protocol in e.PROTOCOLS:
                    ini = configparser.ConfigParser(interpolation=None, strict=False, delimiters=('=',))
                    ini.read(root / f'experiment{number}_{protocol.lower()}.ini')
                    self.assertEqual(len([s for s in ini.sections() if s.startswith('Config ')]), 50)
                    final = next(c for c in configs if c['protocol'] == protocol and c['run'] == 50)
                    self.assertEqual(ini[f'Config {final["config"]}']['seed-set'], '8049')
                self.assertEqual(json.loads((root / 'matrix.json').read_text()), configs)
            records = [json.loads((roots[n] / 'scenarios/run50.json').read_text()) for n in (1, 2)]
            self.assertTrue(all(row['per'] == 0 for row in records[0]))
            self.assertTrue(any(row['per'] > 0 for row in records[1]))
            self.assertEqual([{k: v for k, v in row.items() if k != 'per'} for row in records[0]],
                             [{k: v for k, v in row.items() if k != 'per'} for row in records[1]])

    def test_runner_default_and_run_limit_select_fifty_response_five_rtt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for number in range(1, 5):
                (root / str(number)).mkdir()
                (root / str(number) / 'matrix.json').write_text(json.dumps(e.configurations(number)))
            for flags, expected in (([], 1250), (['--runs', '50'], 1250), (['--runs', '2'], 320)):
                output = io.StringIO()
                with patch.object(runner, 'directory', side_effect=lambda n: root / str(n)), \
                     patch.object(sys, 'argv', ['runExperiments.py', '--skip-generate', '--dry-run', *flags]), \
                     redirect_stdout(output):
                    runner.main()
                self.assertIn(f'Selected {expected} simulations', output.getvalue())
                if not flags or flags == ['--runs', '50']:
                    self.assertIn('Delta_Responsiveness_Run50', output.getvalue())
                    self.assertIn('Delta_ResponsivenessLoss_Run50', output.getvalue())
                    self.assertNotIn('InterRtt_R20_Run6', output.getvalue())
                    self.assertNotIn('IntraRtt_R20_Aligned_Run6', output.getvalue())

    def test_plot_selectors_include_response_trial_fifty_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for number in range(1, 5):
                path = root / str(number)
                path.mkdir()
                configs = e.configurations(number)
                (path / 'matrix.json').write_text(json.dumps(configs))
                last = next(c for c in configs if c['run'] == (50 if number <= 2 else 5))
                out = path / 'csvs/extracted' / last['config']
                out.mkdir(parents=True)
                (out / 'summary.json').write_text(json.dumps(dict(last, sim_time=18, measurement_end=17)))
            with patch.object(aggregates, 'directory', side_effect=lambda n: root / str(n)), \
                 patch.object(aggregates, 'plot') as plotted:
                for flags, expected in (([], [(1, 50), (2, 50), (3, 5), (4, 5)]),
                                        (['--runs', '5'], [(3, 5), (4, 5)])):
                    with patch.object(sys, 'argv', ['plotAggregates.py', *flags]):
                        aggregates.main()
                    self.assertEqual([(c['experiment'], c['run']) for c in plotted.call_args.args[0]], expected)
            with patch.object(metrics, 'directory', side_effect=lambda n: root / str(n)), \
                 patch.object(metrics, 'plot_metrics') as plotted:
                for number in range(1, 5):
                    with patch.object(sys, 'argv', ['plotMetrics.py', '--experiment', str(number), '--metrics', 'cwnd']):
                        metrics.main()
                    self.assertEqual([c['run'] for c in plotted.call_args.args[0]], [50 if number <= 2 else 5])

    def test_ini_paths_queue_scope_and_rtt_arithmetic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(e, 'directory', return_value=root):
                configs = e.generate(4)
            for protocol in e.PROTOCOLS:
                ini = configparser.ConfigParser(interpolation=None, strict=False, delimiters=('=',))
                ini.read(root / f'experiment4_{protocol.lower()}.ini')
                general = ini['General']
                if protocol == 'OrbUncoupled':
                    self.assertEqual(general['**.tcp.typename'], '"MpOrb"')
                    self.assertEqual(general['**.tcp.tcpalgorithmclass'], '"MpOrbUncoupled"')
                self.assertEqual(general['**.numberOfSubflows'.lower()], '2')
                expected = 'MpOrbDeltaQueue' if protocol in e.SHUFFLE else 'PintQueue'
                for path in range(2):
                    self.assertEqual(general[f'*.left[{path}].ppp[0].queue.typename'], f'"{expected}"')
                self.assertEqual(general['**.ppp[*].queue.typename'], '"DropTailQueue"')
                self.assertEqual(general['**.statistic-recording'], 'false')
                self.assertEqual(general['**.vector-recording'], 'false')
                self.assertEqual(general['**.scalar-recording'], 'false')
                enabled = [key for key, value in general.items() if key.endswith('.vector-recording') and value == 'true']
                self.assertEqual({key.split(':')[0].rsplit('.', 1)[1] for key in enabled},
                                 {metric.lower() for metric in e.recorded_metrics(4)})
            for config in configs[::5]:
                tree = ET.parse(root / config['scenario'])
                self.assertEqual(len(tree.findall('at')), 1)  # competition throughout
                for connection in range(2):
                    for path in range(2):
                        link = tree.find(f'.//set-channel-param[@src-module="client[{connection}]"][@src-gate="pppg$o[{path}]"]')
                        access = float(link.attrib['value'].removesuffix('ms'))
                        self.assertAlmostEqual(4 * access + 1, config['path_rtts_ms'][connection][path])

    def test_application_goodput_does_not_sum_subflow_throughput(self):
        config = e.configurations(3)[0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / 'csvs/raw'
            raw.mkdir(parents=True)
            with (raw / f'{config["config"]}.csv').open('w', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(['type', 'module', 'name', 'value', 'vectime', 'vecvalue'])
                for i, rate in enumerate((10, 20)):
                    writer.writerow(['scalar', f'n.server[{i}].app[0]', 'bytesRcvd', 1000000, '', ''])
                    writer.writerow(['vector', f'n.server[{i}].app[0].thread_3', 'goodput:vector', '', '0 10 299 300', ' '.join([str(rate * 1e6)] * 4)])
                    writer.writerow(['vector', f'n.server[{i}].tcp.conn-1', 'throughput:vector', '', '0 10 299 300', '999999999 999999999 999999999 999999999'])
                    writer.writerow(['vector', f'n.client[{i}].tcp.conn-2', 'rtt:vector', '', '0 10 299 300', '.04 .04 .04 .04'])
                writer.writerow(['vector', 'n.server[0].tcp.conn-3', 'holBlockedBytes:vector', '', '0 10 299', '0 1200 0'])
                writer.writerow(['vector', 'n.client[0].tcp.conn-2', 'metaReinjectedBytes:vector', '', '0 10 299', '0 1200 2400'])
                writer.writerow(['vector', 'n.client[0].tcp.conn-2', 'U:vector', '', '0 10 299', '1 1 1'])
            with patch.object(a, 'directory', return_value=root):
                summary = a.extract(config)
            self.assertEqual(summary['goodput_mbps'], [10, 20])
            self.assertEqual(summary['total_goodput_mbps'], 30)
            self.assertAlmostEqual(summary['jain'], .9)
            self.assertAlmostEqual(summary['b_over_a'], 2)
            out = root / 'csvs/extracted' / config['config']
            self.assertEqual(len(list(out.glob('*__goodput.csv'))), 2)
            self.assertEqual(len(list(out.glob('*__throughput.csv'))), 2)
            self.assertEqual(len(list(out.glob('*__rtt.csv'))), 2)
            self.assertEqual(len(list(out.glob('*__holBlockedBytes.csv'))), 1)
            self.assertEqual(len(list(out.glob('*__metaReinjectedBytes.csv'))), 1)
            self.assertFalse(list(out.glob('*__U.csv')))

    def test_recording_matches_orbtcp_baseline_plus_exactly_two_extras(self):
        baseline = {'goodput', 'cwnd', 'rtt', 'srtt', 'throughput', 'mbytesInFlight',
                    'retransmissionRate', 'queueLength', 'bandwidth'}
        for number in range(1, 5):
            expected = baseline - ({'retransmissionRate'} if number == 4 else set())
            self.assertEqual(set(e.recorded_metrics(number)), expected | {'holBlockedBytes', 'metaReinjectedBytes'})
            self.assertFalse(a.accepted_vector('n.client[0].tcp.conn-4', 'U', number))
            self.assertFalse(a.accepted_vector('n.left[0].ppp[1].queue', 'queueLength', number))
            self.assertTrue(a.accepted_vector('n.left[0].ppp[0].queue', 'queueLength', number))
            self.assertTrue(a.accepted_vector('n.server[0].tcp.conn-4', 'holBlockedBytes', number))
            self.assertFalse(a.accepted_vector('n.client[0].tcp.conn-4', 'holBlockedBytes', number))
            self.assertTrue(a.accepted_vector('n.client[0].tcp.conn-4', 'metaReinjectedBytes', number))

    def test_individual_plot_downsampling_is_bounded_and_preserves_extrema(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'queue.csv'
            with path.open('w') as handle:
                handle.write('time,value\n')
                for index in range(10000):
                    handle.write(f'{index / 100},{9999 if index == 5050 else 1}\n')
            points = read_series(path, horizon=100, bins=10)
            self.assertLessEqual(len(points), 40)
            self.assertEqual(max(points[:, 1]), 9999)
            self.assertTrue(all(points[i, 0] <= points[i+1, 0] for i in range(len(points)-1)))

    def test_cdf_uses_actual_number_of_finite_run_means(self):
        x, y = a.cdf_points([30, 0, 10, math.nan])
        self.assertEqual(x.tolist(), [0, 0, 10, 30])
        for actual, expected in zip(y, [0, 100 / 3, 200 / 3, 100]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(len(a.cdf_points([math.nan])[0]), 0)

    def test_capacity_reference_weights_outages_and_measurement_window(self):
        records = [dict(path=0, time=0, up_time=0, bandwidth_mbps=100),
                   dict(path=1, time=0, up_time=0, bandwidth_mbps=80),
                   dict(path=0, time=15, up_time=15.1, bandwidth_mbps=50),
                   dict(path=1, time=30, up_time=30.1, bandwidth_mbps=60)]
        self.assertAlmostEqual(a.average_capacity(records, 10, 20), 154.5)

    def test_base_rtt_averages_paths_and_excludes_outages_instead_of_using_zero(self):
        records = [dict(path=0, time=0, up_time=0, rtt_ms=20),
                   dict(path=1, time=0, up_time=0, rtt_ms=60),
                   dict(path=0, time=15, up_time=16, rtt_ms=80)]
        self.assertAlmostEqual(a.average_base_rtt(records, 10, 20), ((5 * 20 + 4 * 80) / 9 + 60) / 2)

    def test_rtt_means_weight_seconds_then_subflows_and_ignore_receiver_meta(self):
        config = dict(e.configurations(1)[0], measurement_start=10, measurement_end=12)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / 'csvs/extracted' / config['config']
            out.mkdir(parents=True)
            (root / 'scenarios').mkdir()
            (root / 'scenarios/run1.json').write_text(json.dumps([
                dict(path=p, time=0, up_time=0, rtt_ms=rtt) for p, rtt in ((0, 20), (1, 40))]))
            (out / 'n.client[0].tcp.conn-1__rtt.csv').write_text('time,value\n')
            (out / 'n.server[0].tcp.conn-2__rtt.csv').write_text('time,value\n10.1,10\n')
            (out / 'n.client[0].tcp.conn-2__rtt.csv').write_text(
                'time,value\n9.9,99\n10.1,.02\n10.2,.04\n11.1,.06\n11.2,nan\n11.3,0\n12,99\n')
            (out / 'n.client[0].tcp.conn-3__rtt.csv').write_text(
                'time,value\n' + ''.join(f'{10 + i / 100},.1\n' for i in range(10)) + '11.1,.2\n')
            with patch.object(a, 'directory', return_value=root):
                row, paths = a.rtt_observation(config)
                self.assertTrue(row['included'])
                self.assertEqual(row['observed_subflows'], 2)
                self.assertEqual(row['samples'], 14)
                self.assertAlmostEqual(row['average_rtt_ms'], 97.5)
                self.assertAlmostEqual(row['base_rtt_ms'], 30)
                self.assertEqual([p['seconds'] for p in paths], [2, 2])
                (out / 'n.client[0].tcp.conn-3__rtt.csv').write_text('time,value\n9,.1\n')
                row, paths = a.rtt_observation(config)
                self.assertFalse(row['included'])
                self.assertEqual(row['observed_subflows'], 1)
                self.assertTrue(math.isnan(row['average_rtt_ms']))

    def test_missing_rtt_subflows_are_reported_without_leaving_a_stale_cdf(self):
        config = e.configurations(1)[0]
        with tempfile.TemporaryDirectory() as temp:
            sim = Path(temp)
            root = sim / 'experiments/experiment1'
            (root / 'scenarios').mkdir(parents=True)
            (root / 'scenarios/run1.json').write_text(json.dumps([
                dict(path=p, time=0, up_time=0, rtt_ms=20) for p in (0, 1)]))
            output = sim / 'plots/experiment1'
            output.mkdir(parents=True)
            for suffix in ('png', 'pdf'):
                (output / f'rtt_cdf.{suffix}').write_bytes(b'old plot')
            with patch.object(a, 'SIM_DIR', sim), patch.object(a, 'directory', return_value=root):
                with self.assertRaisesRegex(ValueError, 'both sending subflows'):
                    a.plot_rtt_cdf([config])
            with (output / 'rtt_plot_data.csv').open() as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row['included'], 'False')
            self.assertEqual(row['observed_subflows'], '0')
            self.assertFalse((output / 'rtt_cdf.png').exists())
            self.assertFalse((output / 'rtt_cdf.pdf').exists())

    def test_rtt_ratio_averages_aligned_samples_not_overall_means(self):
        config = dict(e.configurations(3)[0], measurement_start=10, measurement_end=14,
                      goodput_mbps=[25.25, 50.25])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / 'csvs/extracted' / config['config']
            out.mkdir(parents=True)
            (out / 'timeseries.csv').write_text(
                'time,connection_0_goodput_mbps,connection_1_goodput_mbps\n'
                '10,100,100\n11,100,1\n12,1,100\n13,0,0\n14,0,100\n15,100,100\n')
            with patch.object(a, 'directory', return_value=root):
                result = a.ratio_observation(config)
                self.assertAlmostEqual(result['goodput_ratio'], .02 / 3)
                self.assertEqual(result['samples'], 3)
                self.assertEqual(result['both_idle_samples'], 1)
                result = a.ratio_observation(dict(config, goodput_mbps=[math.nan, 20]))
                self.assertTrue(math.isnan(result['goodput_ratio']))

    def test_aggregate_loads_only_selected_experiments_and_checks_freshness(self):
        configs = [dict(e.configurations(n)[0], sim_time=18, measurement_end=17) for n in (1, 2)]
        with tempfile.TemporaryDirectory() as temp:
            def root(number):
                return Path(temp) / str(number)
            for config in configs:
                path = root(config['experiment'])
                (path / 'csvs/raw').mkdir(parents=True)
                raw = path / 'csvs/raw' / f'{config["config"]}.csv'
                raw.write_text('fixture\n')
                stat = raw.stat()
                summary = dict(config, raw_stat=[stat.st_size, stat.st_mtime_ns], goodput_mbps=[100])
                out = path / 'csvs/extracted' / config['config']
                out.mkdir(parents=True)
                (out / 'summary.json').write_text(json.dumps(summary))
                (path / 'matrix.json').write_text(json.dumps(e.configurations(config['experiment'])))
            with patch.object(a, 'directory', side_effect=root):
                self.assertEqual(len(a.aggregate_summaries(configs[:1])), 1)
                self.assertEqual(len(a.aggregate_summaries(configs)), 2)
                second = root(2) / 'csvs/extracted' / configs[1]['config'] / 'summary.json'
                summary = json.loads(second.read_text())
                second.write_text(json.dumps(dict(summary, sim_time=300, measurement_end=299)))
                self.assertEqual(len(a.aggregate_summaries(configs[:1])), 1)
                with self.assertRaisesRegex(ValueError, 'mismatched extraction'):
                    a.aggregate_summaries(configs)

    def test_cdf_outputs_keep_loss_and_no_loss_trials_separate(self):
        configs = [dict(e.configurations(n)[0], sim_time=18, measurement_end=17) for n in (1, 2)]
        summaries = {c['experiment']: dict(c, goodput_mbps=[100 if c['experiment'] == 1 else 25]) for c in configs}
        with tempfile.TemporaryDirectory() as temp:
            sim = Path(temp)
            def root(number):
                return sim / 'experiments' / f'experiment{number}'
            for number in (1, 2):
                scenarios = root(number) / 'scenarios'
                scenarios.mkdir(parents=True)
                (scenarios / 'run1.json').write_text(json.dumps([
                    dict(path=path, time=0, up_time=0, bandwidth_mbps=100, rtt_ms=20 + 20 * path) for path in (0, 1)]))
                out = root(number) / 'csvs/extracted' / configs[number - 1]['config']
                out.mkdir(parents=True)
                for index, rtt in enumerate((.02, .04) if number == 1 else (.1, .14)):
                    (out / f'n.client[0].tcp.conn-{index + 1}__rtt.csv').write_text(f'time,value\n10.1,{rtt}\n11.1,{rtt}\n')
            old = sim / 'plots/experiment1and2/joined_goodput_cdf.png'
            old.parent.mkdir(parents=True)
            old.write_bytes(b'old plot')
            with patch.object(a, 'SIM_DIR', sim), patch.object(a, 'directory', side_effect=root), \
                 patch.object(a, 'read_summary', side_effect=lambda c: summaries[c['experiment']]):
                a.plot(configs)
            for number, expected in ((1, 100), (2, 25)):
                out = sim / 'plots' / f'experiment{number}'
                for suffix in ('png', 'pdf'):
                    self.assertGreater((out / f'goodput_cdf.{suffix}').stat().st_size, 0)
                    self.assertGreater((out / f'rtt_cdf.{suffix}').stat().st_size, 0)
                with (out / 'plot_data.csv').open() as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 1)
                self.assertEqual(int(rows[0]['experiment']), number)
                self.assertEqual(float(rows[0]['average_goodput_mbps']), expected)
                with (out / 'rtt_plot_data.csv').open() as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 1)
                self.assertEqual(int(rows[0]['experiment']), number)
                self.assertAlmostEqual(float(rows[0]['average_rtt_ms']), 30 if number == 1 else 120)
                with (out / 'base_rtt_reference.csv').open() as handle:
                    self.assertEqual(len(list(csv.DictReader(handle))), 1)
            self.assertFalse(old.exists())

    def runner_events(self, options=(), fail_export=None):
        """Run the actual CLI orchestration with bounded stand-ins for expensive stages."""
        events = []
        error = None
        matrices = {n: e.configurations(n)[:2] for n in range(1, 5)}
        def parallel(function, jobs, cores, processes=False):
            selected = [job[0] for job in jobs] if function is runner.simulate else jobs
            numbers = {c['experiment'] for c in selected}
            self.assertEqual(len(numbers), 1)
            number = numbers.pop()
            events.append((number, function.__name__))
            self.assertEqual(cores, 2 if processes else 30)
            if function is runner.export and number == fail_export:
                raise RuntimeError('export failed')
            return selected
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            raw = root / 'raw.csv'
            raw.write_text('fixture\n')
            for n, configs in matrices.items():
                (root / str(n)).mkdir()
                (root / str(n) / 'matrix.json').write_text(json.dumps(configs))
            stack.enter_context(patch.object(runner, 'generate', side_effect=lambda n: matrices[n]))
            stack.enter_context(patch.object(runner, 'directory', side_effect=lambda n: root / str(n)))
            stack.enter_context(patch.object(runner, 'parallel', side_effect=parallel))
            stack.enter_context(patch.object(runner, 'completion', return_value={'outputs': {'.vec': [1, 1], '.sca': [1, 1]}}))
            stack.enter_context(patch.object(runner, 'raw_path', return_value=raw))
            stack.enter_context(patch.object(resources, 'extraction_workers', side_effect=lambda cores, jobs: min(cores, jobs)))
            stack.enter_context(patch.object(a, 'write_summaries'))
            stack.enter_context(patch.object(a, 'plot', side_effect=lambda configs: events.append((configs[0]['experiment'], 'plot'))))
            def individual(configs, selected_metrics):
                number = configs[0]['experiment']
                self.assertEqual(selected_metrics, list(e.recorded_metrics(number)))
                events.append((number, 'individual'))
            stack.enter_context(patch.object(metrics, 'plot_metrics', side_effect=individual))
            stack.enter_context(patch.object(sys, 'argv', ['runExperiments.py', '--experiments', '4', '2', '1', '3',
                                                       '--cores', '30', '--plots', 'all', *options]))
            with redirect_stdout(io.StringIO()):
                try:
                    runner.main()
                except RuntimeError as caught:
                    error = caught
        return events, error

    def test_runner_finishes_plots_before_simulating_next_experiment(self):
        events, error = self.runner_events()
        self.assertIsNone(error)
        self.assertEqual(events, [(n, stage) for n in range(1, 5)
                                 for stage in ('simulate', 'export', 'extract', 'plot', 'individual')])

    def test_runner_resume_applies_selected_steps_to_each_experiment(self):
        events, error = self.runner_events(['--start-step', '3', '--end-step', '4'])
        self.assertIsNone(error)
        self.assertEqual(events, [(n, stage) for n in range(1, 5) for stage in ('extract', 'plot', 'individual')])

    def test_runner_failure_keeps_earlier_plots_and_stops_later_experiments(self):
        events, error = self.runner_events(fail_export=2)
        self.assertEqual(str(error), 'export failed')
        self.assertEqual(events, [(1, stage) for stage in ('simulate', 'export', 'extract', 'plot', 'individual')]
                         + [(2, 'simulate'), (2, 'export')])


if __name__ == '__main__':
    unittest.main()
