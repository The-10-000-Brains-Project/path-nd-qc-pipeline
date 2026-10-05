"""Managed model setup for pipeline runs and explicit backend registration.

No package import or pip installation triggers model downloads. User checkouts are never patched.
Only a freshly cloned, pinned checkout inside the managed data directory receives our patches.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request

from pathnd_qc.artifact_store import atomic_write
from .paths import data_dir, settings_path, sha256, describe_pen, describe_grandqc

RESOURCES = Path(__file__).resolve().parent
CATALOG = json.loads((RESOURCES / "catalog.json").read_text())
DEPENDENCIES = {
    "pen": ["torch>=2.6,<3", "torchvision>=0.21,<1", "segmentation-models-pytorch==0.5.0"],
    "grandqc": ["torch>=2.6,<3", "torchvision>=0.21,<1", "segmentation-models-pytorch==0.5.0", "timm>=1.0,<1.1",
                "openslide-python>=1.4,<2", "openslide-bin>=4.0,<5", "tqdm>=4.66,<5",
                "numpy>=2.0,<3", "pillow>=11,<13", "opencv-python-headless>=4.10,<6"],
}


def _command(command, *, cwd=None, capture=False, timeout=900):
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=capture, timeout=timeout)
    if result.returncode:
        detail = (result.stderr or result.stdout or "")[-3000:] if capture else "see command output"
        raise RuntimeError(f"Command failed ({result.returncode}): {command[0]}: {detail}")
    return result.stdout or ""


def download(url: str, target: Path, expected_sha256: str) -> Path:
    """Download with bounded attempts; publish only bytes matching the expected hash."""
    if not url.startswith("https://"):
        raise ValueError("Downloads require an https:// URL")
    if len(expected_sha256) != 64 or any(c not in '0123456789abcdef' for c in expected_sha256.lower()):
        raise ValueError("A 64-character SHA-256 checksum is required")
    if target.exists():
        if sha256(target) != expected_sha256.lower():
            raise ValueError(f"Checksum mismatch in existing {target}; choose a fresh data directory or remove that corrupt file")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    import certifi
    tls = ssl.create_default_context()
    tls.load_verify_locations(cafile=certifi.where())
    last = None
    for _ in range(3):
        try:
            # Written beside the target and renamed into place only after the checksum matches;
            # a failed or interrupted transfer never leaves a partial file at the target name.
            with atomic_write(target) as tmp:
                with open(tmp, 'wb') as out:
                    with urllib.request.urlopen(url, timeout=120, context=tls) as response:
                        if not response.geturl().startswith('https://'):
                            raise ValueError("Refusing a download redirected away from HTTPS")
                        shutil.copyfileobj(response, out, 1024 * 1024)
                if sha256(tmp) != expected_sha256.lower():
                    raise ValueError(f"Checksum mismatch for {target.name}")
            return target
        except ValueError:
            raise
        except OSError as exc:
            last = exc
    raise RuntimeError(f"Download failed: {url}: {last}")


def _read_settings():
    path = settings_path()
    if not path.exists():
        return {"config": {}, "assets": {}}
    result = json.loads(path.read_text())
    if not isinstance(result, dict) or not isinstance(result.get('config'), dict) or not isinstance(result.get('assets'), dict):
        raise ValueError(f"Invalid managed settings in {path}")
    return result


def _register(kind, entry, config):
    # Call under the setup lock. Never overwrite another backend's registration.
    from pathnd_qc.config.config import _deep_merge
    settings = _read_settings()
    settings['config'] = _deep_merge(settings['config'], config)
    settings['assets'][kind] = entry
    dest = settings_path()
    with atomic_write(dest) as tmp:
        tmp.write_text(json.dumps(settings, indent=2) + '\n', encoding='utf-8')


def validate_grandqc(repo, python=None):
    """Check required files and the subprocess CLI interface without modifying the checkout."""
    root = Path(repo).expanduser().resolve()
    for name in ('main.py', 'wsi_tis_detect.py', 'models/td/Tissue_Detection_MPP10.pth'):
        if not (root / name).is_file():
            raise ValueError(f"GrandQC is missing {root / name}")
    if not any((root / 'models/qc' / n).is_file() for n in
               ('GrandQC_MPP1.pth', 'GrandQC_MPP15.pth', 'GrandQC_MPP2.pth')):
        raise ValueError(f"No supported GrandQC artifact checkpoint under {root / 'models/qc'}")
    executable = python or sys.executable
    for script, flags in [('wsi_tis_detect.py', ('--slide_folder', '--output_dir')),
                          ('main.py', ('--slide_folder', '--output_dir', '--mpp_model', '--create_geojson', '--device'))]:
        help_text = _command([executable, '-B', str(root / script), '--help'], cwd=root, capture=True, timeout=120)
        missing = [flag for flag in flags if flag not in help_text]
        if missing:
            raise ValueError(f"Incompatible GrandQC {script}: missing options {', '.join(missing)}")
    return str(root)


def validate_pen(weights):
    """Validate the checkpoint against the pipeline's actual architecture."""
    _command([sys.executable, '-P', '-c',
              'from pathnd_qc.qc_slide.pen.pen import build_pen_model; import sys; '
              'build_pen_model(sys.argv[1], device="cpu")', str(weights)], capture=True, timeout=180)


