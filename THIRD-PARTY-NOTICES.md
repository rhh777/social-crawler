# Third-party notices

Original Social Crawler code and documentation are licensed under Apache-2.0.
That license does not replace the licenses or service terms of dependencies,
browser runtimes, bundled assets, or data obtained from third-party platforms.

## Installed dependencies

Python dependencies are declared in `pyproject.toml` and resolved in `uv.lock`.
They retain the licenses distributed with their respective packages.

## Bundled browser libraries

The console bundles Marked and DOMPurify. Their license notices are preserved in
[marked.LICENSE](src/social_crawler/interfaces/web_static/vendor/marked.LICENSE)
and [dompurify.LICENSE](src/social_crawler/interfaces/web_static/vendor/dompurify.LICENSE).
Versions, archive sources and checksums are recorded in
[the vendor manifest](src/social_crawler/interfaces/web_static/vendor/manifest.json).
The console uses an original Social Crawler icon and does not bundle platform
logos or application icons. Platform names are used only to describe
compatibility; the project's license grants no rights to third-party marks.

## Components installed into container images

No third-party binary is redistributed in this repository. The build files below
download or install components at image build time; the resulting image contains
software under licenses other than Apache-2.0, and redistributing that image
means redistributing those components under their own terms.

| Component | Installed by | License / terms |
| --- | --- | --- |
| Debian base packages, `python:3.13-slim-bookworm` | [Dockerfile](Dockerfile) | Respective Debian package licenses |
| Chrome for Testing | [Dockerfile](Dockerfile), pinned version and SHA-256 | [Google Chrome and ChromeOS Additional Terms of Service](https://www.google.com/chrome/terms/) |
| KasmVNC server, `openbox`, `x11-utils` | [install.sh](deploy/docker/kasmvnc/install.sh), pinned release and SHA-256 | KasmVNC is distributed under the GNU GPL v2; `openbox` under GPL-2.0 |
| `fonts-noto-cjk` | [AdsPower image](deploy/docker/adspower/Dockerfile) | SIL Open Font License 1.1 |
| Node.js runtime | [AdsPower image](deploy/docker/adspower/Dockerfile), copied from `node:24-bookworm-slim` | MIT, with bundled component notices |
| `adspower-browser` CLI | [AdsPower image](deploy/docker/adspower/Dockerfile), installed from npm | [MIT](https://github.com/AdsPower/adspower-browser/blob/main/LICENSE) |

The AdsPower image is an optional convenience for self-hosting. The
MIT-licensed CLI may download or start AdsPower browser runtimes and kernels;
those separately obtained products are not covered by the CLI's MIT license or
by this project's Apache-2.0 license. Obtain your own account or license and
review the applicable AdsPower terms before using them.

Project CI builds the application image for verification but does not publish
it. Do not redistribute prebuilt images until the Chrome terms have been
reviewed for the intended distribution, the GPL components' corresponding
source obligations are satisfied, and all project and third-party license and
notice files are included in the image and its distribution materials.
