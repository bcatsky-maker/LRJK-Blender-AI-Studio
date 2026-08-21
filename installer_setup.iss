; =====================================================================
; Inno Setup Script: LRJK Blender AI Studio (Disk Spanning Enabled)
; =====================================================================

#define MyAppName "LRJK Blender AI Studio"
#define MyAppVersion "2.1.30"
#define MyAppPublisher "LRJK / RK Offisium"
#define MyAppExeName "LRJK_Blender_AI_Studio.exe"
#define MyAppId "{{8F12B5A0-9C3E-4B12-8800-LRJKBLENDERAI}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Same AppId + DefaultDirName means an UPDATE installs over the existing
; install (Windows treats it as an upgrade, not a second copy).
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=Release_Installers

#ifdef UpdateMode
; --- Lean UPDATE installer (built by build_update.py) -------------------
;   Ships ONLY the app - never the multi-GB asset library - so it's a small
;   single-file .exe that upgrades an existing install. No disk spanning.
;   Silent-friendly: can be run /VERYSILENT by the app's auto-updater, and
;   it closes/relaunches the running app around the file replacement.
OutputBaseFilename=LRJK_Blender_AI_Studio_Update_v{#MyAppVersion}
CloseApplications=yes
RestartApplications=yes
#else
; --- Full first-time installer (build_all.py) --------------------------
OutputBaseFilename=LRJK_Blender_AI_Studio_Setup_v{#MyAppVersion}
; DiskSpanning is needed when the big asset library is bundled.
DiskSpanning=yes
DiskSliceSize=2147483648
UseSetupLdr=yes
#endif

SetupIconFile=assets\app_icon.ico
WizardImageFile=assets\app_inno.bmp
WizardSmallImageFile=assets\wizard_small.bmp
WizardImageStretch=yes

WizardStyle=modern
DisableWelcomePage=no
PrivilegesRequired=lowest
Compression=lzma2/ultra64
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autocopyaddon"; Description: "Automatically copy add-on script to Blender AppData scripts"; GroupDescription: "Blender Integration:"

[Files]
Source: "dist\LRJK_Blender_AI_Studio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "src\blender_addon\blender_rag_addon.py"; DestDir: "{app}\blender_addon"; Flags: ignoreversion
Source: "assets\splash_screen.mp4"; DestDir: "{app}\assets"; Flags: ignoreversion

; --- Ingested asset library, bundled when build_all.py passes
;     /DBundleLibrary=1 (whenever studio_memory.db + asset_store exist).
;     The app registers seed_library.db into the recipient's own writable
;     DB on first run and reads the asset files in place from here.
;
;     Excludes: the AI generation feature only imports 3D MODELS. The BBC
;     sound-effect audio (*.wav/*.ogg/*.mp3) and the HDRI environment maps
;     (*.hdr/*.exr) are never used by generation, together make up the vast
;     majority of the ~18GB, and were the direct cause of both the build-time
;     MAX_PATH failure and an install-time "source file is corrupted" error.
;     Dropping them yields a small, reliable installer that still ships every
;     3D model + its textures. (They stay in your local asset_store; only the
;     bundled copy omits them.) To bundle absolutely everything anyway, remove
;     the Excludes clause below.
;     nocompression: the remaining files are already-compressed media, so
;     recompressing them just wastes build time for no size gain. -----------
#ifdef BundleLibrary
Source: "asset_store\*"; DestDir: "{app}\asset_store"; Excludes: "*.wav,*.ogg,*.mp3,*.flac,*.aiff,*.hdr,*.exr"; Flags: ignoreversion recursesubdirs createallsubdirs nocompression
Source: "seed_library.db"; DestDir: "{app}"; Flags: ignoreversion
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
#ifdef UpdateMode
; Relaunch the app after the update, INCLUDING after a silent auto-update
; (no skipifsilent), so the user's app comes back on its own.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait postinstall
#else
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
#endif

[Code]
var
  WelcomeOverviewLabel: TNewStaticText;

function GetBlenderAddonsDir(): String;
var
  AppDataPath: String;
  BlenderRoot: String;
  FindRec: TFindRec;
  LatestVersion: String;
begin
  Result := '';
  AppDataPath := GetEnv('APPDATA');
  if AppDataPath = '' then Exit;

  BlenderRoot := AddBackslash(AppDataPath) + 'Blender Foundation\Blender\';
  if not DirExists(BlenderRoot) then Exit;

  LatestVersion := '';
  if FindFirst(BlenderRoot + '*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
        begin
          if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
          begin
            if FindRec.Name > LatestVersion then
              LatestVersion := FindRec.Name;
          end;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;

  if LatestVersion <> '' then
    Result := BlenderRoot + LatestVersion + '\scripts\addons\';
end;

function InitializeSetup(): Boolean;
var
  RegKey: String;
  InstalledVer: String;
begin
  Result := True;
  RegKey := 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{#MyAppId}_is1';
  
  if RegQueryStringValue(HKLM, RegKey, 'DisplayVersion', InstalledVer) or
     RegQueryStringValue(HKCU, RegKey, 'DisplayVersion', InstalledVer) then
  begin
    if MsgBox('An existing installation of LRJK Blender AI Studio (v' + InstalledVer + ') was detected.' + #13#10 + #13#10 +
              'Would you like to update/overinstall to version {#MyAppVersion}?',
              mbConfirmation, MB_YESNO) = IDNO then
    begin
      Result := False;
    end;
  end;
end;

procedure InitializeWizard();
begin
  WizardForm.WelcomeLabel1.Font.Color := $0080C0; 
  WizardForm.WelcomeLabel1.Font.Style := [fsBold];

  WizardForm.WelcomeLabel2.Font.Color := $000000;
  WizardForm.WelcomeLabel2.Font.Style := [];

  WelcomeOverviewLabel := TNewStaticText.Create(WizardForm);
  WelcomeOverviewLabel.Parent := WizardForm.WelcomePage;
  WelcomeOverviewLabel.Left := WizardForm.WelcomeLabel2.Left;
  WelcomeOverviewLabel.Top := WizardForm.WelcomeLabel2.Top + 140;
  WelcomeOverviewLabel.Width := WizardForm.WelcomeLabel2.Width;
  WelcomeOverviewLabel.Height := 120;
  WelcomeOverviewLabel.AutoSize := False;
  WelcomeOverviewLabel.WordWrap := True;
  WelcomeOverviewLabel.Font.Color := $800000;
  WelcomeOverviewLabel.Caption :=
    '✨ Welcome to LRJK Blender AI Studio (RK Offisium)' + #13#10 + #13#10 +
    '• Real-time 2-way AI bridge for Blender 3.x / 4.x' + #13#10 +
    '• Automatic 3D Mesh & Material Shader generation' + #13#10 +
    '• Local RAG Manual Indexing & Character Auto-Rigging Engine' + #13#10 +
    '• Multi-tier AI support (Offline local models, OpenAI & Claude)';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  SourceAddon: String;
  TargetDir: String;
  TargetFile: String;
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('autocopyaddon') then
  begin
    SourceAddon := ExpandConstant('{app}\blender_addon\blender_rag_addon.py');
    TargetDir := GetBlenderAddonsDir();

    if TargetDir <> '' then
    begin
      if ForceDirectories(TargetDir) then
      begin
        TargetFile := TargetDir + 'blender_rag_addon.py';
        if CopyFile(SourceAddon, TargetFile, False) then
          Log('Successfully copied add-on script to: ' + TargetFile)
        else
          Log('Failed to copy add-on script to Blender AppData.');
      end;
    end;
  end;
end;