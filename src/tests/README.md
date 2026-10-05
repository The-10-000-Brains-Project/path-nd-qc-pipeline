# Regression tests

[Source overview](../README.md) · [Workflow tests](../deploy/verily/README.md#local-verification)

These suites check library, batch, download, reporting and model-setup behavior using synthetic
inputs. They require a repository checkout and the package dependencies. No real slides, cloud
credentials or model downloads are needed. Tests are excluded from installed wheels and release
source archives. Both workflow suites reuse `fake_slide.py` from this directory, resolving
its location from their own source files rather than the working directory or `PYTHONPATH`.

## Run the suites

From the repository root, in your Python environment:

```bash
python -m pip install -e ./src
python src/tests/run_all.py
```

The runner executes every `smoke_*.py` and `test_*.py` file here, the unit tests in
`pathnd_qc/*/tests/`, and both workflow suites, and exits nonzero if any suite fails.
Individual scripts can also be run directly. The download suite starts a localhost HTTP server and
needs permission to bind a local port. The batch suite's process-group checks exercise POSIX
signals; use macOS or Linux to run the full set.

Continuous integration runs this suite and the freeze check on Linux for every push to `main` and
every pull request; see [ci.yml](../../.github/workflows/ci.yml).

## Files

| File | Coverage |
|---|---|
| [run_all.py](run_all.py) | Runs all checkout and workflow suites, retaining failures from each |
| [coverage.ini](coverage.ini) | Line and branch coverage of pipeline and workflow Python, including subprocesses |
| [smoke_normalization.py](smoke_normalization.py) | Reference selection, frozen transforms, numerical invariants and normalization failures |
| [smoke_reader.py](smoke_reader.py) | Physical scaling, bounded reads, corrupt pixels and resource ownership |
| [smoke_tile_metrics.py](smoke_tile_metrics.py) | Refinement, tile drops, measurements and partial-output failures |
| [smoke_artifact_store.py](smoke_artifact_store.py) | Supplied artifacts, atomic writes and lossless packed TIFF pyramids |
| [smoke_grandqc.py](smoke_grandqc.py) | Class geometry and executable synthetic backend success/failure paths |
| [smoke_pen_staining.py](smoke_pen_staining.py) | Torch inference plumbing and staining support/math invariants |
| [smoke_pipeline_paths.py](smoke_pipeline_paths.py) | Orchestration, supplied inputs, physical-scale gates, failure propagation and CLI exits |
| [smoke_config_validation.py](smoke_config_validation.py) | Configuration types, numeric bounds, reference schemas and invalid overrides |
| [smoke_batch_failures.py](smoke_batch_failures.py) | Concurrent workers, corrupt slides, isolated outputs and resumable mixed outcomes |
| [smoke_model_failures.py](smoke_model_failures.py) | Failed model downloads, validation and installation without losing usable assets |
| [smoke_partial_failures.py](smoke_partial_failures.py) | Late read and output-publication failures with retained partial results |
| [smoke_saved_failures.py](smoke_saved_failures.py) | Damaged reports, incomplete statuses and failed summary replacement |
| [fake_slide.py](fake_slide.py) | Synthetic slide pixels, a pyramid TIFF writer and an injected reader |
| [smoke_api.py](smoke_api.py) | Imports, public API, CLI relocation, logging and caller-relative paths |
| [smoke_batch_contract.py](smoke_batch_contract.py) | Manifest merging, resume, summaries, outcomes, cancellation and subprocess cleanup |
| [smoke_download_contract.py](smoke_download_contract.py) | Deadlines, progress, verification, retries and temporary-file cleanup |
| [smoke_report_contract.py](smoke_report_contract.py) | Structured errors, incomplete-run exceptions, partial outputs and mask dependencies |
| [test_throughput.py](test_throughput.py) | Whole-image pen dispatch, child thread budgets and same-name manifest resume |
| [test_unattended.py](test_unattended.py) | GCS cache and closure, tiled pen inference, metadata paths, summary fields, safe child imports and eight concurrent installers |
| [test_external_setup.py](test_external_setup.py) | Model checksums, atomic downloads, custom backends and setup validation |

Synthetic checks verify software behavior; they do not measure model accuracy or verify cloud execution.

## Measure coverage

From the repository root, with the package installed in the active environment:

```bash
python -m pip install "coverage>=7.10"
python -m coverage erase --rcfile=src/tests/coverage.ini
python -m coverage run --rcfile=src/tests/coverage.ini src/tests/run_all.py
python -m coverage combine --rcfile=src/tests/coverage.ini
python -m coverage report --rcfile=src/tests/coverage.ini
python -m coverage html --rcfile=src/tests/coverage.ini
```

Open `htmlcov/index.html` for missed lines and branches. With branch measurement enabled,
the default `Cover` column combines lines and branches; it is not the line-only percentage.
Test code, notebooks, the fold annotator and WDL text are outside this measurement.
The workflow suites exercise the WDL bootstrap and forwarding contracts.

The bucket suite skips its archive case unless `PATHND_TEST_SOURCE_ARCHIVE` names a release
tarball made from this checkout. To include it, first run
`python src/deploy/verily/prepare_release.py --out /new/release-directory`, then set
`PATHND_TEST_SOURCE_ARCHIVE=/new/release-directory/pathnd_qc-0.5.0.tar.gz` before running the suites.
The release directory must not already exist.

Model tests use synthetic inference or executable fixture scripts. They test loading,
coordinates, failure handling and reporting, not biological accuracy of trained weights.
