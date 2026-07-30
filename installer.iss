#define AppName "UTT"
#define AppVersion "1.1.1"
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
UninstallDisplayIcon={app}\UTT.ico
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
Source: "build\UTT.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "build\assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\UTT"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\UTT.ico"
Name: "{autodesktop}\UTT"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\UTT.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch UTT"; Flags: nowait postinstall skipifsilent
