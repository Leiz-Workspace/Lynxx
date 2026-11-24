# Third party notices

Lynxx itself is under the MIT License (see `LICENSE`: original work by trickerer01, derivative work Lynxx).
The release bundles contain or download the components below. Licenses were read from the installed package metadata
(2026-11-17); the full license texts ship inside each package (`*.dist-info/licenses`, `node_modules/*/LICENSE`).

## Downloaded at run time, not distributed with Lynxx

| Component | License | Where |
|---|---|---|
| WD ViT Tagger v3 (`SmilingWolf/wd-vit-tagger-v3`, pinned revision `7f6b584d`), `model.onnx` + `selected_tags.csv` | Apache-2.0 | Downloaded by the model server (or `python -m lynxx.gender_model install`) from Hugging Face into the data folder, sha256 checked |

## Desktop app (AppImage: Electron + frozen Python core)

| Component | License |
|---|---|
| Electron (Chromium, Node.js and their bundled libraries: see `LICENSES.chromium.html` in the app folder) | MIT (Electron); Chromium: BSD-3-Clause and others |
| Python (CPython runtime in the frozen core) | PSF-2.0 |
| requests | Apache-2.0 |
| urllib3 | MIT |
| idna | BSD-3-Clause |
| certifi | MPL-2.0 |
| charset-normalizer | MIT |
| PySocks | BSD |
| beautifulsoup4, soupsieve | MIT |
| fake-useragent | Apache-2.0 |
| yarl, multidict, propcache | Apache-2.0 |
| anthropic (extension generator, AI mode) | Apache-2.0 |
| pydantic, pydantic-core, anyio, jiter, h11, annotated-types, docstring-parser | MIT |
| sniffio | MIT OR Apache-2.0 |
| typing_extensions | PSF-2.0 |

The frozen core is built with PyInstaller (GPL-2.0-or-later with the bootloader exception, which allows distributing the
built program under its own license).

## Model server bundle and Docker image

| Component | License |
|---|---|
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| onnxruntime (CPU) / onnxruntime-gpu | MIT |
| Pillow | MIT-CMU |
| requests, yarl and their dependencies (urllib3, idna, certifi, charset-normalizer, multidict, propcache) | see above |
| Python (CPython runtime in the bundle) | PSF-2.0 |
| onnxruntime-directml (Windows `directml` bundle) | MIT; `DirectML.dll` inside it comes with Microsoft's own license terms, shipped in the package |
| FFmpeg (Debian `ffmpeg` package, Docker image only: frames of videos for `/v1/frames`) | GPL-2.0-or-later (Debian build; FFmpeg itself LGPL-2.1-or-later); run as a separate program, never linked; sources: Debian |
| NVIDIA CUDA / cuDNN libraries (`nvidia-*` pip packages, GPU installs only) | NVIDIA software license agreements; installed from PyPI by the user into a Python environment or a locally built GPU Docker image; Lynxx publishes no bundle or image containing them |
