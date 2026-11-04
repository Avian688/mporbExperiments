import configparser
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'simulations/pythonScripts/experimentLeo'))
import viewExperimentLeo as view
import leo


class LeoViewTests(unittest.TestCase):
    def test_pair_selection_matches_experiment_groups(self):
        self.assertEqual([p['group'] for p in view.resolve_pairs(['all'])],[5,6,7,8,9])
        self.assertEqual([p['group'] for p in view.resolve_pairs(['NewarkToBristol','3','1'])],[5,7])
        self.assertEqual([p['group'] for p in view.resolve_pairs(['stamford-to-edinburgh,5'])],[8,9])
        with self.assertRaises(ValueError):view.resolve_pairs(['SanDiegoToShanghai'])
        with self.assertRaises(ValueError):view.resolve_pairs(['0'])

    def test_launch_keeps_catalog_identity_and_quotes_string_parameters(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(view,'VIEW_DIR',Path(directory)/'viewer'), \
             patch.object(view.subprocess,'call',side_effect=AssertionError('must not launch')), \
             patch.object(view,'generate_ini',side_effect=AssertionError('must not write')):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result=view.main(['--dry-run','--pairs','2','5','--k','3','--time','25.5','--core-only-overlap'])
            self.assertEqual(result,0)
            text=output.getvalue()
            self.assertIn('pathCount=10',text)
            self.assertIn('displayedK=3',text)
            self.assertIn('selectedPairIndices="6 9"',text)
            self.assertIn('snapshotTime=25.5s',text)
            self.assertIn('highlightAccessLinks=false',text)
            self.assertIn('showInterSatelliteLinks=false',text)
            self.assertIn('mporbExperiments/src/mporbExperiments',text)
            self.assertFalse((Path(directory)/'viewer').exists())

    def test_local_ini_generation_preserves_shared_geometry_without_writing_dependencies(self):
        shared_ini=leo.SAMPLES_DIR/'leosatellites/simulations/examples/routing/visualize.ini'
        before=shared_ini.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'visualize.ini'
            view.generate_ini(300,path)
            first=path.read_text()
            config=configparser.ConfigParser(interpolation=None)
            config.read(path)
            self.assertEqual(config['General']['*.numOfUserTerminals'],'15')
            self.assertEqual(config['General']['*.pathVisualizer.pathCount'],'10')
            self.assertEqual(config['General']['*.pathVisualizer.snapshotSet'],'"SharedLeoTenPairs"')
            self.assertIn('5-10,6-11,7-12,8-13,9-14',config['General']['*.pathVisualizer.endpointPairs'])
            self.assertEqual(config['Config ExperimentLeoView']['extends'],'View_EdgeDisjoint')
            self.assertEqual(config['Config ExperimentLeoView']['network'],'mporbexperiments.visualizer.ExperimentLeoOsg')
            self.assertIn('**.vector-recording = false',first)
            self.assertNotIn('TcpSessionApp',first)
            view.generate_ini(300,path)
            self.assertEqual(path.read_text(),first)
        self.assertEqual(shared_ini.read_bytes(),before)
        # Terrestrial runners load the existing simulations NED tree without
        # LeoSatellites. Viewer NED types live only in the viewer's extra src root.
        self.assertFalse(list(view.VIEW_DIR.glob('*.ned')))

    def test_static_and_animated_arguments_and_validation(self):
        with contextlib.redirect_stdout(io.StringIO()) as default_output:
            view.main(['--dry-run'])
        self.assertIn('snapshotTime=-1s',default_output.getvalue())
        self.assertIn('animated replay',default_output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as precise:
            view.main(['--dry-run','--time','30.000001'])
        self.assertIn('snapshotTime=30.000001s',precise.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            view.main(['--dry-run','--animate','--k','10'])
        self.assertIn('snapshotTime=-1s',output.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            for args in (['--time','nan'],['--time','-1'],['--k','11'],['--animate','--time','1']):
                with self.assertRaises(SystemExit):view.main(['--dry-run',*args])

    def test_physical_overlap_count_and_render_styles(self):
        compiler=shutil.which('clang++') or shutil.which('g++')
        if not compiler:self.skipTest('No C++ compiler available')
        with tempfile.TemporaryDirectory() as directory:
            binary=Path(directory)/'test-overlap'
            subprocess.run([compiler,'-std=c++17','-I'+str(PROJECT/'src'),
                str(PROJECT/'tests/test_leo_view_routes.cc'),'-o',str(binary)],check=True,capture_output=True,text=True)
            subprocess.run([str(binary)],check=True,capture_output=True,text=True)


if __name__=='__main__':
    unittest.main()
