# WzToolkit one-shot build: compiles all four native tools into bin\.
# Pure ASCII on purpose (PowerShell 5.1 decodes BOM-less files as GBK).
#
#   scripts\build.bat        -> double-click wrapper around this script
#   build.ps1                -> build everything (skips what is already built)
#   build.ps1 -Clean         -> wipe bin\ and obj\ first
#   build.ps1 -Only name     -> build just one of: wz2nx, nxmap, nxmob, nxscan
#
# Toolchain is discovered without vcvars (same trick as the original MapleStory
# repo scripts: vswhere + cl.exe + link.exe directly, no msbuild).

param([string]$Only = '', [switch]$Clean)

$ErrorActionPreference = 'Stop'

# 脚本活在本项目 scripts\ 下，项目根是它的上一层
$ROOT = Split-Path -Parent $PSScriptRoot
$OBJ  = Join-Path $ROOT 'build\obj'
$RSP  = Join-Path $ROOT 'build\rsp'
$BIN  = Join-Path $ROOT 'bin'

$WZ2NX_SRC = Join-Path $ROOT 'vendor\wz2nx'
$NLNX_INC  = Join-Path $ROOT 'vendor\nlnx\includes'
$NLNX_LIB  = Join-Path $ROOT 'vendor\nlnx\lib'

if ($Clean) {
    foreach ($d in @($OBJ, $RSP, $BIN)) {
        if (Test-Path $d) { Remove-Item -Recurse -Force $d -Confirm:$false }
    }
}
foreach ($d in @($OBJ, $RSP, $BIN)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

# --- Locate toolchain ---------------------------------------------------------
$VSWHERE = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $VSWHERE)) { throw 'vswhere.exe not found (install VS with C++ tools)' }
$VSPATH = & $VSWHERE -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $VSPATH) { throw 'No VS install with C++ toolset' }

$MSVC = Get-ChildItem (Join-Path $VSPATH 'VC\Tools\MSVC') -Directory | Sort-Object Name -Descending | Select-Object -First 1
if (-not $MSVC) { throw 'MSVC dir not found' }
$VC = $MSVC.FullName

$SDKROOT = "${env:ProgramFiles(x86)}\Windows Kits\10"
$SDKVER  = Get-ChildItem (Join-Path $SDKROOT 'Include') -Directory | Sort-Object Name -Descending | Select-Object -First 1
if (-not $SDKVER) { throw 'Windows SDK not found' }
$SDKINC = Join-Path $SDKROOT "Include\$($SDKVER.Name)"
$SDKLIB = Join-Path $SDKROOT "Lib\$($SDKVER.Name)"

$CL   = Join-Path $VC 'bin\Hostx64\x64\cl.exe'
$LINK = Join-Path $VC 'bin\Hostx64\x64\link.exe'
foreach ($p in @($CL, $LINK)) { if (-not (Test-Path $p)) { throw "Missing $p" } }

$env:PATH = (Join-Path $VC 'bin\Hostx64\x64') + ';' + $env:PATH

# --- Compile / link helpers ---------------------------------------------------
# /utf-8 is mandatory: the vendored sources carry UTF-8 Chinese comments without
# BOM. Without the flag MSVC reads them as GBK and mis-lexes the line after a
# comment (observed: the "ladderRope" string literal vanished -> broken JSON).
$CFLAGS = @('/nologo','/c','/EHsc','/std:c++17','/MD','/O2','/DNDEBUG','/D_CONSOLE',
            '/utf-8',
            '/D_CRT_SECURE_NO_WARNINGS','/wd4819','/wd4996','/wd4267','/wd4244','/wd4101')

function Invoke-CC {
    param([string[]]$Src, [string]$ObjDir, [string[]]$IncDirs, [string]$Tag)
    $rspfile = Join-Path $RSP "cc_$Tag.rsp"
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($f in $CFLAGS) { $lines.Add($f) }
    foreach ($d in $IncDirs) { $lines.Add('/I"' + $d + '"') }
    $lines.Add('/Fo' + $ObjDir + [char]92)
    Set-Content -Path $rspfile -Value ($lines -join "`r`n") -Encoding ASCII
    foreach ($s in $Src) {
        $base = [IO.Path]::GetFileNameWithoutExtension($s)
        Write-Host "[cc] $base"
        $log = & $CL "@$rspfile" $s 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { Write-Host $log; throw "compile failed: $s" }
    }
}

