# Third party notices

Lynxx itself is under the MIT License (see `LICENSE`: original work by trickerer01, derivative work Lynxx).

## Desktop app (Electron + frozen Python core)

| Component | License |
|---|---|
| Electron (Chromium, Node.js and their bundled libraries: see `LICENSES.chromium.html` in the app folder) | MIT (Electron); Chromium: BSD-3-Clause and others |
| Python (CPython runtime and standard library in the frozen core) | PSF-2.0 |

The Lynxx core uses the Python standard library only; the app ships no other third-party runtime package. The frozen core
is built with PyInstaller (GPL-2.0-or-later with the bootloader exception, which allows distributing the built program under
its own license). electron-builder and the other build tools are not part of the release files.

## Extensions

Extensions are not shipped with Lynxx: users install them from sources they choose. Their scripts and the
content of the sites they read are under the terms of their authors and of those sites.
