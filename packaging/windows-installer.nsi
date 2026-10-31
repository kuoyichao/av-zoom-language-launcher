Unicode True
!include "MUI2.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
Name "Zoom Language Launcher"
OutFile "../installers/Zoom-Language-Launcher-2.4.2-Windows-x64-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\Zoom Language Launcher"
RequestExecutionLevel user
SetCompressor /SOLID lzma
VIProductVersion "2.4.2.0"
VIAddVersionKey "ProductName" "Zoom Language Launcher"
VIAddVersionKey "FileDescription" "Zoom Language Launcher installer"
VIAddVersionKey "FileVersion" "2.4.2"
VIAddVersionKey "LegalCopyright" "Personal application"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "Install the local Zoom Language Launcher with its bundled Python runtime.$\r$\n$\r$\nZoom Workplace must be installed separately. Configure Zoom to join with microphone muted, camera off, no video preview, and automatic computer audio.$\r$\n$\r$\nUses the experimental /secondary launch switch. Windows Zoom may still reuse an existing client; test with two languages first."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
Function .onInit
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_OK "This installer requires Windows 10 or later."
    Abort
  ${EndIf}
  ${IfNot} ${RunningX64}
    MessageBox MB_OK "This installer requires 64-bit Windows 10 or later."
    Abort
  ${EndIf}
FunctionEnd
Section "Launcher"
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  File "../build/windows-payload/zoom_language_launcher.py"
  File "../build/windows-payload/zoom_window_actions.py"
  File "../build/windows-payload/zoom_audio_routing.py"
  File "../build/windows-payload/README.txt"
  SetOutPath "$INSTDIR\runtime"
  File /r "../build/windows-payload/runtime/*"
  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\Zoom Language Launcher"
  CreateShortCut "$SMPROGRAMS\Zoom Language Launcher\Zoom Language Launcher.lnk" "$INSTDIR\runtime\pythonw.exe" '$\"$INSTDIR\zoom_language_launcher.py$\"'
  CreateShortCut "$SMPROGRAMS\Zoom Language Launcher\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortCut "$DESKTOP\Zoom Language Launcher.lnk" "$INSTDIR\runtime\pythonw.exe" '$\"$INSTDIR\zoom_language_launcher.py$\"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZoomLanguageLauncher" "DisplayName" "Zoom Language Launcher"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZoomLanguageLauncher" "DisplayVersion" "2.4.2"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZoomLanguageLauncher" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZoomLanguageLauncher" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZoomLanguageLauncher" "NoRepair" 1
SectionEnd
Section "Uninstall"
  SetShellVarContext current
  Delete "$DESKTOP\Zoom Language Launcher.lnk"
  Delete "$SMPROGRAMS\Zoom Language Launcher\Zoom Language Launcher.lnk"
  Delete "$SMPROGRAMS\Zoom Language Launcher\Uninstall.lnk"
  RMDir "$SMPROGRAMS\Zoom Language Launcher"
  Delete "$INSTDIR\zoom_language_launcher.py"
  Delete "$INSTDIR\zoom_window_actions.py"
  Delete "$INSTDIR\zoom_audio_routing.py"
  Delete "$INSTDIR\README.txt"
  RMDir /r "$INSTDIR\runtime"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZoomLanguageLauncher"
  ; Saved sessions in Documents are deliberately preserved.
SectionEnd
