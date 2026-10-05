# Third-party notices

Path-ND QC uses, downloads or redistributes the third-party models, code and images below.
Each remains under its owner's terms, which are summarized here for convenience; the linked
upstream terms are authoritative. **Several of these terms restrict use to non-commercial
research**, so a deployment that includes them is subject to those restrictions.

## GrandQC (tile artifact detection)

GrandQC is distributed under a non-commercial license, and use is subject to the terms of the
original GrandQC license.

| Item | How Path-ND QC uses it | Terms |
|---|---|---|
| GrandQC source, pinned commit `002688d7` from [cpath-ukk/grandqc](https://github.com/cpath-ukk/grandqc) | Cloned during model setup and in the container image; not vendored in this repository | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) |
| Compatibility patches: `pathnd_qc/external/patches/` and `external/grandqc/patches/` | Modify the GrandQC source above during setup | Adaptations of GrandQC, shared under [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) |
| `GrandQC_MPP1.pth`, `GrandQC_MPP15.pth`, `GrandQC_MPP2.pth` ([Zenodo 14041538](https://zenodo.org/records/14041538)) | Redistributed unmodified in `external/weights/`; also downloaded during setup | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) |
| `Tissue_Detection_MPP10.pth` ([Zenodo 14507273](https://zenodo.org/records/14507273)) | Redistributed unmodified in `external/weights/`; also downloaded during setup | [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) |

Copyright the GrandQC authors (Tolkach Y., Weng Z. and colleagues). Cite:
Weng Z. et al. "GrandQC: a comprehensive solution to quality control problem in digital
pathology." *Nature Communications* (2024). https://doi.org/10.1038/s41467-024-54769-y

## WSISegQC pen model (pen detection)

| Item | How Path-ND QC uses it | Terms |
|---|---|---|
| `pen.pt` from the [WSISegQC](https://github.com/abhijeetptl5/wsisegqc) [model folder](https://drive.google.com/drive/folders/1P3E9kZDM7A7cM06RR47kywvQCL3X0HJz) | Not redistributed; downloaded from the upstream folder during setup and checksum-verified | No license published upstream |

The WSISegQC repository does not publish a license, so all rights remain with its authors.
The model is credited here; this notice does not grant any rights to it. Cite:
Patil A., Jain G., Diwakar H., Sawant J., Bameta T., Rane S., Sethi A. "Semantic Segmentation
Based Quality Control of Histopathology Whole Slide Images." arXiv:2410.03289 (2024).

## SEA-AD example images

Example thumbnails, masks and overlays in the component guides (files under
`pathnd_qc/**/assets/` whose names begin with `H20.33.` or `H21.33.`, including `ref_` files) and the
outputs saved in the demo notebook are derived from slides in the Seattle Alzheimer's Disease
Brain Cell Atlas (SEA-AD) quantitative neuropathology dataset, which is
[openly available on AWS](https://registry.opendata.aws/allen-sea-ad-atlas/).

Image credit: Allen Institute, SEA-AD — https://registry.opendata.aws/allen-sea-ad-atlas/.
Used under the [Allen Institute Terms of Use](https://alleninstitute.org/legal/terms-use/), which
permit non-commercial redistribution and derivative works with attribution under the
[Allen Institute Citation Policy](https://alleninstitute.org/citation-policy/). Cite:
Gabitto M.I., Travaglini K.J. et al. "Integrated multimodal cell atlas of Alzheimer's disease."
*Nature Neuroscience* (2024). https://doi.org/10.1038/s41593-024-01774-5
