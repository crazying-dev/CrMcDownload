; ============================================================
; CrMCDownload 安装包脚本（Inno Setup 6）
;
; 编译：
;   ISCC.exe /DMyAppVersion=0.5.4 /DPayloadDir=..\dist\CrMCDownload CrMCDownload.iss
; 或直接用 packaging\build_installer.ps1（自动取版本号、校验产物）。
;
; 覆盖升级：AppId 固定 + 默认目录固定在 {localappdata}\Programs\CrMCDownload
; + 文件 ignoreversion，因此新版本安装包可直接盖在旧版上，无需先卸载。
; ============================================================

#define MyAppName "CrMCDownload"
#define MyAppExeName "CrMCDownload.exe"
#define MyAppPublisher "Cr"
#define MyAppUrl "https://github.com/crazying-dev/CrMcDownload"

#ifndef MyAppVersion
  #define MyAppVersion "0.5.3"
#endif

; 四段式版本号，供安装包本体的文件属性使用
#define MyAppVersion4 MyAppVersion + ".0"

#ifndef PayloadDir
  #define PayloadDir "..\dist\CrMCDownload"
#endif

[Setup]
AppId={{9F3C6E24-5B18-4C7A-9D02-8E4A1B75C3D6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppUrl}
AppSupportURL={#MyAppUrl}/issues
AppUpdatesURL={#MyAppUrl}/releases
DefaultDirName={localappdata}\Programs\CrMCDownload
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=CrMCDownload-{#MyAppVersion}-win64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
AllowNoIcons=yes
SetupIconFile=..\assets\icon.ico
; 覆盖升级：沿用上次的安装路径与程序组，不新建第二份安装
UsePreviousAppDir=yes
UsePreviousGroup=yes
; 旧版正在运行时由 Restart Manager 关闭它，随后覆盖文件
CloseApplications=yes
RestartApplications=no
; 安装包本体的文件属性（右键 → 属性 → 详细信息）
VersionInfoVersion={#MyAppVersion4}
VersionInfoProductVersion={#MyAppVersion4}
VersionInfoProductName={#MyAppName}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoCopyright=Copyright (C) 2026 Cr
VersionInfoTextVersion={#MyAppVersion4}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
