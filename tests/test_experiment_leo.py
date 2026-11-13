"""Generator, saved-route preflight, and synthetic extraction/plot regression checks."""
import contextlib
import csv
import io
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'simulations/pythonScripts'))
sys.path.insert(0,str(PROJECT/'simulations/pythonScripts/experimentLeo'))
import leo
import analyzeExperimentLeo as analysis
from vectors import Vectors, interval_mean


def fixture(config, specifications):
    path = leo.result(config,'.vec')
    path.parent.mkdir(parents=True,exist_ok=True)
    definitions=[]
    payload=bytearray(b'version 3\nrun synthetic\n')
    for i,(module,metric,samples) in enumerate(specifications):
        definitions.append(f'vector {i} {module} {metric}:vector ETV\n')
    payload.extend(''.join(definitions).encode())
    blocks=[]
    for i,(_,_,samples) in enumerate(specifications):
        if not samples:
            continue
        offset=len(payload)
        data=''.join(f'{i}\t{n}\t{t}\t{v}\n' for n,(t,v) in enumerate(samples)).encode()
        payload.extend(data)
        blocks.append(f'{i} {offset} {len(data)} 0 {len(samples)-1} {samples[0][0]} {samples[-1][0]} {len(samples)} 0 1 0 0\n')
    path.write_bytes(payload)
    path.with_suffix('.vci').write_text(f'file {len(payload)} {int(path.stat().st_mtime)}\nversion 2\n'+''.join(definitions)+''.join(blocks))
    path.with_suffix('.sca').write_text('version 2\nrun synthetic\n')


def ping_fixture(duration=15,replay_start=0):
    specifications=[]
    for p in leo.PAIRS:
        for rank in range(11):
            module=f'leo.userTerminal[{p["source"]}].app[{rank}]'
            base=.025 if rank==0 else .02+rank*.01
            specifications.append((module,'rtt',[(t+.1,base) for t in range(int(duration))]))
            if rank:
                specifications.append((module,'kPathAvailable',[(.000002,1 if rank<=3 else 0)]))
    c=leo.ping_config(duration,replay_start)
    fixture(c,specifications)
    analysis.extract_ping(c)


def traffic_fixture(config):
    specifications=[]
    for p in leo.PAIRS:
        specifications.append((f'leo.userTerminal[{p["destination"]}].app[0].thread_99','goodput',[(t,12e6) for t in range(9,15)]))
        prefix=f'leo.userTerminal[{p["source"]}].tcp.'
        if config['protocol']=='OrbSingle':
            specifications.append((prefix+'conn-51','rtt',[(t+.2,.035) for t in range(10,14)]))
        else:
            specifications.append((prefix+'conn-100','rtt',[]))
            # Deliberately different index declaration order; active creation IDs
            # establish ranks. Receiver order is deliberately irrelevant.
            for rank in reversed(range(1,config['k']+1)):
                specifications.append((prefix+f'conn-{100+rank}','rtt',[(t+.2,.03+rank*.01) for t in range(10,14)]))
    fixture(config,specifications)


def diagnostic_fixture(config):
    traffic_fixture(config)
    vectors=Vectors(leo.result(config,'.vec'))
    specifications=[(v.module,v.metric,list(vectors.samples(v))) for v in vectors.items.values()]
    for p in leo.PAIRS:
        sender=f'leo.userTerminal[{p["source"]}].tcp.'
        receiver=f'leo.userTerminal[{p["destination"]}].tcp.'
        single=config['protocol']=='OrbSingle'
        specifications.append((sender+('conn-51' if single else 'conn-100'),'metaReinjectedBytes',[]))
        if not single:
            # The meta/control window must not masquerade as a data-subflow CWND.
            specifications.append((sender+'conn-100','cwnd',[(1,9999999),(14,9999999)]))
        for rank in range(1,config['k']+1):
            module=sender+('conn-51' if single else f'conn-{100+rank}')
            for metric,samples in (
                ('cwnd',[(1,1200*rank),(10,2400*rank),(14,3600*rank)]),
                ('U',[(1,.9),(10,1.1),(14,.95)]),
                ('mbytesInFlight',[(1,0),(10,1200*rank),(14,0)]),
                ('retransmissionRate',[(1,0),(10,1e6),(14,0)]),
                ('numRtos',[])):
                specifications.append((module,metric,samples))
            # Deliberately unrelated receiver IDs; plot them without rank labels.
            specifications.append((receiver+f'conn-{900-7*rank}','throughput',[(1,1e6),(10,2e6),(14,3e6)]))
        specifications.append((receiver+'conn-500','holBlockedBytes',[(1,0),(10,1000),(14,0)]))
    specifications.append(('leo.satellite[0].tcp.conn-9','U',[(1,999)]))
    fixture(config,specifications)


def export_fixture(config,bins=3000):
    from terminalVectors import export_vectors
    return export_vectors(config,Vectors(leo.result(config,'.vec')),bins=bins)


class LeoTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        self.override=patch.multiple(leo,EXPERIMENT_DIR=root/'experimentLeo',PING_DIR=root/'experimentLeoPing',PLOT_DIR=root/'plots')
        self.override.start()
        self.matrix=leo.generate(15,root/'routes')

    def tearDown(self):
        self.override.stop()
        self.temp.cleanup()

    def test_load_balance_uses_same_matrix_and_isolated_replay_inputs(self):
        root = Path(self.temp.name)
        old_ini = leo.EXPERIMENT_DIR/'experimentLeo.ini'
        original = old_ini.read_bytes()
        matrix = leo.generate(leo.LOAD_BALANCE_DURATION, root/'routes', replay_start=leo.LOAD_BALANCE_START)
        standard = [c for c in matrix if c['protocol'] != leo.OMEGA_10]
        self.assertEqual(len(standard), 130)
        self.assertEqual([(c['config'], c['starts']) for c in standard],
                         [(c['config'], c['starts']) for c in self.matrix])
        self.assertEqual(old_ini.read_bytes(), original)
        self.assertTrue(all((c['sim_time'],c['measurement_start'],c['measurement_end']) ==
                            (70,10,69) for c in matrix))
        traffic = leo.ini_file(matrix[0]).read_text()
        capture, replay = traffic.split('[Config LeoTransport]', 1)
        self.assertIn('captureReplayAt = 115s', capture)
        self.assertIn('sim-time-limit = 115.000002s', capture)
        self.assertIn('kPathSnapshotMode = "disabled"', capture)
        self.assertIn('replayStart = 115s', replay)
        self.assertIn('sim-time-limit = 70s', replay)
        ping = leo.ini_file(leo.baseline_config(matrix[0])).read_text()
        self.assertIn('include ../experimentLeoLoadBalance/topology.ini', ping)
        self.assertIn('replayStart = 115s', ping)
        self.assertIn('../experimentLeoLoadBalance/results/replay/115.leoreplay', ping)

    def test_omega10_is_opt_in_and_uses_the_same_start_patterns(self):
        import runExperimentLeo as runner
        matrix = leo.generate(70, Path(self.temp.name)/'routes', replay_start=115)
        variants = [c for c in matrix if c['protocol'] == leo.OMEGA_10]
        omega = [c for c in matrix if c['protocol'] == 'Omega']
        self.assertEqual(len(variants), 25)
        self.assertEqual([(c['k'], c['run'], c['starts']) for c in variants],
                         [(c['k'], c['run'], c['starts']) for c in omega])
        standard_ini = leo.ini_file(omega[0]).read_text()
        self.assertNotIn('Omega10', standard_ini)
        self.assertNotIn('deltaShuffleFraction = 0.1', standard_ini)
        variant_ini = leo.ini_file(variants[0]).read_text()
        self.assertIn(f'[Config {variants[0]["config"]}]', variant_ini)
        self.assertIn('tcpAlgorithmClass = "MpOrbOmega"', variant_ini)
        self.assertEqual(variant_ini.count('deltaShuffleFraction = 0.1\n'), 3 * len(variants))
        for options, count in (([], 130), (['--protocols', 'Omega10'], 25),
                               (['--include-omega10'], 155)):
            with self.subTest(options=options), contextlib.redirect_stdout(io.StringIO()) as output:
                runner.main(['--dry-run', '--skip-generate', *options], replay_start=115)
                self.assertIn(f'Selected {count} traffic runs', output.getvalue())
                self.assertEqual('Omega10_EdgeDisjoint_K3_Run1' in output.getvalue(), bool(options))

    def test_capture_reuses_checkpoint_and_failed_refresh_preserves_it(self):
        from types import SimpleNamespace
        import runExperimentLeo as runner
        args = SimpleNamespace(route_store=Path(self.temp.name)/'routes', sim_timeout_seconds=5)
        leo.generate(100,args.route_store,replay_start=170)
        checkpoint = leo.checkpoint_file({'replay_start':170})

        def write_checkpoint(command, cwd, log, timeout):
            candidate = Path(json.loads(command[-1].split('=',1)[1]))
            self.assertNotEqual(candidate,checkpoint)
            candidate.write_text('synthetic checkpoint')
            return True

        with patch.object(leo,'check_routes',return_value='prefix') as preflight, \
             patch.object(runner,'fingerprint',return_value='capture-1') as fingerprint, \
             patch.object(runner,'command',return_value=['opp_run']), \
             patch.object(runner,'logged',side_effect=write_checkpoint) as launched, \
             contextlib.redirect_stdout(io.StringIO()):
            runner.prepare_replay(args,170)
            runner.prepare_replay(args,170)
            launched.assert_called_once()
            preflight.assert_called_with(args.route_store,170.000002,primary_only=True)
            fingerprint.return_value = 'capture-2'
            launched.side_effect = None
            launched.return_value = False
            with self.assertRaisesRegex(RuntimeError,'Replay capture failed'):
                runner.prepare_replay(args,170)
        self.assertEqual(checkpoint.read_text(),'synthetic checkpoint')
        self.assertEqual(json.loads(checkpoint.with_suffix('.json').read_text())['fingerprint'],'capture-1')

    def test_load_balance_reuses_full_plot_pipeline(self):
        import runExperimentLeo as runner
        store = Path(self.temp.name)/'routes'
        matrix = leo.generate(15,store,replay_start=170)
        config = next(c for c in matrix if c['protocol']=='Omega' and c['k']==2 and c['run']==1)
        variant = next(c for c in matrix if c['protocol']==leo.OMEGA_10 and c['k']==2 and c['run']==1)
        comparison = [c for c in matrix if c['run'] == 1 and c['k'] == (1 if c['protocol'] == 'OrbSingle' else 2)]
        plots = Path(self.temp.name)/'plots'/'experimentLeo'
        with patch.object(leo,'PLOT_DIR',plots), patch.object(leo,'PAIRS',leo.PAIRS[:1]):
            ping_fixture(15,replay_start=170)
            for run in comparison:
                diagnostic_fixture(run)
                analysis.extract(run)
                export_fixture(run,bins=20)
            runner.main(['--start-step','4','--skip-generate','--configs',config['config']],
                        replay_start=170,default_duration=100)
            standard_heatmap = leo.plot_directory(config)/'heatmap_data.csv'
            standard_data = standard_heatmap.read_bytes()
            for kind in ('individual', 'summary'):
                runner.main(['--start-step','4','--skip-generate','--protocols','Omega10',
                             '--configs',variant['config'],'--plots',kind],
                            replay_start=170,default_duration=100)
            self.assertEqual(standard_heatmap.read_bytes(), standard_data)
            for case in (config, variant):
                output = leo.plot_directory(case)
                for name in ('goodput_K2','delay_K2'):
                    self.assertTrue((output/f'{name}.pdf').is_file())
                run = output/'individual'/leo.run_subdir(case)
                self.assertTrue((run/'aggregate/combined.pdf').is_file())
                self.assertTrue(any(run.glob('userTerminal*/combined.pdf')))
            with (leo.plot_directory(variant)/'heatmap_data.csv').open() as csv_file:
                rows = list(csv.DictReader(csv_file))
            self.assertEqual({row['protocol'] for row in rows}, set(leo.PROTOCOLS))
            self.assertEqual({row['n'] for row in rows}, {'1'})
            self.assertNotEqual(leo.plot_directory(config), leo.plot_directory(variant))
            self.assertFalse(plots.exists())

    def test_load_balance_fairness_only_uses_aligned_goodput_csvs(self):
        import runExperimentLeo as runner

        matrix = leo.generate(18, Path(self.temp.name)/'routes', replay_start=115)
        config = next(c for c in matrix if c['protocol'] == 'Omega' and c['k'] == 2 and c['run'] == 1)
        # Each connection gets zero for a different second. The last complete
        # second is an outage, and the final second has one missing receiver.
        specs = []
        for index, pair in enumerate(leo.PAIRS):
            samples = [(10, 0.)] + [(11 + second, 0. if index == second else 100e6)
                                    for second in range(5)] + [(16, 0.)]
            if index:
                samples.append((17, 0.))
            specs.append((f'leo.userTerminal[{pair["destination"]}].app[0].thread_99', 'goodput', samples))
        fixture(config, specs)
        export_fixture(config, bins=2)
        leo.result(config, '.vec').unlink()
        with patch.object(Vectors, '__init__', side_effect=AssertionError('must not open native vectors')), \
             patch.object(runner, 'simulate', side_effect=AssertionError('must not simulate')):
            runner.main(['--start-step','4','--skip-generate','--plots','fairness-efficiency',
                         '--protocols','Omega','--ks','2','--runs','1'], replay_start=115)
        output = leo.plot_directory(config)
        with (output/'fairness_efficiency_data.csv').open() as f:
            point, = csv.DictReader(f)
        self.assertAlmostEqual(float(point['mean_per_second_jain']), .8)
        self.assertAlmostEqual(float(point['jain_fairness']), 1.)
        self.assertAlmostEqual(float(point['normalized_goodput']), 4/7)
        self.assertEqual(point['seconds_with_jain'], '5')
        self.assertEqual(point['seconds_all_zero'], '1')
        self.assertEqual(point['seconds_missing_goodput'], '1')
        self.assertTrue((output/'fairness_efficiency_K2.pdf').is_file())
        self.assertFalse((output/'goodput_K2.pdf').exists())

    def test_matrix_and_endpoint_only_recording(self):
        self.assertEqual(len(self.matrix),130)
        self.assertEqual({c['seed'] for c in self.matrix},{1})
        for run in leo.RUNS:
            cases=[c for c in self.matrix if c['run']==run]
            self.assertEqual(len({tuple(c['starts']) for c in cases}),1)
        self.assertEqual(len({tuple(c['starts']) for c in self.matrix}),5)
        self.assertEqual([(p['source'],p['destination'],p['group']) for p in leo.PAIRS],[(i,i+5,i) for i in range(5,10)])
        text=(leo.EXPERIMENT_DIR/'experimentLeo.ini').read_text()
        self.assertNotIn('typename = "KShortestPathPingApp"',text)
        self.assertNotIn('typename = "PingApp"',text)
        enables=[line for line in text.splitlines() if '.statistic-recording = true' in line]
        self.assertEqual(len(enables),50)
        self.assertTrue(all('userTerminal[' in line for line in enables))
        expected_metrics=('rtt','goodput','cwnd','U','holBlockedBytes','throughput','retransmissionRate',
                          'mbytesInFlight','metaReinjectedBytes','numRtos')
        self.assertTrue(all(any(f'.{metric}.' in line for metric in expected_metrics) for line in enables))
        cwnd=[line for line in enables if '.cwnd.' in line]
        self.assertEqual(cwnd,[f'*.userTerminal[{source}].tcp.conn-*.cwnd.statistic-recording = true' for source in range(5,10)])
        for source in range(5,10):
            self.assertIn(f'*.userTerminal[{source}].tcp.conn-*.cwnd.result-recording-modes = vector(removeRepeats)',text)
            self.assertIn(f'*.userTerminal[{source}].tcp.conn-*.cwnd:vector(removeRepeats).vector-recording = true',text)
        for metric in ('U','holBlockedBytes','throughput','retransmissionRate',
                       'mbytesInFlight','metaReinjectedBytes','numRtos'):
            terminals=range(10,15) if metric in ('holBlockedBytes','throughput') else range(5,10)
            expected=[f'*.userTerminal[{i}].tcp.conn-*.{metric}.statistic-recording = true' for i in terminals]
            self.assertEqual([line for line in enables if f'.{metric}.' in line],expected)
            mode='vector' if metric in ('throughput','retransmissionRate') else 'vector(removeRepeats)'
            self.assertIn(f'*.userTerminal[{terminals.start}].tcp.conn-*.{metric}.result-recording-modes = {mode}',text)
        self.assertNotIn('queueLength',text)
        self.assertIn('**.statistic-recording = false',text)
        topology=(leo.EXPERIMENT_DIR/'topology.ini').read_text()
        self.assertNotIn('vector-recording = true',topology)
        self.assertIn('**.numOfUserTerminals = 15',topology)
        ping=(leo.PING_DIR/'experimentLeoPing.ini').read_text()
        self.assertEqual(ping.count('[Config LeoPing]'),1)
        self.assertEqual(ping.count('.typename = "KShortestPathPingApp"'),50)
        self.assertEqual(ping.count('.typename = "PingApp"'),5)
        self.assertNotIn('pingTxSeq',ping)
        self.assertNotIn('.cwnd.',ping)
        for metric in ('U','holBlockedBytes','throughput','retransmissionRate',
                       'mbytesInFlight','metaReinjectedBytes','numRtos'):
            self.assertNotIn(f'.{metric}.',ping)
        self.assertIn('**.ipv4.ip.kPathTcpPathGroups = "5 6 7 8 9"',text)
        self.assertIn('**.ipv4.ip.kPathTcpPathGroups = ""',text)

    def test_route_preflight_detects_missing_wrong_policy_and_timestamps(self):
        store=Path(self.temp.name)/'routes'
        primary,kpath=leo.route_directories(store)
        core=1584+len(leo.SHARED.CITY_COORDINATES)+len(leo.SHARED.load_ground_stations())
        with self.assertRaises(FileNotFoundError):leo.check_routes(store,.3)
        for directory,is_k in ((primary,False),(kpath,True)):
            directory.mkdir(parents=True,exist_ok=True)
            for micros in (0,100001,200001):
                words=[0]*(23 if is_k else 24)
                words[:3]=[0x4c454f4b,3,23] if is_k else [0x4c454f33,3,24]
                if is_k:
                    words[6:11]=[core,core+15,10,1,-1]
                    words[13]=10;words[16]=2
                data=bytearray(struct.pack('<'+'i'*len(words),*words))
                struct.pack_into('<q',data,16 if is_k else 40,micros)
                if is_k:struct.pack_into('<d',data,44,1000.)
                (directory/f'{micros/1e6:.6f}.bin').write_bytes(data)
        self.assertEqual(len(leo.check_routes(store,.3)),64)
        p=kpath/'0.100001.bin'; data=bytearray(p.read_bytes());struct.pack_into('<i',data,36,0);p.write_bytes(data)
        with self.assertRaisesRegex(ValueError,'Wrong edge-disjoint'):leo.check_routes(store,.3)

    def test_compact_heatmaps_raw_rtt_scale_and_baseline_reuse(self):
        ping_fixture()
        configs=[next(c for c in self.matrix if c['protocol']==name and c['k']==k and c['run']==1)
                 for name,k in [('Alpha',2),('OrbSingle',1),('Alpha',1)]]
        for c in configs:
            traffic_fixture(c)
            rows=analysis.extract(c)
            self.assertEqual(len(rows),5)
            self.assertAlmostEqual(rows[0]['goodput_mbps'],12)
            self.assertAlmostEqual(rows[0]['raw_rtt_ms'],35 if c['protocol']=='OrbSingle' else 35+5*c['k'])
            self.assertAlmostEqual(rows[0]['excess_rtt_ms'],10)
            self.assertEqual(rows[0]['baseline_match_pct'],100)
            self.assertEqual(rows[0]['all_k_available_pct'],100)
        import plotStyle
        checked=[]
        save=plotStyle.save_figure
        def capture(fig,output,**kwargs):
            self.assertEqual(tuple(fig.get_size_inches()),(6.6,3.1))
            self.assertIsNone(fig._supxlabel)
            image=fig.axes[0].images[0]
            self.assertEqual(image.get_array().shape,(5,6))
            self.assertEqual(len(fig.axes[0].texts),30)
            self.assertNotIn('requested',fig.axes[0].get_title())
            self.assertEqual(fig.axes[0].get_xticklabels()[0].get_text(),'Alpha')
            if output.name.startswith('delay'):
                self.assertAlmostEqual(image.norm.vmin,(25+30+40)/3)
                self.assertAlmostEqual(image.norm.vmax,45)
                self.assertAlmostEqual(float(image.get_array()[0,5]),35)
                self.assertEqual(fig.axes[-1].get_ylabel(),'RTT (ms)')
            else:
                self.assertEqual((image.norm.vmin,image.norm.vmax),(0,100))
            checked.append(output.name)
            save(fig,output,**kwargs)
        with patch.object(plotStyle,'save_figure',side_effect=capture):
            cells=analysis.plot(configs)
        self.assertEqual(len(checked),4)
        a=next(r for r in cells if r['protocol']=='Alpha' and r['k']==2)
        self.assertAlmostEqual(a['raw_rtt_ms'],45)
        self.assertAlmostEqual(a['colour_excess_ms'],10)
        for name in ('goodput_K1.png','goodput_K2.png','goodput_K2.pdf','delay_K1.png','delay_K2.png','delay_K2.pdf'):
            self.assertGreater((leo.PLOT_DIR/name).stat().st_size,1000)
        data=analysis.checked_summary(leo.ping_config(15))
        # Coverage/excess diagnostics remain available independently of colours.
        for p in leo.PAIRS:data['baseline'][f'{p["source"]}:2'].pop('11')
        leo.summary_path(leo.ping_config(15)).write_text(json.dumps(data))
        analysis.extract(configs[0])
        cell=next(r for r in analysis.aggregate(analysis.load_rows([configs[0]]),[2]) if r['protocol']=='Alpha')
        self.assertAlmostEqual(cell['raw_rtt_ms'],45)
        self.assertLess(cell['baseline_match_pct'],95)
        self.assertTrue(math.isnan(cell['colour_excess_ms']))

    def test_vector_coverage_and_sender_identity_fail_closed(self):
        ping_fixture()
        c=next(c for c in self.matrix if c['protocol']=='Alpha' and c['k']==2)
        traffic_fixture(c)
        vectors=Vectors(leo.result(c,'.vec'))
        ranked=analysis.sender_ranks(vectors,5,2,False)
        self.assertTrue(ranked[1].module.endswith('conn-101'))
        with self.assertRaises(ValueError):analysis.sender_ranks(vectors,5,3,False)
        with self.assertRaises(ValueError):interval_mean(iter([(10,1),(12,1)]),10,14,rate=True)

    def test_missing_goodput_intervals_continue_and_report_coverage(self):
        ping_fixture()
        c=next(c for c in self.matrix if c['protocol']=='Alpha' and c['k']==2)
        traffic_fixture(c)
        vectors=Vectors(leo.result(c,'.vec'))
        specifications=[]
        for vector in vectors.items.values():
            samples=list(vectors.samples(vector))
            if vector.metric=='goodput':
                if '.userTerminal[10].' in vector.module:
                    # The first sample's preceding boundary is unknown. Integrate
                    # only 11–12 at 4 Mbps and 12–13 at 8 Mbps: 12 Mb / 4 s.
                    samples=[(11,90e6),(12,4e6),(13,8e6)]
                elif '.userTerminal[11].' in vector.module:
                    samples=[(t,0) for t in range(9,15)]
                elif '.userTerminal[13].' in vector.module:
                    samples=[]
                elif '.userTerminal[14].' in vector.module:
                    continue
            specifications.append((vector.module,vector.metric,samples))
        # An unused empty thread must not reject the other thread's full data.
        specifications.append(('leo.userTerminal[12].app[0].thread_100','goodput',[]))
        fixture(c,specifications)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            rows=analysis.extract(c)
        self.assertEqual([r['goodput_observed_pct'] for r in rows],[50,100,100,0,0])
        self.assertEqual([r['goodput_complete'] for r in rows],[False,True,True,False,False])
        self.assertEqual([r['goodput_mbps'] for r in rows[:4]],[3,0,12,0])
        self.assertTrue(math.isnan(rows[4]['goodput_mbps']))
        self.assertIn('incomplete goodput coverage',output.getvalue())
        detail=json.loads(leo.summary_path(c).read_text())['goodput_recordings']
        self.assertEqual(detail[0]['recorded_bits'],12e6)
        self.assertEqual((detail[0]['covered_start_s'],detail[0]['covered_end_s']),(11,13))
        self.assertTrue(any(r['status']=='not_recorded' for r in detail))
        self.assertTrue(leo.summary_path(c).with_name(c['config']+'_goodput_coverage.csv').is_file())
        cells=analysis.aggregate(rows,[2])
        self.assertFalse(next(r for r in cells if r['protocol']=='Alpha' and r['pair']==leo.PAIRS[0]['pair'])['goodput_complete'])
        self.assertEqual(analysis.coverage_seconds([(10,12),(11,14),(10.5,11)]),4)

    def test_fewer_paths_keep_requested_k_without_zero_rtt_substitution(self):
        ping_fixture()
        c=next(c for c in self.matrix if c['protocol']=='Alpha' and c['k']==5)
        traffic_fixture(c)
        vectors=Vectors(leo.result(c,'.vec'))
        specifications=[]
        for vector in vectors.items.values():
            unavailable=vector.module.endswith(('conn-104','conn-105'))
            specifications.append((vector.module,vector.metric,[] if unavailable else list(vectors.samples(vector))))
        fixture(c,specifications)
        rows=analysis.extract(c)
        self.assertEqual(c['k'],5)
        self.assertEqual(rows[0]['all_k_available_pct'],0)
        self.assertAlmostEqual(rows[0]['raw_rtt_ms'],50)
        details=json.loads(leo.summary_path(c).read_text())['subflows']
        self.assertTrue(all(r['observed_seconds']==0 for r in details if r['rank']>=4))

    def test_runner_dry_run_never_launches_simulations(self):
        import runExperimentLeo as runner
        with patch.object(runner,'simulate',side_effect=AssertionError('must not simulate')):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                runner.main(['--dry-run','--skip-generate','--sim-time','15','--runs','1','--ks','2','--protocols','Alpha','OrbSingle'])
        self.assertIn('2 traffic runs',out.getvalue())
        self.assertIn('one unloaded ping',out.getvalue())

    def test_individual_sampling_and_endpoint_identity(self):
        import plotExperimentLeo as individual
        samples=((i/10000,1e6 if i==54321 else float(i%7)) for i in range(100000))
        points,count=individual.sample_series(samples,10,bins=8)
        self.assertEqual(count,100000)
        self.assertLessEqual(len(points),32)
        self.assertIn((5.4321,1e6),points)
        self.assertEqual(points[0],(0,0))
        self.assertEqual(points[-1][0],9.9999)
        c=next(c for c in self.matrix if c['protocol']=='Alpha' and c['k']==2)
        diagnostic_fixture(c)
        vectors=Vectors(leo.result(c,'.vec'))
        cwnd=individual.pair_vectors(vectors,c,leo.PAIRS[0],'cwnd')
        self.assertEqual([rank for _,_,rank in cwnd],[1,2])
        self.assertTrue(all('conn-100' not in v.module for v,_,_ in cwnd))
        receiver=individual.pair_vectors(vectors,c,leo.PAIRS[0],'throughput')
        self.assertTrue(all(rank is None and 'Path ' not in label for _,label,rank in receiver))
        counter=individual.pair_vectors(vectors,c,leo.PAIRS[0],'numRtos')[0][0]
        points,count,status=individual.prepare_curve(vectors,counter,'numRtos',15)
        self.assertEqual((points,count,status),([(0.,0.),(15,0.)],0,'zero_events'))
        rtt=individual.pair_vectors(vectors,c,leo.PAIRS[0],'rtt')[0][0]
        points,_,_=individual.prepare_curve(vectors,rtt,'rtt',15)
        self.assertEqual(points[-1][0],13.2)
        rate=receiver[0][0]
        points,_,_=individual.prepare_curve(vectors,rate,'throughput',15)
        self.assertEqual(points[-1][0],14)

    def test_individual_plot_files_missing_data_and_applicability(self):
        import plotExperimentLeo as individual
        c=next(c for c in self.matrix if c['protocol']=='Alpha' and c['k']==2)
        diagnostic_fixture(c)
        with patch.object(leo,'PAIRS',leo.PAIRS[:1]):
            export_fixture(c,bins=20)
            records=individual.plot_metrics([c],['cwnd','rtt','throughput','numRtos','metaReinjectedBytes'],cores=1)
        out=leo.PLOT_DIR/'individual'/leo.run_subdir(c)
        for metric in ('cwnd','rtt','throughput','numRtos','metaReinjectedBytes'):
            terminal=individual.terminal_folder(leo.PAIRS[0],metric=='throughput')
            for ext in ('png','pdf'):
                self.assertGreater((out/terminal/f'{metric}.{ext}').stat().st_size,1000)
        for receiver in (False,True):
            self.assertGreater((out/individual.terminal_folder(leo.PAIRS[0],receiver)/'combined.pdf').stat().st_size,1000)
        self.assertTrue(all('userTerminal[' in r['module'] for r in records))
        self.assertTrue(any(r['metric']=='metaReinjectedBytes' and r['status']=='zero_events' for r in records))
        self.assertTrue((out/'vectors.csv').is_file())
        self.assertEqual(json.loads((out/'plots.json').read_text())['figures'],5)
        self.assertEqual(json.loads((out/'plots.json').read_text())['combined_pdfs'],3)
        self.assertEqual(json.loads((out/'plots.json').read_text())['comparison_figures'],5)
        self.assertTrue((out/'aggregate'/'cwnd.pdf').is_file())
        self.assertTrue((out/'aggregate'/'combined.pdf').is_file())
        self.assertEqual({r['terminal'] for r in records},{5,10})
        # Older results must report absent diagnostics, not invent zero traces.
        traffic_fixture(c)
        export_fixture(c)
        records=individual.plot_metrics([c],['cwnd'],cores=1)
        self.assertEqual({r['status'] for r in records},{'not_recorded'})
        self.assertFalse((out/individual.terminal_folder(leo.PAIRS[0])/'cwnd.png').exists())
        self.assertFalse((out/individual.terminal_folder(leo.PAIRS[0])/'cwnd.pdf').exists())
        self.assertFalse((out/individual.terminal_folder(leo.PAIRS[0])/'combined.pdf').exists())
        self.assertFalse(individual.applicable(dict(protocol='CubicUncoupled'),'U'))
        self.assertFalse(individual.applicable(dict(protocol='OrbSingle'),'holBlockedBytes'))
        self.assertFalse(individual.applicable(dict(protocol='OrbSingle'),'metaReinjectedBytes'))

    def test_plot_only_runner_avoids_simulation_and_heatmap_extraction(self):
        import runExperimentLeo as runner
        import plotExperimentLeo as individual
        with patch.object(runner,'simulate',side_effect=AssertionError('must not simulate')), \
             patch.object(runner,'prepare_ping',side_effect=AssertionError('no ping dependency')), \
             patch.object(runner,'ensure_index',side_effect=AssertionError('plotting must use CSVs')) as indexed, \
             patch.object(analysis,'plot',side_effect=AssertionError('no heatmap selection')), \
             patch.object(individual,'plot_metrics') as plotted, \
             contextlib.redirect_stdout(io.StringIO()):
            runner.main(['--start-step','4','--skip-generate','--cores','3',
                '--plots','cwnd','U','--configs','Delta_EdgeDisjoint_K3_Run1'])
        indexed.assert_not_called()
        args,kwargs=plotted.call_args
        self.assertEqual([c['config'] for c in args[0]],['Delta_EdgeDisjoint_K3_Run1'])
        self.assertEqual(args[1],['cwnd','U'])
        self.assertEqual(kwargs['cores'],3)

    def test_parallel_individual_rendering_uses_explicit_result_paths(self):
        import plotExperimentLeo as individual
        configs=[next(c for c in self.matrix if c['protocol']==protocol and c['k']==1 and c['run']==1)
                 for protocol in ('Alpha','OrbSingle')]
        for c in configs:
            diagnostic_fixture(c)
            export_fixture(c,bins=10)
        records=individual.plot_metrics(configs,['cwnd'],cores=2)
        self.assertEqual(len({r['config'] for r in records}),2)
        for c in configs:
            self.assertTrue((leo.PLOT_DIR/'individual'/leo.run_subdir(c)/individual.terminal_folder(leo.PAIRS[0])/'cwnd.png').is_file())

    def test_individual_figures_have_one_terminal_and_combined_pdf(self):
        import plotExperimentLeo as individual
        import matplotlib.pyplot  # Import before replacing Figure.savefig.
        c=next(c for c in self.matrix if c['protocol']=='Alpha' and c['k']==2)
        diagnostic_fixture(c)
        vectors=Vectors(leo.result(c,'.vec'))
        specifications=[]
        for vector in vectors.items.values():
            samples=list(vectors.samples(vector))
            if '.userTerminal[9].' in vector.module and vector.metric=='cwnd':
                samples=[(t,v*100) for t,v in samples]
            specifications.append((vector.module,vector.metric,samples))
        fixture(c,specifications)
        checked=[]
        export_fixture(c,bins=20)
        def capture(fig,output):
            self.assertEqual(len(fig.axes),1)
            if output.name=='cwnd':
                if output.parent.name=='aggregate':
                    self.assertGreaterEqual(fig.axes[0].get_ylim()[1],600)
                    labels=fig.axes[0].get_legend_handles_labels()[1]
                    self.assertEqual(len(labels),10)
                    self.assertTrue(all(any(f'UT{terminal}' in label for label in labels) for terminal in range(5,10)))
                    colors={terminal:{line.get_color() for line in fig.axes[0].lines
                                      if f'UT{terminal}' in line.get_label()} for terminal in range(5,10)}
                    self.assertTrue(all(len(value)==1 for value in colors.values()))
                    self.assertEqual(len(set().union(*colors.values())),5)
                elif output.parent.name.startswith('userTerminal9_'):
                    self.assertGreaterEqual(fig.axes[0].get_ylim()[1],600)
                else:
                    self.assertLess(fig.axes[0].get_ylim()[1],10)
            else:
                self.assertGreaterEqual(fig.axes[0].get_ylim()[1],1)
                self.assertTrue(all(t==int(t) for t in fig.axes[0].get_yticks()))
            checked.append(output.name)
        with patch.object(individual,'save_figure',side_effect=capture), \
             patch('matplotlib.figure.Figure.savefig') as combined:
            individual.plot_metrics([c],['cwnd','numRtos'],cores=1)
        self.assertEqual(checked,['cwnd','numRtos']*6)
        self.assertEqual(combined.call_count,6)
        self.assertTrue(all(call.args[0].name=='combined.pdf' for call in combined.call_args_list))

    def test_csv_export_is_lossless_endpoint_only_and_overwrites(self):
        from terminalVectors import CsvVectors
        c=next(c for c in self.matrix if c['protocol']=='Alpha' and c['k']==2)
        diagnostic_fixture(c)
        native=Vectors(leo.result(c,'.vec'))
        specifications=[]
        dense=[(i/5000,1e6 if i==23456 else float(i%7)) for i in range(50000)]+[(16,999)]
        target='leo.userTerminal[5].tcp.conn-101'
        for vector in native.items.values():
            samples=dense if vector.module==target and vector.metric=='cwnd' else list(native.samples(vector))
            specifications.append((vector.module,vector.metric,samples))
        fixture(c,specifications)
        manifest=export_fixture(c,bins=8)
        self.assertTrue(all('.userTerminal[' in r['module'] for r in manifest['vectors']))
        exported=CsvVectors(leo.vector_dir(c),c)
        vector=next(v for v in exported.items.values() if v.module==target and v.metric=='cwnd')
        self.assertEqual(list(exported.samples(vector)),dense)
        points,count,status=exported.curve(vector)
        self.assertLessEqual(len(points),4*8+1)
        self.assertIn(dense[23456],points)
        self.assertEqual((count,status),(50001,'recorded'))
        self.assertNotIn((16,999),points)
        meta_rtt=next(v for v in exported.items.values() if v.module.endswith('conn-100') and v.metric=='rtt')
        self.assertEqual(list(exported.samples(meta_rtt)),[])
        counter=next(v for v in exported.items.values() if v.metric=='numRtos')
        self.assertEqual(exported.curve(counter),([(0.,0.),(15.,0.)],0,'zero_events'))
        row=exported.metadata[vector.id]
        with (exported.root/row['csv']).open(newline='') as handle:
            self.assertEqual(next(csv.reader(handle)),['time','cwnd'])
        old_csv=exported.root/row['csv']
        stray=exported.root/'obsolete.csv'
        stray.write_text('old data\n')
        traffic_fixture(c)
        replacement=export_fixture(c)
        self.assertFalse(old_csv.exists())
        self.assertFalse(stray.exists())
        self.assertEqual(len(list(exported.root.rglob('*.csv'))),2*len(replacement['vectors']))
        self.assertFalse(list(exported.root.rglob('*.tmp')))

    def test_plotting_uses_csvs_without_native_vectors_or_downsampling(self):
        import plotExperimentLeo as individual
        import terminalVectors
        c=next(c for c in self.matrix if c['protocol']=='Alpha' and c['k']==2)
        ping_fixture()
        diagnostic_fixture(c)
        with patch.object(leo,'PAIRS',leo.PAIRS[:1]):
            export_fixture(c)
            analysis.extract(c,vectors=terminalVectors.CsvVectors(leo.vector_dir(c),c))
            for config in (c,leo.ping_config(15)):
                for suffix in ('.vec','.vci','.sca'):
                    leo.result(config,suffix).unlink()
            legacy=leo.PLOT_DIR/'individual'/c['config']
            legacy.mkdir(parents=True)
            (legacy/'cwnd.pdf').write_text('superseded flat output')
            with patch.object(Vectors,'samples',side_effect=AssertionError('must not read native vectors')), \
                 patch.object(terminalVectors,'sample_series',side_effect=AssertionError('must not downsample again')):
                self.assertEqual(analysis.load_rows([c])[0]['goodput_mbps'],12)
                records=individual.plot_metrics([c],['cwnd'],cores=1)
            root=leo.PLOT_DIR/'individual'/'Alpha'/'K2'/'run1'
            self.assertTrue((root/'userTerminal5_Newark'/'cwnd.pdf').is_file())
            self.assertTrue((root/'aggregate'/'cwnd.pdf').is_file())
            self.assertTrue((root/'aggregate'/'combined.pdf').is_file())
            self.assertFalse(legacy.exists())
            self.assertEqual({r['rank'] for r in records},{1,2})

    def test_standalone_plot_dry_run_needs_no_plotting_packages(self):
        script=leo.SCRIPT_DIR/'plotExperimentLeo.py'
        code=f'''
import importlib.abc,sys,runpy
sys.path.insert(0,{str(leo.SCRIPT_DIR)!r})
class BlockPlotting(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] in ('numpy','matplotlib'):
            raise AssertionError('Dry run imported plotting package: '+fullname)
sys.meta_path.insert(0,BlockPlotting())
import leo
from pathlib import Path
leo.EXPERIMENT_DIR=Path({str(leo.EXPERIMENT_DIR)!r})
leo.PING_DIR=Path({str(leo.PING_DIR)!r})
sys.argv=[{str(script)!r},'--dry-run','--configs','Delta_EdgeDisjoint_K3_Run1','--plots','cwnd','U']
runpy.run_path({str(script)!r},run_name='__main__')
'''
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('1 traffic runs',result.stdout)
        self.assertNotIn('Step 1:',result.stdout)
        self.assertNotIn('Step 2:',result.stdout)
        self.assertNotIn('Step 3:',result.stdout)
        self.assertIn('Step 4: plot cwnd, U',result.stdout)


if __name__=='__main__':
    unittest.main()
