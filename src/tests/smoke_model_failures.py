"""Smoke test — failed model transfers and installs preserve usable registrations.

Contracts: external/README.md (managed storage and compatibility), manager.download's
atomic publication contract, and pen.detect_pen's null-on-failure contract. All downloads
and clone operations are injected; files, atomic writes and backend help subprocesses are real.
Run: python src/tests/smoke_model_failures.py
"""

from __future__ import annotations

import builtins
from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pathnd_qc.external import manager as M
from pathnd_qc.external.paths import settings_path
from pathnd_qc.qc_slide.pen import pen as P

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + str(detail) if detail else ''}"
    )


def raises(kind, action):
    try:
        action()
    except kind as exc:
        return str(exc)
    return None


class Response(io.BytesIO):
    def geturl(self):
        return "https://example.invalid/pen.pt"


class BrokenResponse(Response):
    """Transfer a real prefix, then fail before EOF."""

    def read(self, size=-1):
        if self.tell():
            raise OSError("connection interrupted after prefix")
        return super().read(3)


def fixture_repo(root):
    (root / "models/td").mkdir(parents=True)
    (root / "models/qc").mkdir()
    (root / "models/td/Tissue_Detection_MPP10.pth").write_bytes(b"tissue fixture")
    (root / "models/qc/GrandQC_MPP1.pth").write_bytes(b"artifact fixture")
    for name in ("main.py", "wsi_tis_detect.py"):
        (root / name).write_text(
            'print("--slide_folder --output_dir --mpp_model --create_geojson --device")\n'
        )
    return root


