#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{A8C72263-9E95-4BF5-8C5A-133B7A8A8D07}
AppName=Multilang
AppVersion={#AppVersion}
AppPublisher=Taha Al-Hariri
AppPublisherURL=https://github.com/telhariri/multilang-keyboard
AppSupportURL=https://github.com/telhariri/multilang-keyboard/issues
AppUpdatesURL=https://github.com/telhariri/multilang-keyboard/releases/latest
DefaultDirName={localappdata}\Programs\Multilang
DefaultGroupName=Multilang
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=.
OutputBaseFilename=Multilang-Setup
SetupIconFile=..\assets\multilang.ico
UninstallDisplayIcon={app}\Multilang.exe
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName=Multilang
VersionInfoProductVersion={#AppVersion}
VersionInfoDescription=Arabic and English keyboard layout correction

[Tasks]
Name: "startup"; Description: "Start Multilang automatically when I sign in"; GroupDescription: "Startup options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\Multilang\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Multilang"; Filename: "{app}\Multilang.exe"
Name: "{autodesktop}\Multilang"; Filename: "{app}\Multilang.exe"; Tasks: desktopicon
Name: "{userstartup}\Multilang"; Filename: "{app}\Multilang.exe"; Parameters: "--start-in-background"; Tasks: startup

[Run]
Filename: "{app}\Multilang.exe"; Description: "Launch Multilang"; Flags: postinstall nowait skipifsilent
