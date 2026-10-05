#!/usr/bin/env bash
# Run in a Verily cloud app terminal. Builds privately and uploads a digest-pinned WDL.
set -euo pipefail
PATHND_SOURCE=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$PATHND_SOURCE"
source "${PATHND_DEPLOY_ENV:-deploy/verily/deploy.env}"
command -v wb >/dev/null
command -v python3 >/dev/null
wb workspace set --id="$PATHND_WORKSPACE_ID"
PATHND_ACTIVE_PROJECT=$(wb gcloud config get-value project)
if [[ "$PATHND_ACTIVE_PROJECT" != "$PATHND_PROJECT" ]]; then
  printf 'Expected project %s, got %s. Stopping.\n' "$PATHND_PROJECT" "$PATHND_ACTIVE_PROJECT" >&2
  exit 1
fi
PATHND_VERSION=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
PATHND_RELEASE="${PATHND_VERSION}-pipeline-$(date -u +%Y%m%dT%H%M%SZ)"
PATHND_BUILD_DIR="$PATHND_SOURCE/dist/deployment/$PATHND_RELEASE"
python3 deploy/verily/prepare_release.py --out "$PATHND_BUILD_DIR"
cd "$PATHND_BUILD_DIR/source"
PATHND_IMAGE="${PATHND_REGION}-docker.pkg.dev/${PATHND_PROJECT}/${PATHND_REPOSITORY}/pathnd-qc:${PATHND_RELEASE}"
if ! wb gcloud artifacts repositories describe "$PATHND_REPOSITORY" \
    --project="$PATHND_PROJECT" --location="$PATHND_REGION" >/dev/null; then
  wb gcloud artifacts repositories create "$PATHND_REPOSITORY" \
    --project="$PATHND_PROJECT" --location="$PATHND_REGION" --repository-format=docker \
    --quiet
fi
wb gcloud builds submit . --project="$PATHND_PROJECT" --region="$PATHND_REGION" \
  --gcs-source-staging-dir="gs://${PATHND_WORKFLOW_BUCKET}/pathnd-qc/cloudbuild/source" \
  --gcs-log-dir="gs://${PATHND_WORKFLOW_BUCKET}/pathnd-qc/cloudbuild/logs" \
  --ignore-file=deploy/verily/gcloudignore --config=deploy/verily/cloudbuild.yaml \
  --substitutions="_IMAGE=${PATHND_IMAGE}" --quiet
PATHND_DIGEST=$(wb gcloud artifacts docker images describe "$PATHND_IMAGE" \
  --project="$PATHND_PROJECT" --format='value(image_summary.digest)')
if [[ ! "$PATHND_DIGEST" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  printf 'No valid image digest returned; workflow not published.\n' >&2
  exit 1
fi
PATHND_PINNED_IMAGE="${PATHND_IMAGE%:*}@${PATHND_DIGEST}"
PATHND_RELEASE_DIR=$(mktemp -d)
trap 'rm -rf "$PATHND_RELEASE_DIR"' EXIT
PATHND_PREFIX="gs://${PATHND_WORKFLOW_BUCKET}/pathnd-qc/${PATHND_RELEASE}"
python3 - "$PATHND_PINNED_IMAGE" "$PATHND_RELEASE_DIR" "$PATHND_BUILD_DIR/release.json" "$PATHND_PREFIX" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
image, out = sys.argv[1], Path(sys.argv[2])
record = json.loads(Path(sys.argv[3]).read_text())
source = Path('deploy/verily/pathnd_qc.wdl').read_text()
# Default only the public workflow input; the task still receives its explicit input.
source = source.replace('    String docker_image\n',
                        '    String docker_image = ' + json.dumps(image) + '\n', 1)
(out / 'pathnd_qc.wdl').write_text(source)
inputs = json.loads((Path(sys.argv[3]).parent / 'inputs.example.json').read_text())
inputs['PathNDQC.docker_image'] = image
inputs['PathNDQC.expected_source_sha256'] = record['source_sha256']
(out / 'inputs.example.json').write_text(json.dumps(inputs, indent=2) + '\n')
(out / 'pathnd_qc_bucket.wdl').write_text(Path('deploy/verily/pathnd_qc_bucket.wdl').read_text())
bucket_inputs = json.loads((Path(sys.argv[3]).parent / 'inputs.bucket.example.json').read_text())
bucket_inputs['PathNDQC.source_archive'] = sys.argv[4] + '/' + record['source_archive']
(out / 'inputs.bucket.example.json').write_text(json.dumps(bucket_inputs, indent=2) + '\n')
(out / 'image.txt').write_text(image + '\n')
record['docker_image'] = image
record['published_workflow_files_sha256'] = {
    name: hashlib.sha256((out / name).read_bytes()).hexdigest()
    for name in ('pathnd_qc.wdl', 'pathnd_qc_bucket.wdl')}
(out / 'release.json').write_text(json.dumps(record, indent=2) + '\n')
PY
wb gcloud storage cp "$PATHND_RELEASE_DIR/pathnd_qc.wdl" \
  "$PATHND_RELEASE_DIR/pathnd_qc_bucket.wdl" "$PATHND_RELEASE_DIR/inputs.bucket.example.json" \
  "$PATHND_RELEASE_DIR/inputs.example.json" "$PATHND_RELEASE_DIR/image.txt" \
  "$PATHND_RELEASE_DIR/release.json" "${PATHND_PREFIX}/"
wb gcloud storage cp "$PATHND_BUILD_DIR"/*.whl "$PATHND_BUILD_DIR"/*.tar.gz \
  "$PATHND_BUILD_DIR"/*.sha256 "${PATHND_PREFIX}/"
printf '\nImage built and workflow files uploaded.\nWDL: %s/pathnd_qc.wdl\nImage: %s\n' \
  "$PATHND_PREFIX" "$PATHND_PINNED_IMAGE"
printf 'In Verily: Workflows → Add workflow → choose this bucket and WDL.\n'
printf 'Then run one representative slide before sharing with collaborators.\n'