def _managed_grandqc(weights_dir=None):
    spec = CATALOG['grandqc']
    parent = data_dir() / 'grandqc'
    dest = parent / f"{spec['commit']}-patch{spec['patch_version']}"
    inference = '01_WSI_inference_OPENSLIDE_QC'
    # A receipt distinguishes a complete managed installation from leftover files.
    if dest.exists():
        receipt = dest / 'pathnd-receipt.json'
        if not receipt.is_file():
            raise ValueError(f"Incomplete managed checkout: {dest}; move it aside before retrying")
        saved = json.loads(receipt.read_text())
        actual = describe_grandqc(dest / inference)['files_sha256']
        if saved['files_sha256'] != actual:
            raise ValueError(f"Managed GrandQC files changed at {dest}; use --repo for a custom version")
        return dest / inference
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.install-', dir=parent) as tmp:
        checkout = Path(tmp) / 'repo'
        _command(['git', 'clone', '--no-checkout', spec['repository'], str(checkout)])
        _command(['git', '-C', str(checkout), 'checkout', '--detach', spec['commit']])
        for patch in sorted((RESOURCES / 'patches').glob('*.patch')):
            _command(['git', '-C', str(checkout), 'apply', '--check', str(patch)])
            _command(['git', '-C', str(checkout), 'apply', str(patch)])
        root = checkout / inference
        shutil.copy2(RESOURCES / 'grandqc_compat.py', root / 'grandqc_compat.py')
        for name, item in CATALOG['weights'].items():
            if name == 'pen.pt':
                continue
            target = root / 'models' / ('td' if name.startswith('Tissue') else 'qc') / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if weights_dir:
                source = Path(weights_dir).expanduser().resolve() / name
                if sha256(source) != item['sha256']:
                    raise ValueError(f"Checksum mismatch: {source}")
                shutil.copyfile(source, target)
            else:
                print(f"Downloading {name}", flush=True)
                download(item['url'], target, item['sha256'])
        receipt = describe_grandqc(root)
        (checkout / 'pathnd-receipt.json').write_text(json.dumps(receipt, indent=2))
        checkout.rename(dest)
    return dest / inference


