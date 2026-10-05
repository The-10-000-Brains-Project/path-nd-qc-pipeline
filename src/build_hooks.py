"""Stamp source identity into distributions without modifying the source checkout."""
import importlib.util
import json
from pathlib import Path

from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist


def _stamp(destination):
    package = Path(__file__).resolve().parent / "pathnd_qc"
    spec = importlib.util.spec_from_file_location("pathnd_build_identity", package / "_provenance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity = module.implementation_provenance(package)
    target = Path(destination) / "pathnd_qc" / module.BUILD_RECORD
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)  # Unlink the build record to avoid modifying a hard-linked source distribution.
    # Preserve known provenance without inventing a commit for source copies.
    if identity["git_commit"] is not None:
        target.write_text(json.dumps({"format": 1, **identity}, indent=2) + "\n", encoding="utf-8")


class build_py(_build_py):
    def run(self):
        super().run()
        if not self.dry_run and not self.editable_mode:
            _stamp(self.build_lib)


class sdist(_sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        _stamp(base_dir)
