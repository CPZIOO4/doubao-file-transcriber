#ifndef PackageDir
  #error PackageDir must be supplied by scripts/build.ps1
#endif
#ifndef ReleaseDir
  #error ReleaseDir must be supplied by scripts/build.ps1
#endif

[Setup]
AppId={{7E82E96B-4763-4E63-8E14-0E49D3687812}
AppName=豆包录音转写助手
AppVersion=1.2.0
AppVerName=豆包录音转写助手 1.2.0-preview.1
AppPublisher=CPZIOO4（第三方工具）
AppPublisherURL=https://github.com/CPZIOO4/doubao-file-transcriber
AppSupportURL=https://github.com/CPZIOO4/doubao-file-transcriber/issues
AppUpdatesURL=https://github.com/CPZIOO4/doubao-file-transcriber/releases
DefaultDirName={localappdata}\Programs\DoubaoFileTranscriber
DefaultGroupName=豆包录音转写助手
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.17763
OutputDir={#ReleaseDir}
OutputBaseFilename=doubao-file-transcriber-1.2.0-preview.1-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\豆包录音转写.exe
InfoBeforeFile={#PackageDir}\使用说明.txt
CloseApplications=yes
RestartApplications=no
AppMutex=Local\DoubaoFileTranscriber
SetupLogging=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl,installer-zh.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Files]
Source: "{#PackageDir}\豆包录音转写.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\第三方组件说明.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\app\compatibility.py"; DestDir: "{app}\app"; Flags: ignoreversion
Source: "{#PackageDir}\app\native_worker.py"; DestDir: "{app}\app"; Flags: ignoreversion
Source: "{#PackageDir}\app\doubao_transcribe.py"; DestDir: "{app}\app"; Flags: ignoreversion
Source: "{#PackageDir}\app\AudioDecode.exe"; DestDir: "{app}\app"; Flags: ignoreversion
Source: "{#PackageDir}\app\NAudio.dll"; DestDir: "{app}\app"; Flags: ignoreversion
Source: "{#PackageDir}\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc"
Source: "{#PackageDir}\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\豆包录音转写助手"; Filename: "{app}\豆包录音转写.exe"
Name: "{autodesktop}\豆包录音转写助手"; Filename: "{app}\豆包录音转写.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\豆包录音转写.exe"; Description: "启动豆包录音转写助手"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeSetup(): Boolean;
var
  Release: Cardinal;
begin
  Result := RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full', 'Release', Release) and (Release >= 461808);
  if not Result then
    MsgBox('需要 .NET Framework 4.7.2 或更高版本。请先通过 Windows 更新安装系统组件。', mbError, MB_OK);
end;
