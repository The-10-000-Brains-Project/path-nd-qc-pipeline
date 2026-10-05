version 1.0

# Run Path-ND QC on one slide per invocation; CSV submissions repeat this workflow.
workflow PathNDQC {
  input {
    File? slide
    String slide_uri = ""
    String stain = ""
    Array[String] components = ["tissue_segmentation", "fold_detection", "pen_detection", "staining_quality", "focus", "tile_selection", "tile_metrics", "tile_artifacts", "stain_normalization"]
    File? metadata
    Array[File] metadata_files = []
    Array[String] metadata_uris = []
    String metadata_key = ""
    Boolean no_metadata = false
    String bank = ""
    File? config_file
    String norm_method = ""
    File? pen_weights
    String grandqc_repo = ""
    String grandqc_python = ""
    File? thumbnail
    File? tissue_mask
    File? fold_mask
    File? pen_mask
    File? tile_list
    Boolean no_model_download = false
    Boolean no_save_artifacts = false
    Boolean quiet = false
    Boolean s3_anonymous = false
    Boolean azure_anonymous = false
    String expected_pipeline_version = "0.5.0"
    String expected_source_sha256 = ""
    String docker_image = "docker.io/library/python@sha256:9c47360a2a0355e2da18516d0b1c2126ec22c195d2185e97347c9d98398c5bef"
    File source_archive
    String source_sha256
    Int cpu = 4
    Int memory_gb = 16
    Int disk_gb = 100
  }

  call RunSlideFromBucket {
    input:
      slide = slide,
      slide_uri = slide_uri,
      stain = stain,
      components = components,
      metadata = metadata,
      metadata_files = metadata_files,
      metadata_uris = metadata_uris,
      metadata_key = metadata_key,
      no_metadata = no_metadata,
      bank = bank,
      config_file = config_file,
      norm_method = norm_method,
      pen_weights = pen_weights,
      grandqc_repo = grandqc_repo,
      grandqc_python = grandqc_python,
      thumbnail = thumbnail,
      tissue_mask = tissue_mask,
      fold_mask = fold_mask,
      pen_mask = pen_mask,
      tile_list = tile_list,
      no_model_download = no_model_download,
      no_save_artifacts = no_save_artifacts,
      quiet = quiet,
      s3_anonymous = s3_anonymous,
      azure_anonymous = azure_anonymous,
      expected_pipeline_version = expected_pipeline_version,
      expected_source_sha256 = expected_source_sha256,
      docker_image = docker_image,
      source_archive = source_archive,
      source_sha256 = source_sha256,
      cpu = cpu,
      memory_gb = memory_gb,
      disk_gb = disk_gb
  }

  output {
    File report = RunSlideFromBucket.report
    File summary = RunSlideFromBucket.summary
    File provenance = RunSlideFromBucket.provenance
    File results_archive = RunSlideFromBucket.results_archive
    File log = RunSlideFromBucket.log
    Boolean complete = RunSlideFromBucket.complete
    File setup_log = RunSlideFromBucket.setup_log
    File environment_file = RunSlideFromBucket.environment_file
    File bootstrap_info = RunSlideFromBucket.bootstrap_info
  }

  meta {
    description: "Path-ND QC 0.5.0: modular whole-slide QC with explicit metadata, reusable artifacts and required completion of every selected analysis."
  }
  parameter_meta {
    slide: "Optional engine-localized self-contained WSI File (normally gs:// on Verily). Supply slide OR slide_uri."
    slide_uri: "Alternative gs://, s3://, az:// or abfs[s]:// object URI read by the pipeline. Requires provider credentials in the task; not localized or content-tracked by Cromwell."
    stain: "Actual stain override, or empty to use explicit metadata. Fold routing: Hirano/LFB/LFB-H&E to d; others to stain2; F_line enabled by default."
    components: "Requested components; default all nine. config_file components.<name>=false excludes any component, including explicit selections. At least one must remain. Select enabled producers or supply required artifacts."
    metadata: "Single metadata File; searched first, before metadata_files and metadata_uris."
    metadata_files: "Explicit localized CSV Files, searched in order. No automatic bank lookup."
    metadata_uris: "Additional explicit cloud metadata URIs, searched after localized CSVs. Credentials must exist in the task."
    metadata_key: "Optional common key column for all metadata CSVs; requires at least one file/URI."
    no_metadata: "Explicit opt-out; incompatible with metadata paths or metadata_key. No paths also skips lookup."
    bank: "Optional bank for normalization reference selection; does not choose metadata files."
    config_file: "JSON override File, including m2.folds F_line settings. See config.folds.example.json. M2 defaults to 8 um/px. Normalization needs suitable references; paths inside JSON are not localized."
    norm_method: "Empty uses config; otherwise macenko or reinhard."
    pen_weights: "Compatible WSISegQC pen.pt File. Optional; missing default assets are installed automatically unless no_model_download is true."
    grandqc_repo: "Optional GrandQC inference-directory path already inside the image/task. Empty uses managed configuration."
    grandqc_python: "Backend Python executable override already inside the image/task; empty uses the configured interpreter."
    thumbnail: "Optional analysis image File (.png/.npy), prepared at the configured M2 scale."
    tissue_mask: "Optional nonempty tissue mask File (.png/.npy)."
    fold_mask: "Optional fold mask File (.png/.npy); an empty detection mask is valid."
    pen_mask: "Optional pen mask File (.png/.npy); an empty detection mask is valid."
    tile_list: "Optional tile-list JSON File on the configured M3 plane."
    no_model_download: "Use existing model assets only; do not download missing models."
    no_save_artifacts: "Suppress generated masks/images/tile files; retain report, status and workflow records."
    quiet: "Reduce pipeline logging; errors, reports and workflow summary remain available."
    s3_anonymous: "Enable unsigned access for public S3 objects. False leaves ambient credentials/settings unchanged."
    azure_anonymous: "Enable public Azure blob access; account configuration may still be required."
    expected_pipeline_version: "Exact package version required before processing; default 0.5.0."
    expected_source_sha256: "Exact pipeline source fingerprint; release preparation pins this default to the frozen package."
    docker_image: "Image used by this task. For the image workflow select a complete 0.5.0 pipeline image; prefer a digest pin."
    source_archive: "Current source archive from prepare_release.py, staged as a workflow File. No historical archive default."
    source_sha256: "Required full SHA256 of source_archive, checked before extraction or installation."
    cpu: "Starting CPU allocation; measure before scaling batches."
    memory_gb: "Starting RAM allocation; model analyses may need more."
    disk_gb: "Must fit localized inputs, model/source assets, results and the results archive."
  }
}

