"""Which slides a batch runs, and what each `pathnd-qc` invocation gets.

Three ways in, like HistoQC: a FOLDER (walked for slide extensions), a LIST file (one path per
line, `#` comments, local or cloud URI), or a CSV/TSV manifest (a `slide` column plus optional
`stain`, `bank` and per-slide artifact columns named after `pipeline_spec.ARTIFACTS`:
`tissue_mask`, `pen_mask`, `fold_mask`, `tile_list`, `thumbnail`). Cloud discovery uses explicit
GCS, S3 or Azure object URIs in a list/manifest; `--slide_dir` walks local directories only.

Per-slide artifacts can also come from a TEMPLATE with placeholders, one per artifact flag:
`{slide_id}`, `{slide_dir}`, `{stem}` and `{latest_run}` -- the newest COMPLETED run folder of that
slide under `--out`. `{latest_artifact}` locates the flag's artifact in that run, in either layout:

    --run_fold_detection --tissue_mask "{latest_artifact}"

A manifest column wins over a template. A template that resolves to a missing file is a recorded
`problem`: the job is reported as refused without paying a process start, since `pathnd-qc` would
refuse it at pre-flight anyway.
"""
from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path

from pathnd_qc import pipeline_spec as spec
from pathnd_qc._fs import slide_id
from pathnd_qc.batch import runs
from pathnd_qc import output_layout

SLIDE_EXTS = (".svs", ".tif", ".tiff", ".ndpi", ".scn", ".mrxs", ".bif", ".vms", ".vmu")
SLIDE_COLUMNS = ("slide", "slide_path", "path")


@dataclass
class Job:
    slide: str                                  # exactly what `pathnd-qc --slide` will see
    slide_id: str
    key: str                                    # unique within the batch (see `build_jobs`)
    stain: str | None = None
    bank: str | None = None
    supplied: dict = field(default_factory=dict)   # artifact name -> resolved path
    problems: list = field(default_factory=list)   # why this job cannot be launched
    output_subdir: str = "."                    # parent relative to --out; populated by folder discovery


def slide_id_of(path: str) -> str:
    """The pipeline's slide id (`pathnd_qc._fs.slide_id`): the basename minus its extension."""
    return slide_id(path)


def _norm(path: str) -> str:
    """Resolve local symlink aliases to one identity; preserve remote URIs."""
    path = str(path).strip()
    return path if "://" in path else os.path.realpath(os.path.expanduser(path))


def _file_identity(path: str):
    """Existing local aliases share identity, including case aliases and hard links."""
    if "://" not in path:
        try:
            stat = os.stat(path)
            if stat.st_ino:
                return stat.st_dev, stat.st_ino
        except OSError:
            pass
    return path


@contextmanager
def _manifest_file(path: Path):
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            yield handle
    except UnicodeError as exc:
        raise ValueError(f"--slides: {path}: expected a UTF-8 manifest/list: {exc}") from exc


def discover(slides_file: str | None = None, slide_dir: str | None = None,
             exts: tuple = SLIDE_EXTS, recursive: bool = True, exclude_dir: str | None = None) -> list[dict]:
    """-> rows of {"slide": path, ...columns}. Local paths canonical, order deterministic."""
    rows: list[dict] = []
    if slide_dir:
        root = Path(slide_dir).expanduser().absolute()
        if not root.is_dir():
            raise ValueError(f"--slide_dir: not a directory: {root}")
        exts_l = tuple(e.lower() for e in exts)
        excluded = Path(exclude_dir).expanduser().resolve() if exclude_dir else None
        if excluded is not None and root.resolve().is_relative_to(excluded):
            raise ValueError("--out must not be the input folder or an ancestor of --slide_dir")
        it = root.rglob("*") if recursive else root.glob("*")
        for p in sorted(it):
            if excluded is not None and p.resolve().is_relative_to(excluded):
                continue
            if p.is_file() and p.suffix.lower() in exts_l:
                rows.append({"slide": _norm(p), "_output_subdir": str(p.relative_to(root).parent)})
    if slides_file:
        f = Path(slides_file).expanduser()
        if not f.is_file():
            raise ValueError(f"--slides: file not found: {f}")
        if f.suffix.lower() in (".csv", ".tsv"):
            with _manifest_file(f) as handle:
                reader = csv.DictReader(handle, delimiter="\t" if f.suffix.lower() == ".tsv" else ",")
                cols = [c for c in (reader.fieldnames or []) if c and c.strip().lower() in SLIDE_COLUMNS]
                if not cols:
                    raise ValueError(f"--slides: {f.name} has no slide column (one of {SLIDE_COLUMNS}); "
                                     f"columns: {reader.fieldnames}")
                col = cols[0]
                for r in reader:
                    slide = (r.get(col) or "").strip()
                    if not slide or slide.startswith("#"):
                        continue
                    row = {k.strip().lower(): (v or "").strip() for k, v in r.items()
                           if k and not k.strip().startswith("_")}
                    row["slide"] = _norm(slide)
                    rows.append(row)
        else:
            with _manifest_file(f) as handle:
                for line in handle:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        rows.append({"slide": _norm(line)})
    if not slides_file and not slide_dir:
        raise ValueError("nothing to run: pass --slides FILE and/or --slide_dir DIR")
    unique: dict = {}
    for r in rows:
        merged = unique.setdefault(_file_identity(r["slide"]), {"slide": r["slide"]})
        for key, value in r.items():
            if not value or key == "slide":
                continue
            if key == "_output_subdir":
                merged.setdefault(key, value)  # first discovered occurrence owns placement
                continue
            if key in spec.ARTIFACTS:
                value = _norm(value)
            same_artifact = (key in spec.ARTIFACTS and merged.get(key)
                             and _file_identity(merged[key]) == _file_identity(value))
            if same_artifact:
                continue
            if merged.get(key) and merged[key] != value:
                merged.setdefault("_problems", []).append(
                    f"conflicting manifest values for {key}: {merged[key]!r} and {value!r}")
            else:
                merged[key] = value
    return list(unique.values())


