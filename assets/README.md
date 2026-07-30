# Runtime assets

The source repository does not license or redistribute the external utilities
used for archive and texture conversion. Place the required files in this
layout before running those features or creating a distribution build:

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

The prebuilt installer includes the runtime files needed by the application.
See `THIRD_PARTY_NOTICES.md` for attribution and licensing boundaries.
