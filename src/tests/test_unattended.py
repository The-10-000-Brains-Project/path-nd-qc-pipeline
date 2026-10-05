"""Unattended-run regressions: no network or model downloads; run with Python directly."""
from pathlib import Path
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))
from pathnd_qc.ingestion._download import open_binary
from pathnd_qc.ingestion.metadata import metadata
from pathnd_qc.ingestion.wsi_reader import wsi_reader
from pathnd_qc.qc_slide.pen import pen
from pathnd_qc.batch import runner, manifest, summary
from fake_slide import FakeSlide, FakeReader, write_tiff


class UnattendedTests(unittest.TestCase):
    def test_pipeline_records_bank_and_keeps_observations_out_of_execution_reasons(self):
        from pathnd_qc import pipeline
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'slide.svs')
            path.touch()
            slide = FakeSlide(base_wh=(2048, 1536), mpp=None, objective_power=20)
            with patch.object(pipeline, 'GCSWSIReader', return_value=FakeReader(slide)):
                result = pipeline.run(str(path), components={'tissue_segmentation'}, bank='PART',
                                      stain_type='AT8', metadata_enabled=False, download_models=False,
                                      out_dir=str(Path(tmp, 'out')))
            report = result['report']
            self.assertEqual(report['m1']['ingestion']['source']['dataset'], 'PART')
            self.assertTrue(report['provenance']['execution']['complete'])
            self.assertEqual(report['provenance']['execution']['reasons'], [])
            self.assertTrue(any('scale' in warning for warning in report['provenance']['warnings']))

    def test_setup_installs_torch_pair_from_one_index(self):
        from pathnd_qc.external import manager
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, PATHND_DATA_DIR=tmp), \
             patch.object(manager, '_command') as command, \
             patch.object(manager, 'validate_pen'), patch.object(manager, 'describe_pen', return_value={'path': '/fixture/pen.pt'}), \
             patch.object(manager, '_register'), patch.object(manager.sys, 'platform', 'linux'):
            self.assertEqual(manager.main(['pen', '--weights', str(Path(tmp, 'pen.pt')), '--install-deps']), 0)
        commands = [call.args[0] for call in command.call_args_list]
        self.assertIn('torch>=2.6,<3', commands[0])
        self.assertIn('torchvision>=0.21,<1', commands[0])
        self.assertIn('https://download.pytorch.org/whl/cpu', commands[0])
        self.assertNotIn('torchvision>=0.21,<1', commands[1])

    def test_gcs_file_options_reach_the_file_and_disable_prefetch(self):
        import gcsfs
        fs = gcsfs.core.GCSFileSystem(token='anon', skip_instance_cache=True)
        with patch('fsspec.core.url_to_fs', return_value=(fs, 'bucket/slide')), \
             patch.object(fs, 'info', return_value={'name': 'bucket/slide', 'size': 5}), \
             patch('gcsfs.core.GCSFile._fetch_range', return_value=b'slide'):
            with open_binary('gs://bucket/slide', 5) as stream:
                self.assertIsNone(getattr(stream, '_prefetch_engine', None))
                self.assertEqual(stream.cache.name, 'readahead')
                self.assertEqual(stream.read(), b'slide')
            self.assertTrue(stream.closed)

    def test_remote_stream_closed_on_normal_and_corrupt_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'slide.tif'
            write_tiff(FakeSlide(base_wh=(128, 96)), str(path))
            stream = io.BytesIO(path.read_bytes())
            with patch.object(wsi_reader, 'open_binary', return_value=stream):
                with wsi_reader.GCSWSIReader().slide('gs://bucket/slide.tif') as slide:
                    self.assertEqual(slide.dimensions, (128, 96))
                self.assertTrue(stream.closed)
            corrupt = io.BytesIO(b'corrupt')
            with patch.object(wsi_reader, 'open_binary', return_value=corrupt):
                self.assertIsNone(wsi_reader.GCSWSIReader().open_slide('gs://bucket/bad'))
            self.assertTrue(corrupt.closed)

    def test_same_filename_in_distinct_folders_keeps_its_own_stain(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv = Path(tmp) / 'metadata.csv'
            csv.write_text('slide_paths,stain_type\ngs://PART/AT8/46884.svs,AT8\ngs://PART/HE/46884.svs,H&E\n')
            sources = metadata.sources_from_paths([str(csv)])
            for folder, stain in [('AT8', 'AT8'), ('HE', 'H&E')]:
                result = metadata.lookup(f'gs://PART/{folder}/46884.svs', sources=sources)
                self.assertEqual(result['result']['source']['stain_type'], stain)
                self.assertEqual(result['result']['source']['slide_id'], '46884')
                self.assertEqual(result['index_errors'], [])
            self.assertIsNone(metadata.lookup('gs://OTHER/46884.svs', sources=sources)['result'])

    def test_pen_stitching_preserves_every_pixel_and_bounds_forward_size(self):
        rng = np.random.default_rng(41)
        pixels = rng.integers(0, 256, (781, 1057, 3), dtype=np.uint8)
        shapes = []
        def classify(image, model, device):
            shapes.append(image.size)
            return (np.asarray(image)[:, :, 0] > 127).astype(np.uint8)
        with patch.object(pen, '_pred', side_effect=classify):
            result = pen._pred_tiled(Image.fromarray(pixels), None, 'cpu', 512, 64)
        np.testing.assert_array_equal(result, pixels[:, :, 0] > 127)
        self.assertEqual(len(shapes), 6)
        self.assertLessEqual(max(max(s) for s in shapes), 640)

    def test_small_pen_image_uses_one_identical_patch(self):
        image = Image.new('RGB', (127, 53), 'white')
        with patch.object(pen, '_pred', return_value=np.ones((53, 127), dtype=np.uint8)) as predict:
            result = pen._pred_tiled(image, None, 'cpu', 512, 64)
        self.assertEqual(predict.call_count, 1)
        np.testing.assert_array_equal(np.asarray(predict.call_args.args[0]), np.asarray(image))
        self.assertTrue(result.all())

    def test_failed_pen_tile_never_returns_partial_measurements(self):
        with patch.object(pen, '_get_model', return_value=None), \
             patch.object(pen, '_pred', side_effect=[np.zeros((512, 576), dtype=np.uint8), RuntimeError('bad tile')]):
            result = pen.detect_pen(Image.new('RGB', (1024, 512)), weights_path='fixture', tile_px=512)
        self.assertIsNone(result['pen_mask'])
        self.assertIsNone(result['pen_area_fraction'])
        self.assertIn('bad tile', result['pen_error'])

    def test_invalid_pen_tile_sizes_fail_loudly(self):
        for tile, halo in [(0, 64), (513, 64), (512, -32), (512, 1), (True, 0)]:
            with self.subTest(tile=tile, halo=halo), self.assertRaises(ValueError):
                pen._pred_tiled(Image.new('RGB', (1, 1)), None, 'cpu', tile, halo)

    def test_child_cannot_import_callers_timeit(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'timeit.py').write_text('raise RuntimeError("shadow imported")')
            job = manifest.Job('/fixture.svs', 'fixture', 'fixture')
            command = runner.build_command(job, tmp, ['--run_tissue_segmentation'])
            command = command[:command.index('-m')] + ['-c', 'import timeit; print(timeit.__file__)']
            env = dict(os.environ)
            env.pop('PYTHONPATH', None)
            result = subprocess.run(command, cwd=tmp, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(tmp, result.stdout)

    def test_parent_prepares_selected_models_and_forwards_paths(self):
        with patch('pathnd_qc.external.manager.ensure_models', return_value=('/models/pen.pt', None, None)) as setup:
            result = runner.prepare_models(['--run_pen_detection', '--no_model_download'])
        self.assertEqual(setup.call_count, 1)
        self.assertEqual(setup.call_args.args[0], {'pen_detection'})
        self.assertFalse(setup.call_args.kwargs['download_models'])
        self.assertEqual(result[-2:], ['--pen_weights', '/models/pen.pt'])

    def test_summary_keeps_thumbnail_scale_base_scale_and_warnings_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 's_status.json').write_text(json.dumps({'state': 'completed', 'slide_path': '/s.svs', 'report': 's_report.json'}))
            (root / 's_report.json').write_text(json.dumps({
                'provenance': {'bank': 'PART', 'execution': {'complete': True, 'reasons': []}, 'warnings': ['scale derived']},
                'm1': {'ingestion': {'source': {'dataset': None}, 'acquisition': {'mpp_effective': 0.5, 'mpp_source': 'stated'}}},
                'm2': {'read': {'achieved_mpp': 8.0, 'source': 'computed'}}}))
            row = summary.row_for(root)
            self.assertIsNone(row['read_error'], row)
            self.assertEqual(row['dataset'], 'PART')
            self.assertEqual(row['mpp_effective'], 8.0)
            self.assertEqual(row['mpp_base'], 0.5)
            self.assertEqual(row['warnings'], 'scale derived')
            self.assertIsNone(row['execution_reasons'])

    def test_eight_installers_publish_one_download(self):
        script = r'''
import hashlib, io, os, time
from pathlib import Path
from unittest.mock import patch
from pathnd_qc.external import manager
payload = b'checkpoint'
class Response(io.BytesIO):
    def geturl(self): return 'https://example.org/pen.pt'
def fetch(*args, **kwargs):
    with open(Path(os.environ['PATHND_DATA_DIR'])/'downloads', 'a') as f: f.write('download\n')
    time.sleep(.15)
    return Response(payload)
with patch.object(manager.urllib.request, 'urlopen', side_effect=fetch), patch.object(manager, 'validate_pen'):
    raise SystemExit(manager.main(['pen', '--url', 'https://example.org/pen.pt', '--sha256', hashlib.sha256(payload).hexdigest()]))
'''
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PATHND_DATA_DIR=tmp, PATHND_CONFIG='', PYTHONPATH=str(SRC))
            processes = [subprocess.Popen([sys.executable, '-P', '-c', script], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
            try:
                for process in processes:
                    stdout, stderr = process.communicate(timeout=60)
                    self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertEqual(Path(tmp, 'downloads').read_text().splitlines(), ['download'])
                weights = list(Path(tmp).glob('pen/*/pen.pt'))
                self.assertEqual(len(weights), 1)
                self.assertEqual(weights[0].read_bytes(), b'checkpoint')
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()


if __name__ == '__main__':
    unittest.main()
