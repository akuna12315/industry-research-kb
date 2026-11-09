#ifndef BuildRoot
  #error BuildRoot is required
#endif
#ifndef OutputRoot
  #error OutputRoot is required
#endif
#ifndef ProjectRoot
  #error ProjectRoot is required
#endif

[Setup]
AppId={{45D16911-D834-470D-8506-E44FA715D76E}
AppName=研知 · 行业知识库
AppVersion=0.1.5
AppPublisher=研知
DefaultDirName={autopf}\YanZhi
DefaultGroupName=研知
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputRoot}
#ifdef SmokeTestOnly
OutputBaseFilename=YanZhi-Payload-Smoke-Only
CreateUninstallRegKey=no
#else
OutputBaseFilename=YanZhi-Setup-0.1.5-x64
CreateUninstallRegKey=yes
#endif
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\YanZhi.exe
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes
InfoBeforeFile=README.txt
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Files]
Source: "{#ProjectRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\OPEN_SOURCE.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\packaging\SOURCE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BuildRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "uninstall.cmd"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
#ifndef SmokeTestOnly
Name: "{group}\研知 · 行业知识库"; Filename: "{app}\YanZhi.exe"
Name: "{autodesktop}\研知 · 行业知识库"; Filename: "{app}\YanZhi.exe"; Tasks: desktopicon
Name: "{group}\卸载研知"; Filename: "{app}\uninstall.cmd"
#endif

; Data is intentionally outside {app}; uninstall never deletes a knowledge library.
