"""Route sharing, interval boundaries, storage reuse and process-worker checks."""
import contextlib
import csv
import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'simulations/pythonScripts/experimentLeo'))
import exportLinkSharing as reports
import leo
import runExperimentLeo as runner


def config(protocol='Alpha', k=2):
    return dict(config=f'{protocol}_EdgeDisjoint_K{k}_Run1', protocol=protocol, k=k,
                run=1, starts=[.5, 1.5], sim_time=3)


def frame(start, end, pairs):
    routes = []
    for index, pair in enumerate(pairs):
        source, destination = 1700+pair['source'], 1700+pair['destination']
        # Connections travel in opposite directions on 0--1. Rank 2 has a
        # separate core route but shares its own terminal's access hops.
        core = [0, 1] if index % 2 == 0 else [1, 0]
        routes.append(dict(source=pair['source'], destination=pair['destination'],
            paths=[[source, *core, destination], [source, core[0], 10+index, core[1], destination]],
            primary=[source, 2, 3, destination], primary_status='available'))
    return dict(start_us=start, end_us=end, core_nodes=1700, routes=routes)


def write_frames(path, frames):
    with gzip.open(path, 'wt') as output:
        for item in frames:
            output.write(json.dumps(item)+'\n')


class LinkSharingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='leo-sharing-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.pairs = list(leo.PAIRS[:2])
        self.frames = self.root/'frames.gz'
        self.output = self.root/'csvs/link_sharing'
        write_frames(self.frames, [frame(i*1000000, (i+1)*1000000, self.pairs) for i in range(3)])

    def export(self, conf=None, fingerprint='fixture'):
        conf = conf or config()
        name = reports.export_profile((self.frames, fingerprint, conf, self.pairs, self.output))
        with (self.output/(name+'.csv')).open() as source:
            rows = list(csv.DictReader(source))
        metadata = json.loads((self.output/(name+'.json')).read_text())
        return rows, metadata

    def test_start_boundaries_directions_shared_access_and_coalescing(self):
        rows, metadata = self.export()
        shared_core = [r for r in rows if (r['node_a_id'], r['node_b_id']) == ('0', '1')]
        self.assertEqual(len(shared_core), 1)
        row = shared_core[0]
        self.assertEqual((row['start_s'], row['end_s']), ('1.500000', '3.000000'))
        self.assertEqual((row['a_to_b_count'], row['b_to_a_count']), ('1', '1'))
        self.assertEqual(row['connections'], 'C1;C2')
        self.assertEqual(row['subflows_a_to_b'], 'C1:SF1')
        self.assertEqual(row['subflows_b_to_a'], 'C2:SF1')
        accesses = [r for r in rows if r['link_type']=='access']
        self.assertEqual(len(accesses), 4)
        self.assertTrue(all(r['connection_count']=='1' and r['subflow_count']=='2' for r in accesses))
        self.assertTrue(any(r['node_b']=='userTerminal[5]' for r in accesses))
        self.assertEqual(metadata['saved_route_intervals'], 3)
        self.assertEqual(metadata['evaluated_intervals'], 5)
        self.assertEqual(metadata['no_shared_links'], [dict(start_s=0., end_s=.5)])
        self.assertEqual(metadata['active_connections'], [dict(start_s=0.,end_s=.5,members=[]),
            dict(start_s=.5,end_s=1.5,members=['C1']), dict(start_s=1.5,end_s=3.,members=['C1','C2'])])

    def test_unavailable_ranks_are_explicit_and_never_duplicated(self):
        frames = [frame(i*1000000, (i+1)*1000000, self.pairs) for i in range(3)]
        for item in frames[1:]:
            item['routes'][0]['paths'] = item['routes'][0]['paths'][:1]
        write_frames(self.frames, frames)
        rows, metadata = self.export()
        self.assertEqual(metadata['unavailable_subflows'], [dict(start_s=1., end_s=3.,
            members=[dict(subflow='C1:SF2', reason='rank_unavailable')])])
        for row in rows:
            if float(row['start_s']) >= 1:
                self.assertNotIn('C1:SF2', row['subflows_a_to_b']+row['subflows_b_to_a'])

    def test_single_uses_primary_routes_and_unreachable_status(self):
        conf = config('OrbSingle', 1)
        rows, _ = self.export(conf)
        self.assertTrue(any(r['node_a_id']=='2' and r['node_b_id']=='3' for r in rows))
        self.assertFalse(any(r['node_a_id']=='0' and r['node_b_id']=='1' for r in rows))
        item = frame(0,3000000,self.pairs)
        item['routes'][1].update(primary=None, primary_status='unreachable')
        write_frames(self.frames, [item])
        _, metadata = self.export(conf, 'changed')
        self.assertEqual(metadata['unavailable_subflows'][0]['members'],
                         [dict(subflow='C2:SF1', reason='unreachable')])

    def test_cache_reuse_overwrite_and_incomplete_input_preserve_previous_csv(self):
        rows, metadata = self.export()
        path = self.output/(metadata['profile']+'.csv')
        stat = path.stat().st_mtime_ns
        self.export()
        self.assertEqual(path.stat().st_mtime_ns, stat)
        self.export(fingerprint='changed')
        with path.open() as source:
            self.assertEqual(list(csv.DictReader(source)), rows)
        previous = path.read_bytes()
        write_frames(self.frames, [frame(0,1000000,self.pairs),frame(2000000,3000000,self.pairs)])
        with self.assertRaisesRegex(ValueError, 'complete simulation interval'):
            self.export(fingerprint='bad')
        self.assertEqual(path.read_bytes(), previous)
        self.assertFalse(list(self.output.glob('*.tmp')))

    def test_process_pool_and_per_config_storage_reuse(self):
        configs = [config('Alpha', 1), config('Delta', 1), config('Alpha', 2)]
        for c in configs:
            c['starts'] = [.5]*5
        write_frames(self.frames, [frame(0,3000000,list(leo.PAIRS))])
        with patch.object(leo, 'EXPERIMENT_DIR', self.root), \
             patch.object(reports, 'prepare_frames', return_value=(self.frames,'fixture')), \
             contextlib.redirect_stdout(io.StringIO()):
            reports.export_reports(configs, self.root/'unused', cores=2)
        csv_root = self.root/'csvs'
        first = csv_root/(configs[0]['config']+'_link_sharing.csv')
        second = csv_root/(configs[1]['config']+'_link_sharing.csv')
        self.assertEqual(first.stat().st_ino, second.stat().st_ino)
        self.assertEqual(len(list((csv_root/'link_sharing').glob('*.csv'))),2)
        metadata = json.loads(first.with_suffix('.json').read_text())
        self.assertEqual(metadata['connections']['C1']['pair'],'NewarkToBristol')
        self.assertEqual(metadata['sender_mapping_status'], 'not_extracted')

    def test_sender_module_mapping_uses_metadata_only(self):
        conf = config()
        conf['starts'] = [.5]*5
        vectors = []
        for pair in leo.PAIRS:
            for connection_id in (102,100,101):
                vectors.append(dict(id=len(vectors),metric='rtt',
                    module=f'leo.userTerminal[{pair["source"]}].tcp.conn-{connection_id}'))
        with patch.object(leo, 'EXPERIMENT_DIR', self.root):
            directory = leo.vector_dir(conf)
            directory.mkdir(parents=True)
            (directory/'vectors.json').write_text(json.dumps(dict(version=1,config=conf,vectors=vectors)))
            mapping, status = reports.sender_mapping(conf,list(leo.PAIRS))
        self.assertEqual(status,'mapped_from_endpoint_vector_metadata')
        self.assertEqual(mapping['C1:SF1'],'leo.userTerminal[5].tcp.conn-101')
        self.assertEqual(mapping['C1:SF2'],'leo.userTerminal[5].tcp.conn-102')

    def test_dry_run_and_runner_integration_do_not_launch(self):
        with patch.object(reports, 'export_reports',side_effect=AssertionError('must not export')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            reports.main(['--dry-run','--cores','30'])
        self.assertIn('130 configuration reports; 30 unique routing profiles',output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            runner.main(['--dry-run','--skip-generate','--start-step','3','--end-step','3'])
        self.assertIn('export saved-route link-sharing reports',output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            runner.main(['--dry-run','--skip-generate','--start-step','3','--end-step','3','--skip-link-sharing'])
        self.assertNotIn('export saved-route link-sharing reports',output.getvalue())

    def test_native_cache_pipeline_reuses_primary_superset_and_rejects_partial_output(self):
        if not all(shutil.which(tool) for tool in ('make','c++','pkg-config')) or \
                subprocess.run(['pkg-config','--exists','igraph']).returncode:
            self.skipTest('Native route reader requires a compiler and igraph development files')
        from test_export_leo_route_frames import ExportLeoRouteFrameTests
        fixture = ExportLeoRouteFrameTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.corpus([{(0,3):1,(1,3):3}, {(0,3):2,(2,3):3}], independent_bases=True)
        duration = fixture.duration/1e6
        with patch.object(leo,'route_directories',return_value=(fixture.primary_directory,fixture.k_directory)), \
             contextlib.redirect_stdout(io.StringIO()):
            path, fingerprint = reports.prepare_frames(self.root,duration,True,self.output)
            original = path.read_bytes()
            with gzip.open(path,'rt') as source:
                decoded = [json.loads(line) for line in source]
            self.assertEqual(decoded[0]['routes'][0]['primary'],[9,0,1,3,14])
            with patch.object(reports.subprocess,'Popen',side_effect=AssertionError('must reuse cache')):
                reused, same_fingerprint = reports.prepare_frames(self.root,duration,False,self.output)
            self.assertEqual((reused,same_fingerprint),(path,fingerprint))
            # Fail after the first valid frame; the old complete cache survives.
            damaged = fixture.k_directory/'0.100001.bin'
            payload = bytearray(damaged.read_bytes())
            payload[-1] ^= 1
            damaged.write_bytes(payload)
            with self.assertRaisesRegex(RuntimeError,'no incomplete cache was published'):
                reports.prepare_frames(self.root,duration,True,self.output)
        self.assertEqual(path.read_bytes(),original)
        self.assertFalse(list(self.output.glob('*.tmp')))


if __name__ == '__main__':
    unittest.main()
