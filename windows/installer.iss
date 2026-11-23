; Inno Setup script for the Windows installer.
;
; The release workflow builds the app into build\ChatGPT-Share-Exporter and
; compiles this script with ISCC. The result is a per-user installer: no
; administrator rights, no system-wide changes, and an uninstaller in
; Add/Remove Programs.

#define AppName "ChatGPT Share Exporter"
#define AppVersion "1.0.0"
#define AppPublisher "zahraam111112-hash"
#define AppExe "start.cmd"

[Setup]
AppId={{8B7C4F21-5E3A-4F0D-9B2E-6A1D3C7F5A94}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\ChatGPTShareExporter\app
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=ChatGPT-Share-Exporter-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Per-user install: the app needs no elevated privileges, and Chromium runs
; fine unprivileged.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
DisableDirPage=auto

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; recursesubdirs is what makes the bundled python/ tree survive intact.
Source: "..\build\ChatGPT-Share-Exporter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; One launch so Chromium is registered and the user sees it working; the task
; is deliberately not mandatory.
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName} now"; Flags: postinstall nowait skipifsilent
