# Windows build integrity checks

Run `build.bat --check` first. This checks inputs without packing, compiling,
installing anything, or changing the previous output. A successful check is not
an antivirus result or proof that the computer is clean. Do not publish builds
from an unresolved security incident; use a trusted build environment.

On failure, the batch keeps the error visible and waits for **Enter** before
exiting, so screen-reader users can review the console. Automated callers can
set `BT_BUILD_NO_PAUSE=1` to skip only this wait; all checks still run and a
failure still returns exit code 1. Successful builds/checks do not wait.

## What the build checks

- Before executing Python, the launcher requires a valid Python Software
  Foundation Authenticode signature. It selects `python.exe` on PATH, or the
  absolute path in `BT_BUILD_PYTHON`. An unverified local `.venv` is not selected
  automatically. The chosen interpreter needs the game's build dependencies.
- The guard runs with `-I -S` and uses only the standard library. It does not
  import the game or execute helper programs. It checks selected Python package
  directories before the packer/compiler can import them.
- Input files and directories with `$` anywhere in their names are excluded
  without reading/copying their contents or deleting/renaming the originals.
  This applies to asset packing, DLL/HRTF copies, runtime package copies, and
  the cyal DLL plugin. A `$` name does **not** prove a file is malware.
- Files that will actually be used still undergo the integrity checks.
  `.blocked` quarantine files, links and junctions in included paths, the known
  replacement SHA-256, and both known AutoPlay markers still stop the build.
  The compiled distribution and final package remain strict: a `$` file or
  directory appearing there is an error, not something silently shipped.
- `ffmpeg.exe` and `oalinst.exe` must match the size and SHA-256 in
  `build_binary_manifest.json` and have a Windows PE header. A renamed ZIP is not
  accepted as an EXE. Missing or modified helpers stop the build.
- Broad asset/runtime trees and the compiled distribution are checked. The
  final package is checked again, including helper hashes and required files.
  Raw `data/` must not ship; `tools/pack_data.py` still produces encrypted
  `sounds.dat` directly in the staging directory.
- Failed compilation or copying stops immediately. Files are assembled in
  `Beyond Tournament.pending`; only a checked package replaces the generated
  `Beyond Tournament` output. Existing output is not an archive for personal
  files. The generated Nuitka distribution is removed only after promotion.

If staging already exists, the next attempt stops. Review it and move needed
files elsewhere before removing that exact generated directory manually. The
check never automatically restores, renames, or deletes suspicious originals.
The existing generated output/distribution is also checked strictly before
replacement or cleanup; `$` leftovers there require review to avoid deleting
unresolved originals. Excluding inputs does not certify the host as clean.

## Approved helper downloads

The manifest records the archive URL, archive hash, archive member, and extracted
EXE hash. Helper EXEs remain ignored by Git and must be provisioned separately.

- FFmpeg 8.1.2 full static build: [Gyan Windows builds](https://www.gyan.dev/ffmpeg/builds/),
  linked from [FFmpeg's download page](https://www.ffmpeg.org/download.html).
  Verify the archive against the publisher's linked SHA-256 before extraction.
  This EXE is unsigned. Include `third_party/ffmpeg/LICENSE.txt` and its bundled
  build README/source reference in the package. This file is not a complete
  software-distribution license compliance audit.
- OpenAL installer: [official OpenAL downloads](https://www.openal.org/downloads/).
  Extract `oalinst.exe` from `oalinst.zip`; do not rename the ZIP to `.exe`.
  The extracted file was verified with a valid Creative Labs signature.
  The archive hash is locally recorded: the download page did not publish a
  separate checksum. Downloading it does not authorize running the installer.

To update a helper, obtain it from the verified publisher, check available
publisher checksums/signatures, inspect the extracted file, and deliberately
update the manifest. Never approve a suspect local file simply by recording its
current hash, or disable the guard to get a build through.

## Scope and tests

These checks target a known replacement incident and accidental packaging.
They cannot detect all malware, malicious source changes, an already-compromised
interpreter/operating system, infection inside archives or embedded in old
compiled games, or files changed concurrently after checking. Do not infer that
old builds are safe because a new input check passes.

Focused tests use disposable fixture files only:

```bat
python -I -S -m unittest discover -s tests -p test_build_safety.py
python -I -m unittest discover -s tests -p test_build_pack_data.py
```

Use a verified interpreter for this command. It does not compile the game or
restore/reset production data.
The asset-packaging tests use the installed `cryptography` dependency to decrypt
only their temporary output and check the archive members and embedded endpoint.
