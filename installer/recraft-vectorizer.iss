; ---------------------------------------------------------------------------
;  Recraft Vectorizer — Inno Setup installer script
;
;  Built by build.py after Nuitka has produced dist\windows-x64\RecraftVectorizer\. To build it by
;  hand, open this file in the Inno Setup IDE, or run:
;
;      ISCC.exe installer\recraft-vectorizer.iss
;
;  The result lands in dist\windows-x64\RecraftVectorizer-<version>-Setup.exe.
; ---------------------------------------------------------------------------

#define AppName        "Recraft Vectorizer"
#define AppSlug        "RecraftVectorizer"
#define AppSlogan      "Convert images to SVG with Recraft"
#define AppPublisher   "Tobse"
#define AppExeName     "RecraftVectorizer.exe"
#define AppId          "{{3E7A5D21-8B4C-4F96-A1D3-5C0E92B7F418}"

; build.py passes /DAppVersion=...; this is the fallback for a manual run.
#ifndef AppVersion
  #define AppVersion   "1.0.0"
#endif

; build.py passes /DSourceDir=... and /DOutputDir=... so that it stays the one
; place where artefact paths are decided; these are the fallbacks for a manual
; run from the Inno Setup IDE.
#ifndef SourceDir
  #define SourceDir    "..\dist\windows-x64\RecraftVectorizer"
#endif
#ifndef OutputDir
  #define OutputDir    "..\dist\windows-x64"
#endif
#define IconFile       "..\icon\icon.ico"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoDescription={#AppName} — {#AppSlogan}
VersionInfoVersion={#AppVersion}

DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
AllowNoIcons=yes

; Per-user by default, so no UAC prompt; the user can still elevate to install
; for everyone from the installer's own first page.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline

OutputDir={#OutputDir}
OutputBaseFilename={#AppSlug}-{#AppVersion}-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}

; LZMA2/max is worth the extra build minute here: the payload is mostly the
; Tcl/Tk runtime and Python extension modules, which compress very well.
Compression=lzma2/max
SolidCompression=yes
InternalCompressLevel=max

WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german";  MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\*";             DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md";               DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";                  Filename: "{app}\{#AppExeName}"; \
    Comment: "{#AppSlogan}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";            Filename: "{app}\{#AppExeName}"; \
    Comment: "{#AppSlogan}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "{cm:LaunchProgram,{#AppName}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Nuitka's onefile cache and our own Python bytecode, if either was created.
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// The user's settings and API key live in %USERPROFILE%\.recraft_vectorizer and are deliberately left
// behind on uninstall — unless they ask for a clean sweep.
//
// UninstallSilent() has to be checked explicitly: /SUPPRESSMSGBOXES does not
// reach MsgBox calls made from [Code], so without this guard an unattended
// removal (winget, a deployment script) would hang on a dialog nobody sees.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ConfigDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if UninstallSilent() then
      Exit;
    ConfigDir := ExpandConstant('{%USERPROFILE}\.recraft_vectorizer');
    if DirExists(ConfigDir) then
      if MsgBox('Also remove your Recraft Vectorizer settings (including your stored API key)?' + #13#10 +
                ConfigDir, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(ConfigDir, True, True, True);
  end;
end;
