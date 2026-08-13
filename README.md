# UTT

UTT is a Windows desktop tool for browsing and modifying Skate 3
`createacharacter.big` archives.

## Features

- Unpack `createacharacter.big` into a local cache.
- Browse and export PSG textures (PS3) and RX2 textures (Xbox 360).
- Browse character models directly from
  `cache/data/content/createacharacter/model/cas_db`.
- Preview PSG and RX2 models in an interactive GPU-accelerated 3D viewer
  (OpenGL) with soft hemisphere lighting.
- Convert images to PSG textures with resolution and opacity controls.
- Convert images to RX2 textures with a built-in encoder
  (pure-Python DXT5 + Xbox 360 tiled layout) and the same resolution and
  opacity controls as PSG. Sizes up to 2048x2048 use the standard Xbox 360
  header and work on real hardware; 4K (4096) exports use the extended
  13-bit header and are for the PC recomp only (a one-time warning explains
  this).
- Quick viewer: open any `.rx2` or `.psg` file with UTT (or right-click ->
  Open with) to preview it in a maximized window — textures and models for
  both platforms, with model export to glTF/GLB.
- Convert glTF/GLB models back to PSG (PS3) or RX2 (Xbox 360) using a donor
  game file as a template (pick the mode from the Convert tab's dropdown).
- Search the whole cache: hex searches find files that are not listed in the
  catalog, shown under "Other cache files".
- Repack the edited cache to `cache/data/createacharacter.big` (choose
  compression level in the Repack dialog; uncompressed is fastest).
- Live character scan for PS3: read the current character's models and
  textures from RPCS3 memory and preview them (saved to `output/current_items.txt`).
- Check for updates: on startup UTT quietly checks GitHub Releases for a newer
  version and offers to install it (you can say "later" or skip a version);
  Settings also has a manual **Check for updates** button.
- Crash log: when running from source for development, details are written to
  `utt_crash.log` next to the executable. Packaged builds do not write crash logs.

## Install

Download `UTT-Setup-2.0.2.exe` from the
[latest release](https://github.com/Aughhhhhhh/UTT/releases/latest).
The installer defaults to `Documents/UTT`, and the destination can be changed.

On first launch, choose your own `createacharacter.big`. UTT creates the cache
only after an archive is selected.

## Run from source

UTT requires Python 3.11 or newer.

```powershell
python -m pip install -r "Tool Requirements.txt" -r "Get Current Models and Textures requirements.txt"
python main.py
```

Archive and texture conversion features use the runtime utilities included in
the [assets](assets) folder. Those external utilities are not covered by UTT's
MIT license.

## Build

Install the build dependencies:

```powershell
python -m pip install -r requirements-build.txt
```

Build the portable executable (a single self-contained `UTT.exe` with
everything baked inside):

```powershell
.\build.bat
```

Install [Inno Setup](https://jrsoftware.org/isdl.php), then build the executable
and installer:

```powershell
.\build_installer.bat
```

The script detects normal Inno Setup 6 and 7 installations. If it is installed
somewhere else, set `INNO_ISCC` to the full path of `ISCC.exe`. The finished
installer is placed in `build/installer` and selected in File Explorer.

To Authenticode-sign distribution builds, set `UTT_SIGN_CERT_SHA1` to the
thumbprint of a trusted code-signing certificate before running either script.

## Publish a release

Collaborators with write access can publish installers without the GitHub CLI:

1. Update `AppVersion` in `installer.iss` and the matching version fields in
   `version_info.txt`.
2. Commit and push the source changes. GitHub Desktop can do this without a
   separate Git installation.
3. Run `build_installer.bat`.
4. Open the repository's **Releases** page and choose **Draft a new release**.
5. Create a tag matching the version, such as `v2.0.2`, and attach
   `build/installer/UTT-Setup-2.0.2.exe`.
6. Set a release title, add notes, and choose **Publish release**.

Use a new version and tag for normal changes. Only replace an existing release
asset when correcting the build for that exact version.

## What's new in 2.0.2

- Fixed the Xbox RX2 image convert: the built-in encoder was missing since
  2.0.0, so converting an image to an RX2 texture failed. The encoder is
  restored and round-trip verified.
- RX2 exports now write the real Xbox 360 header layout, so textures up to
  2048x2048 load correctly on a console (or in Noesis). Selecting 4K on Xbox
  mode warns that the extended-header export is for the PC recomp only, with
  a "don't show again" option.
- All saved settings (platform, theme, export mode, skipped update version,
  skipped archive picker, skipped 4K warning) were consolidated into a single
  `utt_config.json` next to the executable. Existing `*.txt` settings are
  migrated automatically on first launch.

## Credits

 - duckyinnit — had the idea
 - ai — everything
 - itsclaudeya — model viewer
 - Salix — Get Current Models And Textures
 - S4M — PSG Converter
 - Wisp — RX2 Converter
 - GHFear — RX2 Parse
 - Tuukkas — RX2
 <sub>hi its me itscloudya</sub>

## License

UTT's source code is released under the [MIT License](LICENSE). External
runtime tools, game archives, and game assets are not licensed under MIT; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
