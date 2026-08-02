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
  rx2/
    container.rx2
  PsgCliTool/
    PsgCliTool.exe
    convert.bat
    Assets/
      temp.psg
```

`assets/rx2/container.rx2` is the template used by the built-in RX2 texture
encoder (header and file table for tiled DXT5 mip chains). It must stay in
place for the Xbox 360 converter.

The files in this folder are not covered by UTT's MIT license. See
`THIRD_PARTY_NOTICES.md` and `assets/licenses` for attribution and licensing
boundaries.
