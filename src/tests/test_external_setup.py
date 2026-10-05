"""Hermetic packaging/setup regressions. No model downloads or inference."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pathnd_qc.external import manager
from pathnd_qc.external.paths import settings_path
from pathnd_qc.config import config


class Response(io.BytesIO):
    def geturl(self):
        return 'https://example.org/model'


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'PATHND_DATA_DIR': str(self.root), 'PATHND_CONFIG': ''})
        self.env.start()
        config.load(force=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        config.load(force=True)

    def test_download_atomically_verifies_and_reuses(self):
        target = self.root/'nested/model'
        digest = hashlib.sha256(b'model').hexdigest()
        with patch.object(manager.urllib.request, 'urlopen', return_value=Response(b'model')) as fetch:
            manager.download('https://example.org/model', target, digest)
            manager.download('https://example.org/model', target, digest)
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(target.read_bytes(), b'model')
        self.assertEqual(list(target.parent.glob('.download-*')), [])

    def test_checksum_mismatch_never_publishes(self):
        target = self.root/'model'
        with patch.object(manager.urllib.request, 'urlopen', return_value=Response(b'bad')):
            with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
                manager.download('https://example.org/model', target, '0'*64)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob('.download-*')), [])

    def test_existing_corruption_is_not_overwritten(self):
        target = self.root/'model'; target.write_bytes(b'bad')
        with patch.object(manager.urllib.request, 'urlopen') as fetch:
            with self.assertRaises(ValueError):
                manager.download('https://example.org/model', target, '0'*64)
            fetch.assert_not_called()
        self.assertEqual(target.read_bytes(), b'bad')

    def test_download_retry_exhaustion_cleans_partial_files(self):
        with patch.object(manager.urllib.request, 'urlopen', side_effect=OSError('offline')) as fetch:
            with self.assertRaisesRegex(RuntimeError, 'Download failed'):
                manager.download('https://example.org/model', self.root/'model', '0'*64)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(list(self.root.glob('.download-*')), [])

    def test_non_https_and_bad_digest_rejected(self):
        for url, digest in [('http://example.org/m', '0'*64), ('https://example.org/m', '../bad')]:
            with self.assertRaises(ValueError):
                manager.download(url, self.root/'m', digest)

    def test_managed_settings_merge_and_explicit_override(self):
        manager._register('pen', {'path': '/pen'}, {'m2': {'pen': {'weights_path': '/pen'}}})
        manager._register('grandqc', {'path': '/gc'}, {'m3': {'artifacts': {'repo_path': '/gc'}}})
        config.load(force=True)
        self.assertEqual(config.cfg('m2.pen.weights_path'), '/pen')
        self.assertEqual(config.cfg('m3.artifacts.repo_path'), '/gc')
        override = self.root/'override.json'
        override.write_text(json.dumps({'m2': {'pen': {'weights_path': '/custom'}}}))
        config.load(path=str(override))
        self.assertEqual(config.cfg('m2.pen.weights_path'), '/custom')
        self.assertEqual(set(manager._read_settings()['assets']), {'pen', 'grandqc'})
        self.assertIn(str(settings_path()), config.sources())

    def fixture_repo(self, compatible=True):
        root = self.root/'my fork'; root.mkdir()
        (root/'helper.py').write_text('VALUE=1\n')
        for name in ('main.py','wsi_tis_detect.py'):
            root.joinpath(name).write_text('import helper\nprint("--slide_folder --output_dir --mpp_model --create_geojson '+('--device' if compatible else '')+'")')
        for name in ('models/qc/GrandQC_MPP1.pth','models/td/Tissue_Detection_MPP10.pth'):
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture')
        return root

    def test_custom_repo_registration_leaves_files_untouched(self):
        repo=self.fixture_repo()
        before={str(p):p.read_bytes() for p in repo.rglob('*') if p.is_file()}
        self.assertEqual(manager.main(['grandqc','--repo',str(repo),'--python',sys.executable]),0)
        after={str(p):p.read_bytes() for p in repo.rglob('*') if p.is_file()}
        self.assertEqual(before,after)
        self.assertFalse(manager._read_settings()['assets']['grandqc']['managed'])
        self.assertEqual(manager._read_settings()['config']['m3']['artifacts']['python'],sys.executable)

    def test_incompatible_repo_does_not_register(self):
        repo=self.fixture_repo(False)
        self.assertEqual(manager.main(['grandqc','--repo',str(repo)]),1)
        self.assertFalse(settings_path().exists())

    def test_failed_pen_validation_does_not_register(self):
        weights=self.root/'pen.pt';weights.write_bytes(b'invalid')
        with patch.object(manager,'validate_pen',side_effect=RuntimeError('wrong architecture')):
            self.assertEqual(manager.main(['pen','--weights',str(weights)]),1)
        self.assertFalse(settings_path().exists())
        self.assertEqual(weights.read_bytes(),b'invalid')

    def test_setup_check_never_downloads(self):
        repo=self.fixture_repo()
        with patch.object(manager,'download',side_effect=AssertionError('unexpected download')):
            self.assertEqual(manager.main(['grandqc','--check','--repo',str(repo)]),0)
        self.assertFalse(settings_path().exists())

    def test_failed_managed_clone_leaves_no_install(self):
        with patch.object(manager, '_command', side_effect=RuntimeError('clone failed')):
            self.assertEqual(manager.main(['grandqc']),1)
        self.assertFalse(settings_path().exists())
        self.assertEqual(list((self.root/'grandqc').iterdir()),[])

    def test_bad_settings_not_overwritten(self):
        settings_path().write_text('[]')
        with self.assertRaises(ValueError):
            manager._register('pen', {}, {})
        self.assertEqual(settings_path().read_text(),'[]')

    def test_batch_forwards_backend_interpreter(self):
        from pathnd_qc.batch.cli import build_parser, _run_args
        args=build_parser().parse_args(['run','--slides','slides.txt','--out','out','--grandqc_python',sys.executable])
        self.assertEqual(_run_args(args),['--grandqc_python',sys.executable])

    def test_backend_uses_selected_interpreter(self):
        from pathnd_qc.qc_tile.artifacts import artifacts_tile as backend
        repo=self.fixture_repo();slide=self.root/'slide.svs';slide.write_bytes(b'fixture')
        failure=subprocess.CompletedProcess([],1,stdout='',stderr='expected fixture stop')
        with patch.object(backend.subprocess,'run',return_value=failure) as launch:
            result=backend.run_grandqc_full_mask(str(slide),str(repo),python='/custom/python')
        self.assertEqual(launch.call_args.args[0][0],'/custom/python')
        self.assertIsNotNone(result['grandqc_error'])


if __name__ == '__main__':
    unittest.main()