def _resolve_template(template: str, job: Job, out_dir: str | None,
                      latest_runs: dict | None = None, artifact: str | None = None) -> tuple[str | None, str | None]:
    """-> (path, problem). `{latest_run}` needs a completed run of this slide under `out_dir`."""
    slide_dir = os.path.dirname(job.slide) if "://" not in job.slide else job.slide.rsplit("/", 1)[0]
    values = {"slide_id": job.slide_id, "slide_dir": slide_dir, "stem": job.slide_id,
              "latest_run": None, "latest_artifact": None}
    if "{latest_run}" in template or "{latest_artifact}" in template:
        if latest_runs is None:
            latest = runs.latest_run(out_dir, job.slide) if out_dir else None
        else:
            latest = (latest_runs.get(job.slide) or {}).get("run_dir")
        if latest is None:
            return None, (f"{template!r}: no completed run of this slide under {out_dir} to take "
                          f"{{latest_run}} from")
        values["latest_run"] = str(latest)
        saved = output_layout.existing_artifact(latest, artifact, job.slide_id)
        if "{latest_artifact}" in template:
            if saved is None:
                return None, f"{template!r}: newest completed run has no saved {artifact}: {latest}"
            values["latest_artifact"] = str(saved)
    try:
        path = _norm(template.format(**values))
        # Resolve artifacts from the run root and masks/ as well as the image/data subdirectories.
        if values["latest_run"] and saved is not None and not Path(path).exists():
            candidates = output_layout.artifact_candidates(values["latest_run"], artifact, job.slide_id)
            if path in {_norm(candidate) for candidate in candidates}:
                path = _norm(saved)
        return path, None
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        return None, f"{template!r}: invalid template: {exc} (valid: slide_id, slide_dir, stem, latest_run, latest_artifact)"


def build_jobs(rows: list[dict], templates: dict | None = None, out_dir: str | None = None,
               stain: str | None = None, bank: str | None = None) -> list[Job]:
    """Rows -> Jobs: a unique key each, per-slide stain/bank, artifacts resolved and checked."""
    templates = {k: v for k, v in (templates or {}).items() if v}
    unknown = set(templates) - set(spec.ARTIFACTS)
    if unknown:
        raise ValueError(f"unknown artifact(s) {sorted(unknown)}; valid: {sorted(spec.ARTIFACTS)}")
    ids: dict[str, int] = {}
    for r in rows:
        sid = slide_id_of(r["slide"])
        ids[sid] = ids.get(sid, 0) + 1
    jobs: list[Job] = []
    latest_runs = (runs.scan_runs(out_dir, only_state="completed") if out_dir
                   and any("{latest_run}" in t or "{latest_artifact}" in t for t in templates.values()) else {})
    for r in rows:
        sid = slide_id_of(r["slide"])
        # Basenames collide across banks (PART/42053.svs vs SEA-AD/42053.svs): the key stays
        # readable and becomes unique with a hash of the full path only when it has to.
        key = sid if ids[sid] == 1 else f"{sid}~{hashlib.sha1(r['slide'].encode()).hexdigest()[:8]}"
        job = Job(slide=r["slide"], slide_id=sid, key=key,
                  stain=(r.get("stain") or stain) or None, bank=(r.get("bank") or bank) or None,
                  problems=list(r.get("_problems") or []), output_subdir=r.get("_output_subdir",
                      f"sources/{hashlib.sha256(_norm(r['slide']).encode()).hexdigest()}"))
        for name in spec.ARTIFACTS:
            value = (r.get(name) or "").strip()
            if value:
                path = _norm(value)
            elif name in templates:
                path, problem = _resolve_template(templates[name], job, out_dir, latest_runs, name)
                if problem:
                    job.problems.append(f"{spec.ARTIFACTS[name]['flag']}: {problem}")
                    continue
            else:
                continue
            if "://" not in path and not os.path.exists(path):
                job.problems.append(f"{spec.ARTIFACTS[name]['flag']}: file not found: {path}")
                continue
            job.supplied[name] = path
        jobs.append(job)
    # Reject explicitly colliding placements; manifest-only sources have stable path identities.
    destinations: dict[str, list[Job]] = {}
    for job in jobs:
        destination = output_layout.job_output_parent(out_dir or ".", job.output_subdir) / f"{job.slide_id}_output"
        destinations.setdefault(str(destination), []).append(job)
    for destination, colliding in destinations.items():
        if len(colliding) > 1:
            for job in colliding:
                job.problems.append(f"output folder collision: {destination}; different slides share this name. "
                                    "Rename the slides or use --slide_dir with separate subfolders")
    return jobs


def manifest_hash(jobs: list[Job]) -> str:
    """Hash the ordered slide list to identify the batch inputs."""
    h = hashlib.sha256()
    for j in jobs:
        h.update(j.slide.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()
