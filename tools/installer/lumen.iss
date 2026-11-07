; Lumen 安装程序 —— Inno Setup 6 脚本
; 编译：ISCC.exe tools\installer\lumen.iss
; 产物：dist_installer\LumenSetup.exe（单文件，无需自解压运行时）

#define AppName "Lumen"
#define AppVer "1.0.0"
#define AppExe "Lumen.exe"
#define SrcDir "..\..\dist\Lumen"

[Setup]
AppId={{7F3C9A2E-5B41-4C8D-9E77-2A1F0B6C3D18}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName} {#AppVer}
AppPublisher={#AppName}
VersionInfoVersion=1.0.0.0
VersionInfoCompany={#AppName}
VersionInfoDescription={#AppName} 安装程序
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVer}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist_installer
OutputBaseFilename=LumenSetup
SetupIconFile=..\..\app\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
LicenseFile=..\..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
AllowNoIcons=yes
MinVersion=10.0

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[Messages]
SetupWindowTitle=%1 安装程序
SetupAppRunningError=检测到 %1 正在运行。%n%n请先关闭它，然后点「确定」继续，或点「取消」退出。
UninstallAppRunningError=检测到 %1 正在运行。%n%n请先关闭它，然后点「确定」继续，或点「取消」退出。
ButtonBack=< 上一步
ButtonNext=下一步 >
ButtonInstall=安装
ButtonCancel=取消
ButtonFinish=完成
ButtonBrowse=浏览...
WelcomeLabel1=欢迎使用 [name] 安装向导
WelcomeLabel2=即将在你的电脑上安装 [name/ver]。%n%n建议先关闭其它正在运行的程序，然后点「下一步」继续。
SelectDirDesc=[name] 装到哪里？
SelectDirLabel3=安装程序会把 [name] 装到下面的文件夹。
SelectDirBrowseLabel=点「下一步」继续；想换位置就点「浏览」。
DiskSpaceMBLabel=至少需要 [mb] MB 可用空间。
SelectTasksDesc=需要执行哪些附加任务？
SelectTasksLabel2=选择安装期间要做的附加任务，然后点「下一步」。
ReadyLabel1=准备就绪，可以开始安装 [name]。
ReadyLabel2a=点「安装」开始；想改设置就点「上一步」。
ReadyLabel2b=点「安装」开始。
FinishedHeadingLabel=[name] 安装完成
FinishedLabel=[name] 已安装完成。可以从开始菜单或桌面快捷方式启动。
FinishedLabelNoIcons=[name] 已安装完成。
ClickInstall=点「安装」开始安装。
ClickFinish=点「完成」退出安装程序。
StatusExtractFiles=正在解压文件…
StatusCreateIcons=正在创建快捷方式…
StatusCreateRegistryEntries=正在写注册表…
StatusRollback=正在回滚更改…
StatusUninstalling=正在卸载…
ConfirmUninstall=确定要卸载 %1 吗？%n%n注意：安装目录里的 data 文件夹（媒体库索引、缩略图、备份）不会被删除。
UninstalledAll=%1 已成功卸载。
UninstalledMost=%1 卸载完成，部分文件需要手动删除。
UninstalledAndNeedsRestart=需要重启电脑才能完成卸载，现在重启吗？

