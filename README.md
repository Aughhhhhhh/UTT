# UTT

UTT is a Windows desktop tool for browsing and modifying Skate 3
`createacharacter.big` archives.

## Features

- Unpack `createacharacter.big` into a local cache.
- Browse and export PSG textures.
- Browse character models directly from
  `cache/data/content/createacharacter/model/cas_db`.
- Preview PSG models with an interactive 3D viewer.
- Convert images to PSG textures with resolution and opacity controls.
- Repack the edited cache to `cache/data/createacharacter.big`.

## Install

Download `UTT-Setup-1.1.1.exe` from the
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

Build the portable executable:

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
5. Create a tag matching the version, such as `v1.1.2`, and attach
   `build/installer/UTT-Setup-1.1.2.exe`.
6. Set a release title, add notes, and choose **Publish release**.

Use a new version and tag for normal changes. Only replace an existing release
asset when correcting the build for that exact version.

## Credits

- duckyinnit — everything
- itsclaudeya — model viewer
<sub>hi its me itscloudya</sub>

## License

UTT's source code is released under the [MIT License](LICENSE). External
runtime tools, game archives, and game assets are not licensed under MIT; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
