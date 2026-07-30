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
python -m pip install -r requirements.txt
python main.py
```

Archive and texture conversion features use the runtime utilities included in
the [assets](assets) folder. Those external utilities are not covered by UTT's
MIT license.

## Build

Build the portable executable:

```powershell
build.bat
```

Build the executable and installer with Inno Setup 7:

```powershell
$env:INNO_ISCC = "C:\Program Files\Inno Setup 7\ISCC.exe"
build_installer.bat
```

To Authenticode-sign distribution builds, set `UTT_SIGN_CERT_SHA1` to the
thumbprint of a trusted code-signing certificate before running either script.

## Credits

- duckyinnit — everything
- itsclaudeya — model viewer
<sub>hi its me itscloudya</sub>

## License

UTT's source code is released under the [MIT License](LICENSE). External
runtime tools, game archives, and game assets are not licensed under MIT; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
