# Runtime assets

These files are the runtime utilities used for archive and texture conversion.
Keep this layout intact when running from source or creating a distribution
build:

```text
assets/
  bigfile.exe
  nvcompress.exe
  nvdecompress.exe
  nvtt.dll
  texconv.exe
  PsgCliTool/
    PsgCliTool.exe
    convert.bat
    Assets/
      temp.psg
```

The files in this folder are not covered by UTT's MIT license. See
`THIRD_PARTY_NOTICES.md` and `assets/licenses` for attribution and licensing
boundaries.