out = Path(tempfile.mkdtemp(prefix="pathnd-model-failures-"))
try:
    print("\n[1] Transfer failure never publishes a partial checkpoint")
    payload = b"a complete checkpoint fixture"
    digest = hashlib.sha256(payload).hexdigest()
    destination = out / "transfer" / "pen.pt"
    # download docstring: bounded attempts; only hash-verified bytes may be published.
    with patch.object(
        M.urllib.request,
        "urlopen",
        side_effect=[BrokenResponse(payload), Response(payload)],
    ) as fetch:
        result = M.download("https://example.invalid/pen.pt", destination, digest)
    check(
        "a retry starts from clean bytes after a partial transfer",
        result.read_bytes() == payload
        and fetch.call_count == 2
        and list(destination.parent.iterdir()) == [destination],
    )
    destination.unlink()
    with patch.object(
        M.urllib.request,
        "urlopen",
        side_effect=lambda *a, **kw: BrokenResponse(payload),
    ) as fetch:
        error = raises(
            RuntimeError,
            lambda: M.download("https://example.invalid/pen.pt", destination, digest),
        )
    check(
        "exhausted partial transfers leave neither checkpoint nor temporary files",
        bool(error)
        and "connection interrupted" in error
        and fetch.call_count == 3
        and list(destination.parent.iterdir()) == [],
        error,
    )
    response = Response(payload)
    response.geturl = lambda: "http://example.invalid/pen.pt"
    with patch.object(M.urllib.request, "urlopen", return_value=response) as fetch:
        error = raises(
            ValueError,
            lambda: M.download("https://example.invalid/pen.pt", destination, digest),
        )
    check(
        "an insecure redirect is refused before any checkpoint is published",
        bool(error)
        and "HTTPS" in error
        and fetch.call_count == 1
        and list(destination.parent.iterdir()) == [],
        error,
    )
    with patch.object(M.urllib.request, "urlopen", side_effect=KeyboardInterrupt):
        error = raises(
            KeyboardInterrupt,
            lambda: M.download("https://example.invalid/pen.pt", destination, digest),
        )
    check(
        "an interrupted transfer removes its open temporary checkpoint",
        error is not None and list(destination.parent.iterdir()) == [],
    )
    with patch.object(
        M.urllib.request, "urlopen", side_effect=lambda *a, **kw: Response(payload)
    ), patch.object(M.os, "replace", side_effect=PermissionError("destination denied")):
        error = raises(
            RuntimeError,
            lambda: M.download("https://example.invalid/pen.pt", destination, digest),
        )
    check(
        "publication failure removes verified temporary bytes and reports the filesystem error",
        bool(error)
        and "destination denied" in error
        and list(destination.parent.iterdir()) == [],
        error,
    )

    print("\n[2] Provider errors leave previous model registrations intact")
    with patch.dict(os.environ, PATHND_DATA_DIR=str(out / "managed"), PATHND_CONFIG=""):
        settings_path().parent.mkdir(parents=True)
        previous_weights = out / "previous-pen.pt"
        previous_weights.write_bytes(b"previous checkpoint")
        previous = {
            "config": {"m2": {"pen": {"weights_path": str(previous_weights)}}},
            "assets": {
                "pen": {
                    "path": str(previous_weights),
                    "sha256": M.sha256(previous_weights),
                    "managed": False,
                },
                "grandqc": {"path": "/previous/backend", "managed": False},
            },
        }
        settings_path().write_text(json.dumps(previous))
        before = settings_path().read_bytes()
        pen_root = settings_path().parent / "pen" / digest
        target = pen_root / "pen.pt"
        candidate = SimpleNamespace(path="upstream/pen.pt", id="fixture-id")
        provider = SimpleNamespace(
            download_folder=lambda **kw: [candidate], download=lambda **kw: None
        )
        catalog = dict(
            M.CATALOG, weights={**M.CATALOG["weights"], "pen.pt": {"sha256": digest}}
        )
        for label, listing, downloader, diagnostic in [
            ("missing upstream checkpoint", [], lambda **kw: None, "exactly one"),
            (
                "ambiguous upstream checkpoint",
                [candidate, candidate],
                lambda **kw: None,
                "exactly one",
            ),
            (
                "provider returns no file",
                [candidate],
                lambda **kw: None,
                "did not return a file",
            ),
        ]:
            provider.download_folder = lambda *a, _listing=listing, **kw: _listing
            provider.download = downloader
            with patch.dict(sys.modules, gdown=provider), patch.object(
                M, "CATALOG", catalog
            ), redirect_stderr(io.StringIO()) as stderr:
                code = M.main(["pen"])
            check(
                f"{label} preserves registration and leaves no partial model",
                code == 1
                and diagnostic in stderr.getvalue()
                and settings_path().read_bytes() == before
                and previous_weights.read_bytes() == b"previous checkpoint"
                and list(pen_root.iterdir()) == [],
                stderr.getvalue().strip(),
            )
        provider.download_folder = lambda *a, **kw: [candidate]

        def failed_download(**kwargs):
            Path(kwargs["output"]).write_bytes(b"partial")
            raise OSError("provider connection reset")

        provider.download = failed_download
        with patch.dict(sys.modules, gdown=provider), patch.object(
            M, "CATALOG", catalog
        ), redirect_stderr(io.StringIO()) as stderr:
            code = M.main(["pen"])
        check(
            "provider failure after writing bytes removes staging files and retains registration",
            code == 1
            and "provider connection reset" in stderr.getvalue()
            and list(pen_root.iterdir()) == []
            and settings_path().read_bytes() == before,
            stderr.getvalue().strip(),
        )

        def write_download(**kwargs):
            Path(kwargs["output"]).write_bytes(b"wrong checkpoint")
            return kwargs["output"]

        provider.download = write_download
        with patch.dict(sys.modules, gdown=provider), patch.object(
            M, "CATALOG", catalog
        ), redirect_stderr(io.StringIO()) as stderr:
            code = M.main(["pen"])
        check(
            "provider checksum mismatch cannot replace a registered model",
            code == 1
            and "checksum" in stderr.getvalue()
            and list(pen_root.iterdir()) == []
            and settings_path().read_bytes() == before,
            stderr.getvalue().strip(),
        )
        with patch.dict(sys.modules, gdown=None), patch.object(M, "CATALOG", catalog):
            error = raises(RuntimeError, M._managed_pen)
        check(
            "missing provider dependency names the offline registration remedy",
            bool(error)
            and "gdown" in error
            and "--weights" in error
            and settings_path().read_bytes() == before,
            error,
        )

        # A hash-valid download is reusable bytes, but failed architecture validation must not register it.
        def valid_download(**kwargs):
            Path(kwargs["output"]).write_bytes(payload)
            return kwargs["output"]

        provider.download = valid_download
        with patch.dict(sys.modules, gdown=provider), patch.object(
            M, "CATALOG", catalog
        ), patch.object(
            M,
            "validate_pen",
            side_effect=RuntimeError("checkpoint architecture mismatch"),
        ), redirect_stderr(
            io.StringIO()
        ) as stderr:
            code = M.main(["pen"])
        check(
            "failed architecture validation preserves previous settings after a valid transfer",
            code == 1
            and "architecture mismatch" in stderr.getvalue()
            and settings_path().read_bytes() == before
            and target.read_bytes() == payload,
            stderr.getvalue().strip(),
        )
        with patch.object(
            M, "_command", side_effect=RuntimeError("pip unavailable")
        ), patch.object(M, "validate_pen") as validate, redirect_stderr(
            io.StringIO()
        ) as stderr:
            code = M.main(["pen", "--weights", str(previous_weights), "--install-deps"])
        check(
            "dependency installation failure stops setup without replacing prior settings",
            code == 1
            and "pip unavailable" in stderr.getvalue()
            and not validate.called
            and settings_path().read_bytes() == before,
            stderr.getvalue().strip(),
        )
        with patch.object(
            M.os, "replace", side_effect=PermissionError("settings denied")
        ):
            error = raises(
                PermissionError,
                lambda: M._register(
                    "pen",
                    {"path": str(target)},
                    {"m2": {"pen": {"weights_path": str(target)}}},
                ),
            )
        check(
            "failed settings publication preserves both existing backend registrations",
            bool(error)
            and settings_path().read_bytes() == before
            and list(settings_path().parent.glob(".settings.json.*.tmp")) == [],
            error,
        )

        print("\n[3] Managed checkout failures cannot become a usable installation")
        grandqc_parent = settings_path().parent / "grandqc"
        spec = M.CATALOG["grandqc"]
        install = grandqc_parent / f"{spec['commit']}-patch{spec['patch_version']}"

        def clone_then_fail(command, **kwargs):
            if command[:3] == ["git", "clone", "--no-checkout"]:
                fixture_repo(Path(command[-1]) / "01_WSI_inference_OPENSLIDE_QC")
            elif "apply" in command:
                raise RuntimeError("compatibility patch rejected")
            return ""

        with patch.object(M, "_command", side_effect=clone_then_fail), redirect_stderr(
            io.StringIO()
        ) as stderr:
            code = M.main(["grandqc"])
        check(
            "a patch failure removes its staged checkout and preserves registration",
            code == 1
            and "patch rejected" in stderr.getvalue()
            and list(grandqc_parent.iterdir()) == []
            and settings_path().read_bytes() == before,
            stderr.getvalue().strip(),
        )

        def clone_fixture(command, **kwargs):
            if command[:3] == ["git", "clone", "--no-checkout"]:
                fixture_repo(Path(command[-1]) / "01_WSI_inference_OPENSLIDE_QC")
            return ""

        supplied = out / "supplied-weights"
        supplied.mkdir()
        for filename in M.CATALOG["weights"]:
            if filename != "pen.pt":
                (supplied / filename).write_bytes(b"bad supplied checkpoint")
        with patch.object(M, "_command", side_effect=clone_fixture), patch.object(
            M, "download", side_effect=AssertionError("network prohibited")
        ), redirect_stderr(io.StringIO()) as stderr:
            code = M.main(["grandqc", "--weights-dir", str(supplied)])
        check(
            "invalid supplied GrandQC weights remove the staged checkout without downloading",
            code == 1
            and "Checksum mismatch" in stderr.getvalue()
            and list(grandqc_parent.iterdir()) == []
            and settings_path().read_bytes() == before,
            stderr.getvalue().strip(),
        )
        install.mkdir()
        marker = install / "leftover"
        marker.write_bytes(b"preserve for inspection")
        with patch.object(
            M, "_command", side_effect=AssertionError("unexpected clone")
        ):
            error = raises(ValueError, M._managed_grandqc)
        check(
            "a leftover checkout without a receipt is refused without overwriting it",
            bool(error)
            and "Incomplete" in error
            and marker.read_bytes() == b"preserve for inspection",
            error,
        )
        shutil.rmtree(install)
        backend = fixture_repo(install / "01_WSI_inference_OPENSLIDE_QC")
        receipt = M.describe_grandqc(backend)
        (install / "pathnd-receipt.json").write_text(json.dumps(receipt))
        (backend / "main.py").write_text("# edited backend\n")
        with patch.object(
            M, "_command", side_effect=AssertionError("unexpected clone")
        ):
            error = raises(ValueError, M._managed_grandqc)
        check(
            "a changed managed script is refused instead of silently repaired",
            bool(error)
            and "files changed" in error
            and (backend / "main.py").read_text() == "# edited backend\n",
            error,
        )
        # Real help subprocess must reject a missing backend import before custom registration.
        custom = fixture_repo(out / "custom-backend")
        (custom / "main.py").write_text(
            "import pathnd_deliberately_absent_backend_dependency\n"
        )
        with redirect_stderr(io.StringIO()) as stderr:
            code = M.main(
                ["grandqc", "--repo", str(custom), "--python", sys.executable]
            )
        check(
            "backend dependency import failure preserves settings and reports the dependency",
            code == 1
            and "pathnd_deliberately_absent_backend_dependency" in stderr.getvalue()
            and settings_path().read_bytes() == before,
            stderr.getvalue().strip(),
        )
        custom.joinpath("models/qc/GrandQC_MPP1.pth").unlink()
        with patch.object(
            M, "_command", side_effect=AssertionError("backend must not launch")
        ):
            error = raises(ValueError, lambda: M.validate_grandqc(custom))
        check(
            "missing artifact checkpoint fails before launching backend code",
            bool(error) and "No supported" in error,
            error,
        )

    print(
        "\n[4] Inference dependency failures are actionable and never clean measurements"
    )
    original_import = builtins.__import__
    for label, exception, required in [
        (
            "missing inference dependency",
            ImportError("no segmentation_models_pytorch"),
            "--install-deps",
        ),
        (
            "incompatible Torch pair",
            RuntimeError("operator torchvision::nms does not exist"),
            "same PyTorch index",
        ),
        (
            "other backend initialization failure",
            RuntimeError("backend initialization failed"),
            "backend initialization failed",
        ),
    ]:

        def import_backend(name, *args, _exception=exception, **kwargs):
            if name == "segmentation_models_pytorch":
                raise _exception
            return original_import(name, *args, **kwargs)

        P._MODEL_CACHE.clear()
        with patch.object(builtins, "__import__", side_effect=import_backend):
            result = P.detect_pen(Image.new("RGB", (32, 32)), weights_path="fixture.pt")
        check(
            f"{label} retains its diagnostic with null pen measurements",
            required in result["pen_error"]
            and result["pen_mask"] is None
            and result["pen_area_fraction"] is None
            and result["pen_area_fraction_raw"] is None
            and not P._MODEL_CACHE,
            result["pen_error"],
        )
finally:
    P._MODEL_CACHE.clear()
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{'=' * 70}\nMODEL FAILURES SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  - {name}")
sys.exit(1 if FAIL else 0)
