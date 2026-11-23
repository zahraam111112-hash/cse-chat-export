; Inno Setup script for the Windows installer.
;
; The release workflow builds the app into a short path (a bundled Chromium
; tree gets deep enough to hit MAX_PATH under a long runner path) and passes
; it in as SourceRoot; the output directory is passed with /O.
;
; The result is a per-user installer: no administrator rights, no system-wide
; changes, and an uninstaller in Add/Remove Programs.

#ifndef SourceRoot
  #define SourceRoot "..\build\ChatGPT-Share-Exporter"
#endif
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "ChatGPT Share Exporter"

[Setup]
AppId={{8B7C4F21-5E3A-4F0D-9B2E-6A1D3C7F5A94}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=zahraam111112-hash
DefaultDirName={localappdata}\ChatGPTShareExporter\app
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
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
; recursesubdirs is what keeps the bundled python\ tree intact.
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\start.cmd"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\start.cmd"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\start.cmd"; Description: "Open {#AppName} now"; Flags: postinstall nowait skipifsilent
