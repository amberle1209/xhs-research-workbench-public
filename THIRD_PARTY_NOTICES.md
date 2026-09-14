# Third-Party Notices

Phase 0 does not copy source code from third-party repositories. Dependencies and local tools
are adopted only through their documented installation mechanisms and remain subject to their
respective licenses.

| Component | License | Fixed source or adoption mode |
| --- | --- | --- |
| [xhs-cli](https://github.com/jackwener/xhs-cli) | Apache-2.0 | Python dependency pinned to commit `3ce71415dc0816ebb4c3f547baf6c08fb3d5cb5a`. |
| [xiaohongshu-downloader](https://github.com/smile7up/xiaohongshu-downloader) | MIT | Reference-only downloader pinned to commit `3fa2d26d8e6af15be47834b4cc590d340cf85972`; no repository code is copied in Phase 0. |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense | Future media retrieval uses its separately installed CLI; it is not vendored. |
| [FFmpeg](https://ffmpeg.org/) | LGPL-2.1-or-later by default; optional GPL components | Future local media processing uses an externally installed binary; it is not bundled. |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | Optional `video` extra pins version 1.2.1 for local transcription; it is not vendored. |

Use of any component in later phases must remain read-only with respect to the Xiaohongshu
platform and comply with the component license and applicable platform terms.

The Chrome extension bundle is built with esbuild. TypeScript, Vitest, jsdom,
and `@types/chrome` are development-only dependencies and are not shipped in
the extension bundle or Python distributions. Their license texts remain
available from their respective package distributions.

The video extra also installs CTranslate2, PyAV and Hugging Face Hub through their Python
distributions, which carry their own license notices. The multilingual small model is downloaded
from `Systran/faster-whisper-small` at revision `536b0662742c02347bc0e980a01041f333bce120`;
model binaries and user media are not included in the wheel or source distribution.