task RunSlideFromBucket {
  input {
    File? slide
    String slide_uri
    String stain
    Array[String] components
    File? metadata
    Array[File] metadata_files
    Array[String] metadata_uris
    String metadata_key
    Boolean no_metadata
    String bank
    File? config_file
    String norm_method
    File? pen_weights
    String grandqc_repo
    String grandqc_python
    File? thumbnail
    File? tissue_mask
    File? fold_mask
    File? pen_mask
    File? tile_list
    Boolean no_model_download
    Boolean no_save_artifacts
    Boolean quiet
    Boolean s3_anonymous
    Boolean azure_anonymous
    String expected_pipeline_version
    String expected_source_sha256
    String docker_image
    File source_archive
    String source_sha256
    Int cpu
    Int memory_gb
    Int disk_gb
  }

  # JSON transports values without interpolating user input into shell code.
  File parameters = write_json(object {
    slide: slide,
    slide_uri: slide_uri,
    stain: stain,
    components: components,
    metadata: metadata,
    metadata_files: metadata_files,
    metadata_uris: metadata_uris,
    metadata_key: metadata_key,
    no_metadata: no_metadata,
    bank: bank,
    config_file: config_file,
    norm_method: norm_method,
    pen_weights: pen_weights,
    grandqc_repo: grandqc_repo,
    grandqc_python: grandqc_python,
    thumbnail: thumbnail,
    tissue_mask: tissue_mask,
    fold_mask: fold_mask,
    pen_mask: pen_mask,
    tile_list: tile_list,
    no_model_download: no_model_download,
    no_save_artifacts: no_save_artifacts,
    quiet: quiet,
    s3_anonymous: s3_anonymous,
    azure_anonymous: azure_anonymous,
    expected_pipeline_version: expected_pipeline_version,
    expected_source_sha256: expected_source_sha256,
    docker_image: docker_image,
    source_archive: source_archive,
    source_sha256: source_sha256
  })

  command <<<
    set -euo pipefail
    exec > >(tee setup.log) 2>&1
    export PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
    export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
    export PATHND_DATA_DIR=/opt/pathnd-models DEBIAN_FRONTEND=noninteractive
    python - '~{parameters}' <<'PATHND_BOOTSTRAP'
    import hashlib
    import json
    import os
    from pathlib import Path
    import re
    import subprocess
    import sys
    import tarfile
    import tomllib

    def prepare_source(params):
        expected = params["source_sha256"]
        if not isinstance(expected, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected):
            raise ValueError("source_sha256 must be a 64-character SHA256 hex digest")
        archive_path = Path(params["source_archive"])
        with archive_path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected.lower():
            raise ValueError("Source archive checksum mismatch; refusing to install")
        destination = Path("source").resolve()
        destination.mkdir()
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(destination, filter="data")
        # Accept a standard sdist root; never assume a particular release folder name.
        roots = [p for p in destination.iterdir() if p.is_dir() and (p / "pyproject.toml").is_file()]
        if len(roots) != 1 or len(list(destination.iterdir())) != 1:
            raise ValueError("Source archive must have exactly one project root")
        root = roots[0]
        for required in ("build_hooks.py", "deploy/verily/run_workflow.py", "pathnd_qc/__init__.py"):
            if not (root / required).is_file():
                raise ValueError("Source archive is missing " + required)
        version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
        if version != params.get("expected_pipeline_version", "0.5.0"):
            raise ValueError("Source archive pipeline version does not match expected_pipeline_version")
        return root

    def install_source(root, params):
        # Every component uses the standard package; selection controls execution and asset setup.
        # Config and the registry use only the standard library, so the archived package's
        # selection policy can run before dependency installation or model downloads.
        sys.path.insert(0, str(root.resolve()))
        if params.get("expected_source_sha256"):
            from pathnd_qc._provenance import source_sha
            if source_sha(root / "pathnd_qc") != params["expected_source_sha256"]:
                raise ValueError("Source archive fingerprint differs from expected_source_sha256")
        if params.get("config_file"):
            os.environ["PATHND_CONFIG"] = str(Path(params["config_file"]).resolve())
        else:
            os.environ.pop("PATHND_CONFIG", None)
        from pathnd_qc.config import load, cfg
        load(force=True)
        from pathnd_qc.pipeline_spec import COMPONENTS, select_run_set
        selected = params.get("components", list(COMPONENTS))
        if not isinstance(selected, list) or not selected or any(not isinstance(x, str) or x not in COMPONENTS for x in selected):
            raise ValueError("Select a non-empty list of known pipeline components")
        selected = select_run_set(set(selected))
        if not selected:
            raise ValueError("nothing to run: enable at least one requested component in configuration")
        pen_weights = params.get("pen_weights") or cfg("m2.pen.weights_path")
        if "pen_detection" in selected and (params.get("pen_weights") or params.get("no_model_download")) and not (pen_weights and Path(pen_weights).is_file()):
            raise ValueError("Bucket workflow pen_detection requires pen_weights or a configured checkpoint")
        if "stain_normalization" in selected and not params.get("config_file"):
            raise ValueError("normalization requires config_file with suitable references")
        subprocess.run(["apt-get", "update"], check=True)
        subprocess.run(["apt-get", "install", "-y", "--no-install-recommends",
                        "ca-certificates", "libgomp1", "libopenslide0", "git"], check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "torch>=2.6,<3", "torchvision",
                        "--index-url", "https://download.pytorch.org/whl/cpu"], check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", str(root)], check=True)
        subprocess.run([sys.executable, "-m", "pip", "check"], check=True)

    def main(params_path):
        params_path = str(Path(params_path).resolve())
        params = json.loads(Path(params_path).read_text())
        root = prepare_source(params)
        install_source(root, params)
        with Path("environment.txt").open("w") as stream:
            subprocess.run([sys.executable, "-m", "pip", "freeze"], stdout=stream, check=True)
        Path("/opt/pathnd-requirements.txt").write_text(Path("environment.txt").read_text())
        Path("bootstrap.json").write_text(json.dumps({
            "source_sha256": params["source_sha256"].lower(),
            "docker_image": params["docker_image"],
            "expected_pipeline_version": params.get("expected_pipeline_version", "0.5.0"),
            "python": sys.version,
            "dependency_versions": "environment.txt",
        }, indent=2) + "\n")
        os.execv(sys.executable, [sys.executable, str(root / "deploy/verily/run_workflow.py"),
                                 "--request", params_path])

    if __name__ == "__main__":
        main(sys.argv[1])
    PATHND_BOOTSTRAP
  >>>

  output {
    File report = "report.json"
    File summary = "summary.json"
    File provenance = "provenance.json"
    File results_archive = "results.tar.gz"
    File log = "pipeline.log"
    Boolean complete = read_boolean("complete.txt")
    File setup_log = "setup.log"
    File environment_file = "environment.txt"
    File bootstrap_info = "bootstrap.json"
  }

  runtime {
    docker: docker_image
    cpu: cpu
    memory: "~{memory_gb} GB"
    disks: "local-disk ~{disk_gb} SSD"
    bootDiskSizeGb: 30
    preemptible: 0
    maxRetries: 0
  }
}
