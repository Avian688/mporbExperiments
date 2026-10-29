import configparser
import csv
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
from plotMetrics import read_series


class ExperimentsTests(unittest.TestCase):
    def test_matrix_has_only_requested_ccs_and_two_subflows(self):
        self.assertEqual(tuple(e.PROTOCOLS), ('SenderDelta', 'Omega', 'Alpha', 'Theta', 'CubicUncoupled'))
        for number, expected in ((1, 25), (2, 25), (3, 275), (4, 475)):
            configs = e.configurations(number)
            self.assertEqual(len(configs), expected)
            self.assertEqual(len({c['config'] for c in configs}), expected)
            self.assertTrue(all(c['subflows'] == 2 for c in configs))
            self.assertEqual({c['connections'] for c in configs}, {1 if number <= 2 else 2})
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

    def test_ini_paths_queue_scope_and_rtt_arithmetic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(e, 'directory', return_value=root):
                configs = e.generate(4)
            for protocol in e.PROTOCOLS:
                ini = configparser.ConfigParser(interpolation=None, strict=False, delimiters=('=',))
                ini.read(root / f'experiment4_{protocol.lower()}.ini')
                general = ini['General']
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

    def test_joint_aggregate_includes_available_matching_counterpart_once(self):
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
                self.assertEqual(len(a.aggregate_summaries(configs[:1])), 2)
                self.assertEqual(len(a.aggregate_summaries(configs)), 2)
                second = root(2) / 'csvs/extracted' / configs[1]['config'] / 'summary.json'
                summary = json.loads(second.read_text())
                second.write_text(json.dumps(dict(summary, sim_time=300, measurement_end=299)))
                self.assertEqual(len(a.aggregate_summaries(configs[:1])), 1)
                with self.assertRaisesRegex(ValueError, 'mismatched extraction'):
                    a.aggregate_summaries(configs)


if __name__ == '__main__':
    unittest.main()
