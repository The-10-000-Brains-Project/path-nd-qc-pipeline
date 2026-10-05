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
    String docker_image
    Int cpu = 4
    Int memory_gb = 16
    Int disk_gb = 100
  }

  call RunSlide {
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
      cpu = cpu,
      memory_gb = memory_gb,
      disk_gb = disk_gb
  }

  output {
    File report = RunSlide.report
    File summary = RunSlide.summary
    File provenance = RunSlide.provenance
    File results_archive = RunSlide.results_archive
    File log = RunSlide.log
    Boolean complete = RunSlide.complete
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
    cpu: "Starting CPU allocation; measure before scaling batches."
    memory_gb: "Starting RAM allocation; model analyses may need more."
    disk_gb: "Must fit localized inputs, model/source assets, results and the results archive."
  }
}

task RunSlide {
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
    expected_source_sha256: expected_source_sha256
  })

  command <<<
    set -euo pipefail
    python /opt/pathnd/run_workflow.py --request '~{parameters}'
  >>>

  output {
    File report = "report.json"
    File summary = "summary.json"
    File provenance = "provenance.json"
    File results_archive = "results.tar.gz"
    File log = "pipeline.log"
    Boolean complete = read_boolean("complete.txt")
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
