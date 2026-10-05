# Deployment validation status

[Deployment guide](README.md) · [Verily usage](../../USAGE_VERILY.md)

These results apply to the 0.5.0 prepared release. Documentation updates in the working tree
do not rebuild release artifacts or repeat their model-inference tests.

The local Linux amd64 image builds successfully, including its CPU inference dependencies and
checksum-verified GrandQC checkpoints. Real-checkpoint tests pass for both deployment entry points:

- The installed image adapter completes eight components with networking disabled and empty
  initialization caches. Pen and GrandQC report no errors; artifacts cover 16 tiles.
- The bucket WDL task command verifies and installs the source archive, then completes tissue,
  pen, tile selection and GrandQC artifact detection. This test uses the built model image and
  reuses its GrandQC installation; the minimal Python base image is not a separate tested run.

The workflow adapter tests, bucket tests, both WDL validators and the offline GrandQC image check
pass. **No Verily job has been verified end to end by these checks.** These synthetic integration
checks do not measure scientific or clinical accuracy.

Use [inputs.models-check.example.json](inputs.models-check.example.json) for a focused model run.
Supply a compatible `pen_weights` File. Prepared releases include source-pinned examples and
`model-validation.json`.