function Invoke-Link {
    param([string[]]$Objs, [string[]]$LibDirs, [string[]]$Libs, [string]$OutExe)
    $rspfile = Join-Path $RSP 'link.rsp'
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('/nologo'); $lines.Add('/SUBSYSTEM:CONSOLE')
    $lines.Add('/OUT:"' + $OutExe + '"')
    foreach ($d in $LibDirs) { $lines.Add('/LIBPATH:"' + $d + '"') }
    foreach ($o in $Objs) { if (-not (Test-Path $o)) { throw "missing obj $o" }; $lines.Add('"' + $o + '"') }
    foreach ($l in $Libs)   { $lines.Add($l) }
    Set-Content -Path $rspfile -Value ($lines -join "`r`n") -Encoding ASCII
    Write-Host "[link] $(Split-Path -Leaf $OutExe)"
    $log = & $LINK "@$rspfile" 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { Write-Host $log; throw 'link failed: ' + $OutExe }
    Write-Host ("  OK -> {0}  {1} bytes" -f $OutExe, (Get-Item $OutExe).Length)
}

$VC_INC = @((Join-Path $VC 'include'), (Join-Path $SDKINC 'ucrt'),
            (Join-Path $SDKINC 'um'), (Join-Path $SDKINC 'shared'))
$VC_LIB = @((Join-Path $VC 'lib\x64'), (Join-Path $SDKLIB 'ucrt\x64'), (Join-Path $SDKLIB 'um\x64'))

function Want([string]$n) { return ($Only -eq '' -or $Only -eq $n) }

# --- 1) NoLifeWzToNx: WZ -> NX converter ---------------------------------------
if (Want 'wz2nx') {
    $objdir = Join-Path $OBJ 'wz2nx'; New-Item -ItemType Directory -Force -Path $objdir | Out-Null
    $inc = @((Join-Path $WZ2NX_SRC 'includes\lz4_v1_8_2_win64\include'),
             (Join-Path $WZ2NX_SRC 'includes\zlib\include'),
             (Join-Path $WZ2NX_SRC 'includes\libsquish'),
             $WZ2NX_SRC) + $VC_INC
    $lib = @((Join-Path $WZ2NX_SRC 'includes\lz4_v1_8_2_win64\static'),
             (Join-Path $WZ2NX_SRC 'includes\zlib\lib'),
             (Join-Path $WZ2NX_SRC 'includes\libsquish\win64\Release')) + $VC_LIB
    Invoke-CC -Src @((Join-Path $WZ2NX_SRC 'NoLifeWzToNx.cpp'),
                     (Join-Path $WZ2NX_SRC 'Keys.cpp'),
                     (Join-Path $WZ2NX_SRC 'includes\lz4_v1_8_2_win64\lz4.c'),
                     (Join-Path $WZ2NX_SRC 'includes\lz4_v1_8_2_win64\lz4hc.c')) `
             -ObjDir $objdir -IncDirs $inc -Tag 'wz2nx'
    Invoke-Link -Objs @((Join-Path $objdir 'NoLifeWzToNx.obj'),
                        (Join-Path $objdir 'Keys.obj'),
                        (Join-Path $objdir 'lz4.obj'),
                        (Join-Path $objdir 'lz4hc.obj')) `
                -LibDirs $lib -Libs @('zlib.lib','libsquish.lib') `
                -OutExe (Join-Path $BIN 'NoLifeWzToNx.exe')
}

# --- 2) nxmap / nxmob / nxscan: NX exporters (share lz4.obj from step 1) --------
$lz4obj = Join-Path $OBJ 'wz2nx\lz4.obj'
if (-not (Test-Path $lz4obj)) { throw 'missing lz4.obj - build wz2nx first' }

$nl_inc = @((Join-Path $NLNX_INC 'NoLifeNx'), $NLNX_INC) + $VC_INC
$nl_lib = @($NLNX_LIB) + $VC_LIB

foreach ($t in @('nxmap','nxmob','nxscan')) {
    if (-not (Want $t)) { continue }
    $objdir = Join-Path $OBJ $t; New-Item -ItemType Directory -Force -Path $objdir | Out-Null
    $inc_t = $nl_inc
    if ($t -eq 'nxmob') {
        # nxmob.cpp includes <lz4.h> directly (it validates frame headers while compositing)
        $inc_t = @((Join-Path $NLNX_INC 'NoLifeNx\nlnx\includes\lz4_v1_8_2_win64\include')) + $nl_inc
    }
    Invoke-CC -Src @(Join-Path $ROOT "vendor\cpp\$t.cpp") -ObjDir $objdir -IncDirs $inc_t -Tag $t
    $libs = @('NoLifeNx.lib')
    if ($t -eq 'nxmob') { $libs += 'Psapi.lib' }
    Invoke-Link -Objs @((Join-Path $objdir "$t.obj"), $lz4obj) `
                -LibDirs $nl_lib -Libs $libs `
                -OutExe (Join-Path $BIN "$t.exe")
}

Write-Host ''
Write-Host 'All done. Tools:'
Get-ChildItem $BIN -Filter *.exe | ForEach-Object { Write-Host ("  {0}  {1} bytes" -f $_.Name, $_.Length) }
Write-Host ''
Write-Host 'Start the WebUI:  double-click wztool.exe   (or: wztool.exe web / python wztool.py web)'