def _managed_pen(url=None, checksum=None):
    spec = CATALOG['weights']['pen.pt']
    expected = checksum or spec['sha256']
    # Validate custom digests before using them as directory names.
    if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected.lower()):
        raise ValueError('Invalid SHA-256 checksum')
    target = data_dir() / 'pen' / expected.lower() / 'pen.pt'
    if url:
        return download(url, target, expected)
    if target.exists():
        if sha256(target) != expected:
            raise ValueError(f'Checksum mismatch: {target}')
        return target
    try:
        import gdown
    except ImportError as exc:
        raise RuntimeError('Pen download needs gdown: reinstall "pathnd-qc", or use setup pen --weights /path/to/pen.pt') from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.pen-', dir=target.parent) as tmp:
        try:
            files = gdown.download_folder(CATALOG['pen_folder'], output=tmp, quiet=False,
                                          skip_download=True, use_cookies=False)
            candidates = [f for f in (files or []) if Path(f.path).name == 'pen.pt']
            if len(candidates) != 1:
                raise RuntimeError('Expected exactly one pen.pt in the upstream folder')
            downloaded = gdown.download(id=candidates[0].id, output=str(Path(tmp) / 'pen.pt'),
                                       quiet=False, use_cookies=False)
            if not downloaded:
                raise RuntimeError('Google Drive did not return a file')
            matches = [Path(downloaded)]
        except Exception as exc:
            raise RuntimeError('Pen download failed; download pen.pt using your browser and use '
                               f'setup pen --weights PATH. Provider error: {exc}') from exc
        if sha256(matches[0]) != expected:
            raise ValueError('Downloaded pen.pt checksum does not match the catalog')
        os.replace(matches[0], target)
    return target


def check_installations(kind=None, repo=None, weights=None, python=None):
    from pathnd_qc.config.config import cfg
    from pathnd_qc.qc_tile.artifacts.artifacts import resolve_repo
    kinds = [kind] if kind else ['pen', 'grandqc']
    results = {}
    for name in kinds:
        try:
            if name == 'pen':
                selected = weights or cfg('m2.pen.weights_path')
                if not selected:
                    raise ValueError('No pen weights configured')
                info = describe_pen(selected)
                validate_pen(info['path'])
            else:
                selected = resolve_repo(repo)[0]
                if not selected:
                    raise ValueError('No usable GrandQC checkout configured')
                executable = python or cfg('m3.artifacts.python') or sys.executable
                selected = validate_grandqc(selected, executable)
                info = describe_grandqc(selected, executable)
            registered = _read_settings()['assets'].get(name)
            if registered and registered.get('path') == info['path']:
                for field in ('sha256', 'files_sha256'):
                    if field in registered and registered[field] != info[field]:
                        raise ValueError('Registered files have changed; inspect them and register again explicitly')
            results[name] = {'ok': True, **info}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            results[name] = {'ok': False, 'error': str(exc)}
    return results


def ensure_models(components, *, pen_weights=None, grandqc_repo=None, grandqc_python=None,
                  download_models=True):
    """Resolve selected backends, installing missing default assets when downloads are allowed.

    Explicit or custom configured paths are used as supplied. Invalid custom paths remain
    preflight errors. Managed registration is read on each call so a long-lived process can
    reuse setup completed after its component modules were imported.
    """
    from pathnd_qc.config.config import cfg, DEFAULTS_PATH, resolve_path
    from pathnd_qc.qc_tile.artifacts.artifacts import check_grandqc_repo

    if type(download_models) is not bool:
        raise ValueError('download_models must be true or false')
    offline = os.environ.get('PATHND_NO_MODEL_DOWNLOAD', '0')
    if offline not in {'0', '1'}:
        raise ValueError('PATHND_NO_MODEL_DOWNLOAD must be 0 or 1')
    allowed = download_models and offline != '1'
    defaults = json.loads(DEFAULTS_PATH.read_text())
    paths = {'pen': pen_weights or resolve_path(cfg('m2.pen.weights_path')),
             'grandqc': grandqc_repo or resolve_path(cfg('m3.artifacts.repo_path'))}
    specifications = (
        ('pen', 'pen_detection', defaults['m2']['pen']['weights_path']),
        ('grandqc', 'tile_artifacts', defaults['m3']['artifacts']['repo_path']),
    )
    for kind, component, default in specifications:
        if component not in components:
            continue
        path = paths[kind]
        usable = (lambda p: bool(p and Path(p).is_file())) if kind == 'pen' else (
            lambda p: check_grandqc_repo(p) is None)
        if usable(path):
            continue
        registered = _read_settings()['assets'].get(kind, {})
        managed_path = registered.get('managed') and registered.get('path') == path
        if path and path != resolve_path(default) and not managed_path:
            continue
        if not usable(registered.get('path')):
            if not allowed:
                continue
            print(f'Setting up {kind} for this run; model files are reused on later runs.', flush=True)
            if main([kind]) != 0:
                raise ValueError(f'Automatic {kind} setup failed; see the setup error above.')
            registered = _read_settings()['assets'].get(kind, {})
        paths[kind] = registered.get('path') or path
        if kind == 'grandqc':
            grandqc_python = grandqc_python or registered.get('python')
    return paths['pen'], paths['grandqc'], grandqc_python


