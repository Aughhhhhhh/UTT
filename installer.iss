#define AppName "UTT"
#define AppVersion "1.1.8"
#define AppExeName "UTT.exe"

[Setup]
AppId={{4D716A6A-9BD1-4F7A-A5D7-C1BB77554269}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=duckyinnit
DefaultDirName={userdocs}\UTT
DefaultGroupName=UTT
DisableDirPage=no
UsePreviousAppDir=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
OutputDir=build\installer
OutputBaseFilename=UTT-Setup-{#AppVersion}
SetupIconFile=UTT.ico
UninstallDisplayIcon={app}\UTT.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
SetupLogging=yes
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription=UTT Installer

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "build\UTT.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "build\assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\UTT"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"
Name: "{autodesktop}\UTT"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; Open .rx2 and .psg files with UTT's quick viewer on double-click.
; (Windows 10/11 remember a user's "Open with" choice and keep using it;
; the first time, use right-click -> Open with -> UTT to re-pick the app.)
Root: HKCU; Subkey: "Software\Classes\.rx2"; ValueType: string; ValueName: ""; ValueData: "UTT.rx2"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.psg"; ValueType: string; ValueName: ""; ValueData: "UTT.psg"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\UTT.rx2"; ValueType: string; ValueName: ""; ValueData: "UTT RX2 File"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\UTT.psg"; ValueType: string; ValueName: ""; ValueData: "UTT PSG File"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\UTT.rx2\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\UTT.exe,0"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\UTT.psg\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\UTT.exe,0"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\UTT.rx2\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\UTT.exe"" ""%1"""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\UTT.psg\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\UTT.exe"" ""%1"""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch UTT"; Flags: nowait postinstall skipifsilent
