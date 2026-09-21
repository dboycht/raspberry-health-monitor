# Build helper for this Android project on THIS machine.
#
# Why not gradlew.bat: a local TLS-terminating proxy (Steam++) breaks the Gradle
# wrapper distribution download ("PKIX path building failed"), so we use the
# already-unpacked Gradle 8.13 below. Everything here is plain ASCII on purpose
# (PowerShell 5.1 reads a BOM-less .ps1 as ANSI; CJK inside would break parsing).
#
# Usage (from anywhere):
#   powershell -File tools\build.ps1 -Test          # JVM unit tests only
#   powershell -File tools\build.ps1 -Apk           # assembleDebug
#   powershell -File tools\build.ps1 -All           # tests + APK (default when no switch)
#   powershell -File tools\build.ps1 -Clean
#   powershell -File tools\build.ps1 -Install       # adb install the debug APK
#   powershell -File tools\build.ps1 -Log           # follow logcat
#   powershell -File tools\build.ps1 -Devices       # adb devices

[CmdletBinding()]
param(
    [switch]$Test,
    [switch]$Apk,
    [switch]$All,
    [switch]$Clean,
    [switch]$Install,
    [switch]$Log,
    [switch]$Devices,
    [switch]$WithDaemon
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path

$jdk = 'C:\Program Files\Microsoft\jdk-21.0.8.9-hotspot'
$sdk = 'D:\Program\Android\SDK'
$gradle = Join-Path $env:USERPROFILE '.gradle\wrapper\dists\gradle-8.13-bin\5xuhj0ry160q40clulazy9h7d\gradle-8.13\bin\gradle.bat'

if (-not (Test-Path $jdk)) { throw "JDK not found: $jdk" }
if (-not (Test-Path $sdk)) { throw "Android SDK not found: $sdk" }
if (-not (Test-Path $gradle)) { throw "Gradle 8.13 not found: $gradle" }

$env:JAVA_HOME = $jdk
$env:ANDROID_HOME = $sdk

$daemonArg = if ($WithDaemon) { @() } else { @('--no-daemon') }

function Invoke-Gradle {
    param([string[]]$Tasks)
    Write-Host "=== gradle $($Tasks -join ' ') ==="
    & $gradle @daemonArg -p $projectRoot @Tasks
    if ($LASTEXITCODE -ne 0) { throw "gradle failed (exit $LASTEXITCODE): $($Tasks -join ' ')" }
}

$didSomething = $false

if ($Clean) {
    $didSomething = $true
    Invoke-Gradle @('clean')
}
if ($All -or $Test) {
    $didSomething = $true
    Invoke-Gradle @('testDebugUnitTest')
    Write-Host 'Unit test report: app\build\reports\tests\testDebugUnitTest\index.html'
}
if ($All -or $Apk) {
    $didSomething = $true
    Invoke-Gradle @('assembleDebug')
    $apk = Join-Path $projectRoot 'app\build\outputs\apk\debug\app-debug.apk'
    if (Test-Path $apk) {
        $size = [math]::Round((Get-Item $apk).Length / 1MB, 2)
        Write-Host "APK: $apk ($size MB)"
    }
}

$adb = Join-Path $sdk 'platform-tools\adb.exe'
if ($Devices) {
    $didSomething = $true
    & $adb devices -l
}
if ($Install) {
    $didSomething = $true
    $apk = Join-Path $projectRoot 'app\build\outputs\apk\debug\app-debug.apk'
    if (-not (Test-Path $apk)) { throw "APK not found: $apk (run -Apk first)" }
    & $adb install -r $apk
}
if ($Log) {
    $didSomething = $true
    & $adb logcat -v time -s HealthMonitor:* AndroidRuntime:E
}

if (-not $didSomething) {
    Write-Host 'android build helper - pick a switch:'
    Write-Host '  -Test      run JVM unit tests (testDebugUnitTest)'
    Write-Host '  -Apk       build the debug APK (assembleDebug)'
    Write-Host '  -All       tests + APK'
    Write-Host '  -Clean     remove build outputs'
    Write-Host '  -Install   adb install the debug APK'
    Write-Host '  -Log       follow logcat'
    Write-Host '  -Devices   list attached devices'
    Write-Host '  -WithDaemon  allow the Gradle daemon (faster repeat builds)'
}
