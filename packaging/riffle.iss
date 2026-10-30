; Inno Setup script for Riffle. Per-user install: no admin prompt.
; Build with build.bat (runs PyInstaller first).
#define AppVer "1.1.0"

[Setup]
; Same AppId as 1.0.x so this upgrades the existing install in place.
AppId={{8F3C2B1E-6D2A-4E5B-9C7F-1A2B3C4D5E6F}
AppName=Riffle
AppVersion={#AppVer}
AppVerName=Riffle {#AppVer}
AppPublisher=Bliink
AppPublisherURL=https://github.com/ravirahul/riffle
AppSupportURL=https://github.com/ravirahul/riffle/issues
AppUpdatesURL=https://github.com/ravirahul/riffle/releases
DefaultDirName={localappdata}\Programs\Riffle
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Riffle-Setup-{#AppVer}
SetupIconFile=riffle.ico
UninstallDisplayIcon={app}\Riffle.exe
UninstallDisplayName=Riffle
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=100
CloseApplications=force
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
VersionInfoVersion={#AppVer}
VersionInfoCompany=Bliink
VersionInfoDescription=Riffle installer

[Tasks]
Name: "autostart"; Description: "Start Riffle with Windows (recommended, so Ctrl+Shift+Space always works)"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\build\dist\Riffle\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; 1.0.x bundled the visual libraries; they now come as an optional download.
Type: filesandordirs; Name: "{app}\_internal\openvino"
Type: filesandordirs; Name: "{app}\_internal\cv2"
Type: filesandordirs; Name: "{app}\_internal\PIL"
Type: filesandordirs; Name: "{app}\_internal\models"

[Icons]
Name: "{userprograms}\Riffle"; Filename: "{app}\Riffle.exe"; AppUserModelID: "Bliink.Riffle"; Comment: "Search and clean up your files"
Name: "{userdesktop}\Riffle"; Filename: "{app}\Riffle.exe"; AppUserModelID: "Bliink.Riffle"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Riffle"; ValueData: """{app}\Riffle.exe"" --no-window"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\Riffle.exe"; Description: "Open Riffle now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM Riffle.exe /F"; Flags: runhidden; RunOnceId: "StopRiffle"

[UninstallDelete]
; The index and settings live in %LOCALAPPDATA%\Riffle. Leave them on uninstall,
; except the large rebuildable parts.
Type: files; Name: "{localappdata}\Riffle\riffle-index.bin"
Type: files; Name: "{localappdata}\Riffle\hashes.db"
Type: filesandordirs; Name: "{localappdata}\Riffle\addons"
