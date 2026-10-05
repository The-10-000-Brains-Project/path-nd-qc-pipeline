"""Output locations shared by the pipeline, batch tools and workflow adapter.

Runs live in <relative input parent>/<slide_id>_output/<readable UTC timestamp>/,
under batch_<batch_id>/ for batches or directly under --out for single slides.
Discovery also accepts flat run folders without moving their contents.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from pathnd_qc._fs import slide_id

BATCHES_DIR = "batches"
RUN_NAME = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_UTC(?:_\d+)?")
ARTIFACT_FILES = {
    "tissue_mask": "images/{slide_id}_tissue_mask.png",
    "fold_mask": "images/{slide_id}_fold_mask.png",
    "pen_mask": "images/{slide_id}_pen_mask.png",
    "tissue_mask_20x": "images/{slide_id}_tissue_mask_20x.tif",
    "tile_list": "data/{slide_id}_tile_list.json",
    "tile_records": "data/{slide_id}_tile_records.json",
    "normalized_image": "images/{slide_id}_normalized.png",
    "blur_overlay": "images/{slide_id}_blur_overlay.png",
    "artifact_overlay": "images/{slide_id}_artifact_overlay.png",
}


def artifact_path(run_dir, name: str, slide_id: str) -> Path:
    return Path(run_dir) / ARTIFACT_FILES[name].format(slide_id=slide_id)


def artifact_candidates(run_dir, name: str, slide_id: str) -> tuple[Path, ...]:
    """Return artifact locations in subdirectories and at the run root, in search order."""
    if name not in ARTIFACT_FILES:
        return ()
    current = artifact_path(run_dir, name, slide_id)
    old_masks = ((Path(run_dir) / "masks" / current.name,)
                 if name in {"tissue_mask", "fold_mask", "pen_mask", "tissue_mask_20x"} else ())
    return (current, *old_masks, Path(run_dir) / current.name)


def existing_artifact(run_dir, name: str, slide_id: str) -> Path | None:
    """Find an artifact within this run’s supported output locations."""
    for candidate in artifact_candidates(run_dir, name, slide_id):
        if candidate.is_file():
            return candidate
    return None


def job_output_parent(out_dir, relative_parent=".") -> Path:
    """Resolve a mirrored parent beneath --out, refusing traversal and escaping symlinks."""
    root = Path(out_dir).expanduser().resolve()
    relative = Path(relative_parent)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("output_subdir must be a relative path inside --out")
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("output_subdir resolves outside --out")
    return target


def create_run_dir(base, slide_path: str) -> Path:
    """Create a dated run in <slidename>_output; refuse to mix different slide sources."""
    from pathnd_qc import artifact_store as store

    base = Path(base).resolve()
    source = slide_path if "://" in slide_path else str(Path(slide_path).expanduser().resolve())
    parent = base / f"{slide_id(source)}_output"
    if not parent.resolve().is_relative_to(base):
        raise ValueError(f"--out: slide output folder resolves outside {base}: {parent}")
    try:
        parent.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        try:
            owner = json.loads((parent / "slide.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"--out: {parent} already exists without a readable slide.json; "
                             "choose a fresh output location") from exc
        if not isinstance(owner, dict) or owner.get("slide_path") != source:
            raise ValueError(f"--out: output folder collision at {parent}; it belongs to a different slide. "
                             "Rename the slide or use a separate output parent")
    except OSError as exc:
        raise ValueError(f"--out: cannot create slide output folder {parent}: {exc}") from exc
    else:
        try:
            store.save_json({"slide_path": source}, parent / "slide.json")
        except OSError as exc:
            raise ValueError(f"--out: cannot record slide identity in {parent}: {exc}") from exc
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S_UTC", time.gmtime())
    n = 1
    while True:
        folder = parent / (stamp if n == 1 else f"{stamp}_{n}")
        try:
            folder.mkdir(parents=True, exist_ok=False)
            return folder
        except FileExistsError:
            n += 1
        except OSError as exc:
            raise ValueError(f"--out: cannot create the run folder {folder}: {exc}") from exc


def iter_run_dirs(out_dir):
    """Find nested slide runs and flat run folders."""
    root = Path(out_dir)
    folders = {p.parent for p in root.rglob("*_status.json") if is_run_dir(root, p.parent)}
    yield from sorted(folders)


def iter_report_paths(out_dir):
    root = Path(out_dir)
    yield from sorted(p for p in root.rglob("*_report.json") if is_run_dir(root, p.parent))


def is_run_dir(out_dir, folder) -> bool:
    """Check supported run-folder shapes and containment, resolving symlinks."""
    try:
        parts = Path(folder).resolve().relative_to(Path(out_dir).resolve()).parts
    except ValueError:
        return False
    return ((len(parts) >= 2 and parts[-2].endswith("_output") and bool(RUN_NAME.fullmatch(parts[-1])))
            or (len(parts) == 1 and parts[0] not in {"slides", BATCHES_DIR, "_batch"})
            or (len(parts) == 3 and parts[0] == "slides"))


def write_output_guide(out_dir) -> None:
    """Static guide, identical across workers; preserve an existing user-written README."""
    from pathnd_qc import artifact_store as store

    path = Path(out_dir) / "README.txt"
    if path.exists():
        # Update only the recognized generated-guide header, preserving user notes.
        if not path.read_text(encoding="utf-8").startswith(("Path-ND results\n\n1. Open slides/",
                                                         "Path-ND results\n\n1. Folder inputs",
                                                         "Path-ND results\n\n1. Batch results live in batch_")):
            return
    with store.atomic_write(path) as tmp:
        tmp.write_text(
            "Path-ND results\n\n"
            "1. Batch results live in batch_<batch_id>/. Open its index.html to choose a slide.\n"
            "   Inside a batch, folder inputs mirror subfolders; slide lists use the batch root.\n"
            "   Single-slide results live directly here in <slidename>_output/.\n"
            "2. Choose a dated run (UTC), then open index.html in a browser.\n"
            "   images/: segmentation masks and visual review; data/: tile details.\n"
            "   The report JSON contains measurements, errors and pipeline Git provenance.\n"
            "3. results.csv lists this batch's job outcomes; summary.csv includes its saved runs.\n"
            "   Reuse --batch_id to resume in place; a new ID creates an independent batch.\n\n"
            "Only requested, successfully saved artifacts appear. Supplied inputs are not copied.\n"
            "Completed processing does not imply acceptable slide quality; consult the report.\n"
            "Older flat slide folders and _batch/ folders are retained as originally written.\n",
            encoding="utf-8")