def main(argv=None):
    parser = argparse.ArgumentParser(description='Download tested backends or register your own. No downloads happen during pip install.')
    parser.add_argument('backend', nargs='?', choices=['pen', 'grandqc'])
    parser.add_argument('--check', action='store_true', help='check selected/current backends without downloads or registration')
    parser.add_argument('--repo', help='your compatible GrandQC inference directory; never modified')
    parser.add_argument('--weights', help='your pen checkpoint; never modified')
    parser.add_argument('--weights-dir', help='reuse checksum-verified official GrandQC weights from this directory')
    parser.add_argument('--python', help='Python executable for GrandQC; defaults to this environment')
    parser.add_argument('--install-deps', action='store_true', help='install backend Python dependencies into the selected environment')
    parser.add_argument('--url', help='HTTPS URL for pen weights; requires --sha256')
    parser.add_argument('--sha256', help='SHA-256 of --url bytes')
    args = parser.parse_args(argv)
    if not args.backend and not args.check:
        parser.error('choose pen or grandqc, or use --check')
    if bool(args.url) != bool(args.sha256):
        parser.error('--url and --sha256 must be used together')
    if args.backend == 'pen' and (args.repo or args.python or args.weights_dir):
        parser.error('--repo, --python and --weights-dir are GrandQC options')
    if args.backend == 'grandqc' and (args.weights or args.url):
        parser.error('--weights and --url are pen options')
    if args.repo and args.weights_dir:
        parser.error('--repo and --weights-dir cannot be combined')
    if args.weights and args.url:
        parser.error('--weights and --url cannot be combined')
    if args.check and (args.install_deps or args.url or args.weights_dir):
        parser.error('--check cannot install or download files')
    try:
        if args.check:
            results = check_installations(args.backend, args.repo, args.weights, args.python)
            print(json.dumps(results, indent=2))
            return 0 if all(r['ok'] for r in results.values()) else 1
        from filelock import FileLock
        data_dir().mkdir(parents=True, exist_ok=True)
        with FileLock(str(data_dir() / '.setup.lock'), timeout=3600):
            python = shutil.which(args.python or sys.executable)
            if not python:
                raise ValueError(f'Python executable not found: {args.python}')
            python = str(Path(python).absolute())
            if args.install_deps:
                deps = DEPENDENCIES[args.backend] + (['gdown>=5.2,<6'] if args.backend == 'pen' and not args.weights and not args.url else [])
                pair = [dep for dep in deps if dep.startswith(('torch>', 'torchvision>'))]
                command = [python, '-P', '-m', 'pip', 'install', '--upgrade', '--force-reinstall', *pair]
                if sys.platform != 'darwin':
                    command += ['--index-url', 'https://download.pytorch.org/whl/cpu']
                _command(command)
                _command([python, '-P', '-m', 'pip', 'install', *[dep for dep in deps if dep not in pair]])
            if args.backend == 'grandqc':
                repo = Path(args.repo).expanduser().resolve() if args.repo else _managed_grandqc(args.weights_dir)
                repo = validate_grandqc(repo, python)
                entry = describe_grandqc(repo, python)
                config = {'m3': {'artifacts': {'repo_path': repo, 'python': python}}}
            else:
                weights = Path(args.weights).expanduser().resolve() if args.weights else _managed_pen(args.url, args.sha256)
                validate_pen(weights)
                entry = describe_pen(weights)
                config = {'m2': {'pen': {'weights_path': str(weights)}}}
            entry['managed'] = not bool(args.repo or args.weights)
            _register(args.backend, entry, config)
            print(f"Ready: {args.backend} at {entry['path']}\nSettings: {settings_path()}")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'Setup failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
