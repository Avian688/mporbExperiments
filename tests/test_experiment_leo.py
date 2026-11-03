"""Generator, saved-route preflight, and synthetic extraction/plot regression checks."""
import contextlib
import io
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
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


def ping_fixture(duration=15):
    specifications=[]
    for p in leo.PAIRS:
        for rank in range(11):
            module=f'leo.userTerminal[{p["source"]}].app[{rank}]'
            base=.025 if rank==0 else .02+rank*.01
            specifications.append((module,'rtt',[(t+.1,base) for t in range(int(duration))]))
            if rank:
                specifications.append((module,'kPathAvailable',[(.000002,1 if rank<=3 else 0)]))
    c=leo.ping_config(duration)
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

    def test_matched_rtt_raw_labels_excess_colours_and_baseline_reuse(self):
        ping_fixture()
        configs=[next(c for c in self.matrix if c['protocol']==name and c['k']==k and c['run']==1)
                 for name,k in [('Alpha',2),('OrbSingle',1)]]
        for c in configs:
            traffic_fixture(c)
            rows=analysis.extract(c)
            self.assertEqual(len(rows),5)
            self.assertAlmostEqual(rows[0]['goodput_mbps'],12)
            self.assertAlmostEqual(rows[0]['raw_rtt_ms'],35 if c['protocol']=='OrbSingle' else 45)
            self.assertAlmostEqual(rows[0]['excess_rtt_ms'],10)
            self.assertEqual(rows[0]['baseline_match_pct'],100)
            self.assertEqual(rows[0]['all_k_available_pct'],100)
        cells=analysis.plot(configs)
        a=next(r for r in cells if r['protocol']=='Alpha')
        self.assertAlmostEqual(a['raw_rtt_ms'],45)
        self.assertAlmostEqual(a['colour_excess_ms'],10)
        for name in ('goodput_K2.png','goodput_K2.pdf','delay_K2.png','delay_K2.pdf'):
            self.assertGreater((leo.PLOT_DIR/name).stat().st_size,1000)
        data=analysis.checked_summary(leo.ping_config(15))
        # Missing baseline bins must not silently make the delay cell green.
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


if __name__=='__main__':
    unittest.main()
