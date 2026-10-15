# Builds the Windows release of Ramuzu IPTV Player.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# 1. Creates an isolated build environment (.venv-build) with PyInstaller.
# 2. Produces dist\Ramuzu IPTV Player\  (the app folder with the .exe).
# 3. If Inno Setup 6 is installed, produces dist\RamuzuIPTVPlayer-Setup-<version>.exe

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$appName = 'Ramuzu IPTV Player'
$appPy = Join-Path $root 'ramuzu_iptv_player.py'
$buildDir = Join-Path $root 'build'
$distDir = Join-Path $root 'dist'
$venv = Join-Path $root '.venv-build'
$python = Join-Path $venv 'Scripts\python.exe'

function Invoke-Step($exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $exe $arguments" }
}

if (-not (Test-Path $python)) {
    Write-Host '==> Creating build environment...'
    Invoke-Step 'py' @('-3', '-m', 'venv', $venv)
}

Write-Host '==> Installing build dependencies...'
Invoke-Step $python @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip')
Invoke-Step $python @('-m', 'pip', 'install', '--quiet', '-r', (Join-Path $root 'requirements.txt'), 'pyinstaller>=6.16')

Write-Host '==> Generating icon and version info...'
New-Item -ItemType Directory -Force $buildDir | Out-Null
$version = (& $python -I (Join-Path $PSScriptRoot 'prepare.py') $appPy $buildDir | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0) { throw 'prepare.py failed' }

Write-Host "==> Building $appName $version with PyInstaller..."
Invoke-Step $python @('-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed',
    '--name', $appName,
    '--icon', (Join-Path $buildDir 'ramuzu.ico'),
    '--version-file', (Join-Path $buildDir 'version_info.txt'),
    '--collect-submodules', 'keyring.backends',
    '--copy-metadata', 'keyring',
    '--exclude-module', 'tkinter',
    '--distpath', $distDir,
    '--workpath', (Join-Path $buildDir 'pyinstaller'),
    '--specpath', $buildDir,
    $appPy)
Write-Host '==> Removing Qt components the app does not use...'
# The app only uses QtCore/QtGui/QtWidgets with the native Windows platform plugin:
# no OpenGL, QML, WebGL, networking through Qt, or Qt's own translations.
$qt = Join-Path $distDir "$appName\_internal\PyQt5\Qt5"
$unused = @(
    'bin\opengl32sw.dll', 'bin\d3dcompiler_47.dll', 'bin\libEGL.dll', 'bin\libGLESv2.dll',
    'bin\Qt5Quick.dll', 'bin\Qt5Qml.dll', 'bin\Qt5QmlModels.dll', 'bin\Qt5WebSockets.dll',
    'bin\Qt5Network.dll', 'bin\Qt5DBus.dll',
    'plugins\platforms\qwebgl.dll', 'plugins\platforms\qminimal.dll', 'plugins\platforms\qoffscreen.dll',
    'translations'
)
foreach ($item in $unused) {
    $path = Join-Path $qt $item
    if (Test-Path $path) { Remove-Item $path -Recurse -Force }
}

Copy-Item (Join-Path $root 'LICENSE') (Join-Path $distDir "$appName\LICENSE.txt") -Force

Write-Host '==> Creating portable zip...'
$zip = Join-Path $distDir "RamuzuIPTVPlayer-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $distDir $appName) -DestinationPath $zip
Write-Host "Portable: $zip"

$iscc = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($iscc) {
    Write-Host '==> Building installer with Inno Setup...'
    Invoke-Step $iscc @('/Q', "/DAppVersion=$version", (Join-Path $PSScriptRoot 'installer.iss'))
    Write-Host "Done: $distDir\RamuzuIPTVPlayer-Setup-$version.exe"
} else {
    Write-Host "Done: $distDir\$appName\$appName.exe"
    Write-Host 'Inno Setup 6 was not found, so no installer was built (https://jrsoftware.org/isdl.php).'
}
