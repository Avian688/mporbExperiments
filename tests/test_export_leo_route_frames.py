"""Small saved-route corpora exercise the standalone native exporter, without simulation."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
MASK = (1 << 64) - 1
CORE_NODES = 4


def splitmix64(value):
    value = (value + 0x9e3779b97f4a7c15) & MASK
    value = ((value ^ (value >> 30)) * 0xbf58476d1ce4e5b9) & MASK
    value = ((value ^ (value >> 27)) * 0x94d049bb133111eb) & MASK
    return value ^ (value >> 31)


def state_hash(routes):
    result = 0
    for (source, destination), next_hop in routes.items():
        value = (splitmix64(source) ^ splitmix64(destination + 0x100000001b3) ^
                 splitmix64(next_hop + 0x517cc1b727220a95))
        result ^= splitmix64(value)
    return result


def payload_hash(payload):
    result = 1469598103934665603
    for byte in payload:
        result = ((result ^ byte) * 1099511628211) & MASK
    return result


def primary_bytes(sequence, timestamp, previous, current, base_hash, *, base=False):
    if base:
        sequence, previous, base_hash = 0, {}, state_hash(current)
    records = [(source, destination, hop)
               for (source, destination), hop in sorted(current.items())
               if previous.get((source, destination)) != hop]
    records += [(source, destination, -1) for source, destination in sorted(previous)
                if (source, destination) not in current]
    payload = b''.join(struct.pack('<iii', *record) for record in records)
    header = struct.pack('<10i', 0x4c454f33, 3, 24, 1 if base else 2,
                         sequence, 0, sequence - 1, CORE_NODES, CORE_NODES, len(records))
    values = (timestamp, len(previous), len(current), state_hash(previous),
              state_hash(current), base_hash, payload_hash(payload))
    return header + struct.pack('<7Q', *values) + payload


def k_bytes(sequence, timestamp, route_hash, *, attachments=None):
    payload = bytearray()
    path_count = node_count = 0
    for source in range(5, 10):
        source_node, destination_node = CORE_NODES + source, CORE_NODES + source + 5
        source_core, destination_core = (attachments or {}).get(source, (0, 3))
        if source_core < 0 or destination_core < 0:
            core_path = None
        elif source_core == destination_core:
            core_path = [source_core]
        elif source_core == 0 and destination_core == 3:
            core_path = [0, 2, 3]
        else:
            core_path = [source_core, destination_core]
        payload.extend(struct.pack('<6i', source_node, destination_node, source_core,
                                   destination_core, int(core_path is not None), 0))
        if core_path is not None:
            nodes = [source_node, *core_path, destination_node]
            payload.extend(struct.pack('<iddd', len(nodes), 2, 3, 6))
            payload.extend(struct.pack('<' + 'i' * len(nodes), *nodes))
            path_count += 1
            node_count += len(nodes)
    header = struct.pack('<4iQ5id4i3Q',
        0x4c454f4b, 3, 23, sequence, timestamp,
        CORE_NODES, CORE_NODES + 15, 10, 1, -1, 1000,
        5, path_count, node_count, 2, route_hash, 0, payload_hash(payload))
    return header + payload


class ExportLeoRouteFrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        supplied = os.environ.get('LEO_ROUTE_FRAME_TOOL')
        if supplied:
            cls.binary = Path(supplied).resolve()
            if not cls.binary.is_file():
                raise RuntimeError(f'LEO_ROUTE_FRAME_TOOL does not exist: {cls.binary}')
            return
        compiler = shutil.which('c++') or shutil.which('clang++') or shutil.which('g++')
        if not compiler or not shutil.which('make') or not shutil.which('pkg-config'):
            raise unittest.SkipTest('A C++ compiler, make and pkg-config are required')
        if subprocess.run(['pkg-config', '--exists', 'igraph']).returncode:
            raise unittest.SkipTest('igraph development files are required')
        cls.build_directory = tempfile.TemporaryDirectory(prefix='leo-route-frame-build-')
        cls.addClassCleanup(cls.build_directory.cleanup)
        cls.binary = Path(cls.build_directory.name) / 'exportLeoRouteFrames'
        subprocess.run(['make', '-C', str(PROJECT / 'tools'), f'TARGET={cls.binary}',
            f'CXX={compiler}', 'CXXFLAGS=-O0 -std=c++17 -Wall -Wextra -Wpedantic'],
            check=True, capture_output=True, text=True)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='leo-route-frame-fixture-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.k_directory, self.primary_directory = self.root / 'k', self.root / 'primary'
        self.k_directory.mkdir()
        self.primary_directory.mkdir()

    def corpus(self, states, *, independent_bases=False, attachments=None):
        self.timestamps = [0, *(100001 + index * 100000 for index in range(len(states) - 1))]
        self.duration = self.timestamps[-1] + 1
        for index, (timestamp, state) in enumerate(zip(self.timestamps, states)):
            name = f'{timestamp // 1000000}.{timestamp % 1000000:06d}.bin'
            (self.k_directory / name).write_bytes(k_bytes(index, timestamp, state_hash(state),
                attachments=(attachments or {}).get(index)))
            (self.primary_directory / name).write_bytes(primary_bytes(index, timestamp,
                states[index - 1] if index else {}, state, state_hash(states[0]),
                base=independent_bases or index == 0))

    def run_exporter(self, *, primary=True):
        command = [str(self.binary), '--k-dir', str(self.k_directory),
                   '--duration-us', str(self.duration)]
        if primary:
            command += ['--primary-dir', str(self.primary_directory)]
        return subprocess.run(command, capture_output=True, text=True)

    def frames(self, **kwargs):
        result = self.run_exporter(**kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()], result.stderr

    def test_independent_primary_bases_use_native_route_not_k_rank_one(self):
        self.corpus([{(0, 3): 1, (1, 3): 3}, {(0, 3): 2, (2, 3): 3}],
                    independent_bases=True)
        frames, _ = self.frames()
        self.assertEqual([(frame['start_us'], frame['end_us']) for frame in frames],
                         [(0, 100001), (100001, 100002)])
        self.assertEqual(frames[0]['routes'][0]['primary'], [9, 0, 1, 3, 14])
        self.assertEqual(frames[0]['routes'][0]['paths'], [[9, 0, 2, 3, 14]])
        self.assertEqual(frames[1]['routes'][0]['primary'], [9, 0, 2, 3, 14])
        self.assertEqual([route['source'] for route in frames[1]['routes']], list(range(5, 10)))

    def test_delta_replay_handles_delete_cycles_disconnection_and_same_core(self):
        states = [{(0, 3): 1, (1, 3): 3},
                  {(0, 3): 2, (1, 3): 3, (2, 3): 3},
                  {(0, 3): 1, (1, 3): 0, (2, 3): 3},
                  {(0, 3): 1, (1, 3): 0}]
        attachments = {index: {5: (-1, 3), 7: (2, 3), 8: (3, 3)} for index in (2, 3)}
        self.corpus(states, attachments=attachments)
        frames, _ = self.frames()
        self.assertEqual(frames[1]['routes'][0]['primary'], [9, 0, 2, 3, 14])
        self.assertEqual(frames[2]['routes'][0]['primary_status'], 'disconnected')
        self.assertEqual(frames[2]['routes'][1]['primary_status'], 'unreachable')
        self.assertIsNone(frames[2]['routes'][1]['primary'])
        self.assertEqual(frames[2]['routes'][2]['primary'], [11, 2, 3, 16])
        self.assertEqual(frames[2]['routes'][3]['primary'], [12, 3, 17])
        self.assertEqual(frames[3]['routes'][2]['primary_status'], 'unreachable')

    def test_optional_primary_does_not_read_primary_files(self):
        self.corpus([{(0, 3): 1, (1, 3): 3}])
        shutil.rmtree(self.primary_directory)
        frames, _ = self.frames(primary=False)
        for route in frames[0]['routes']:
            self.assertIsNone(route['primary'])
            self.assertEqual(route['primary_status'], 'not_requested')
            self.assertEqual(len(route['paths']), 1)

    def test_native_codecs_reject_payload_corruption(self):
        self.corpus([{(0, 3): 1, (1, 3): 3}])
        for directory in (self.k_directory, self.primary_directory):
            with self.subTest(directory=directory.name):
                path = directory / '0.000000.bin'
                original = path.read_bytes()
                damaged = bytearray(original)
                damaged[-1] ^= 1
                path.write_bytes(damaged)
                result = self.run_exporter()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('checksum mismatch', result.stderr)
                self.assertEqual(result.stdout, '')
                path.write_bytes(original)

    def test_interval_gaps_and_primary_timestamp_misalignment_fail(self):
        self.corpus([{(0, 3): 1, (1, 3): 3}] * 3)
        path = self.k_directory / '0.100001.bin'
        path.rename(self.k_directory / 'saved')
        result = self.run_exporter()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('coverage gap', result.stderr)
        self.assertEqual(result.stdout, '')
        (self.k_directory / 'saved').rename(path)
        (self.primary_directory / '0.100001.bin').rename(self.primary_directory / '0.100000.bin')
        result = self.run_exporter()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('timestamps are not aligned', result.stderr)

    def test_primary_state_hash_must_match_companion_k_snapshot(self):
        self.corpus([{(0, 3): 1, (1, 3): 3}])
        path = self.k_directory / '0.000000.bin'
        damaged = bytearray(path.read_bytes())
        struct.pack_into('<Q', damaged, 68, 123)
        path.write_bytes(damaged)
        result = self.run_exporter()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('same-time primary route state', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_progress_uses_stderr_and_stdout_remains_json_lines(self):
        self.corpus([{(0, 3): 1, (1, 3): 3}] * 101, independent_bases=True)
        frames, progress = self.frames(primary=False)
        self.assertEqual(len(frames), 101)
        self.assertIn('reading 101 saved-route snapshots', progress)
        self.assertIn('decoded 100/101 snapshots', progress)
        self.assertIn('decoded 101 snapshots; complete coverage', progress)


if __name__ == '__main__':
    unittest.main()
