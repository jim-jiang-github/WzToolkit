# 把一份便携版 CPython 下载到 <项目根>\.pyruntime\，供 build.bat 全程使用。
#
# 为什么要自己带解释器：
#   公源代码要给任何人 clone 下来双击就能编。对方电脑上有没有 Python、
#   是 3.8 还是 3.13、装的是 32 位还是 64 位，全都不可控；用本机解释器
#   还会把依赖装进系统 site-packages（.pylibs 就白设计了）。所以解释器
#   也一起放进项目目录，删掉 .pyruntime\ 就能回到「全新环境」重来。
#
# 来源：nuget.org 的 python 包（就是 python.org 官方构建，含完整 Lib\ 与 DLLs\，
#       PyInstaller 能直接用；embed 版缺这些，不适合打包）。
# 体积：约 15 MB 压缩包，解开约 40 MB。
#
# 用法（一般不用手动跑，build.bat 第 1 步会调）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\_fetch-python.ps1
#   powershell ... -File scripts\_fetch-python.ps1 -Force     # 强制重下
#
# 退出码：0 = 就绪；1 = 失败（网络/解压问题，屏幕上有中文说明）

param(
    [string]$Version = "3.12.4",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
# PowerShell 5.1 默认可能还在 TLS 1.0，nuget.org 要求 TLS 1.2+
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    # 老系统没有这个枚举值也没关系，下面下载失败会给出提示
}

$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$dst = Join-Path $root ".pyruntime"
$exe = Join-Path $dst "python.exe"
$build = Join-Path $root ".build"

function Get-PyVersion([string]$path) {
    if (-not (Test-Path $path)) { return $null }
    try {
        $out = & $path -V 2>&1
        if ($LASTEXITCODE -ne 0) { return $null }
        return ($out | Out-String).Trim()
    } catch {
        return $null
    }
}

$have = Get-PyVersion $exe
if (-not $Force -and $have -and ($have -match [regex]::Escape($Version))) {
    Write-Host "[Python] 已就绪：.pyruntime\python.exe（$have），跳过下载。"
    exit 0
}
if ($have) {
    Write-Host "[Python] 现有解释器版本不对（$have，需要 $Version），重新下载。"
}

Write-Host "[Python] 项目内没有可用解释器，下载便携版 Python $Version（约 15 MB）..."
Write-Host "         来源：https://www.nuget.org/api/v2/package/python/$Version"

New-Item -ItemType Directory -Force -Path $build | Out-Null
$zip = Join-Path $build "python-$Version.nupkg"
$url = "https://www.nuget.org/api/v2/package/python/$Version"

$downloaded = $false
# ZipFile 用于下载后的完整性校验，必须在 Test-Zip 定义之前加载
Add-Type -AssemblyName System.IO.Compression.FileSystem
# 优先用系统自带 curl.exe（Win10 1803+）：流式写盘、断点重试，比
# Invoke-WebRequest 整段读进内存稳（实测 IWR 偶尔存出半截文件）。
$sys32 = Join-Path $env:SystemRoot "System32"
$curl = Join-Path $sys32 "curl.exe"

function Test-Zip([string]$path) {
    # 只认真正的 zip：OpenRead 会校验中央目录，半截文件直接抛异常
    if (-not (Test-Path $path)) { return $false }
    try {
        $fs = [System.IO.Compression.ZipFile]::OpenRead($path)
        $fs.Dispose()
        return $true
    } catch {
        return $false
    }
}

for ($i = 1; $i -le 3; $i++) {
    try {
        if (Test-Path $zip) { Remove-Item -Force $zip }
        if (Test-Path $curl) {
            # --silent：进度条走 stderr，在 $ErrorActionPreference=Stop 下会被
            # PowerShell 当成 NativeCommandError 打断；--show-error 只保留错误信息
            & $curl -fL --silent --show-error --retry 2 --connect-timeout 30 -o $zip $url
            if ($LASTEXITCODE -ne 0) { throw "curl 退出码 $LASTEXITCODE" }
        } else {
            $ProgressPreference = "SilentlyContinue"
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 600
        }
        if (-not (Test-Zip $zip)) {
            $size = 0
            if (Test-Path $zip) { $size = (Get-Item $zip).Length }
            throw "文件不完整或不是 zip（$size 字节），可能被网络截断"
        }
        $downloaded = $true
        break
    } catch {
        Write-Host "[Python] 第 $i 次下载失败：$($_.Exception.Message)"
        if (Test-Path $zip) { Remove-Item -Force $zip -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
    }
}

if (-not $downloaded) {
    Write-Host ""
    Write-Host "[错误] 下载 Python 失败（需要能访问 nuget.org）。"
    Write-Host "       可选办法："
    Write-Host "       1) 手动下载 https://www.nuget.org/api/v2/package/python/$Version"
    Write-Host "          （浏览器会存成 python.$Version.nupkg，把扩展名改成 .zip 也能解开）"
    Write-Host "       2) 解压后把里面的 tools\ 目录内容整个复制到 $dst\"
    Write-Host "          确认 $dst\python.exe 存在，再重跑构建。"
    Write-Host "       3) 公司网络需要代理时，先 set HTTPS_PROXY=http://主机:端口 再重试。"
    exit 1
}

Write-Host "[Python] 下载完成，解压..."
$tmp = Join-Path $build "python-extract"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Add-Type -AssemblyName System.IO.Compression.FileSystem
try {
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $tmp)
} catch {
    Write-Host "[错误] 解压失败：$($_.Exception.Message)"
    Write-Host "       文件可能下载不完整，删掉 $zip 后重试。"
    exit 1
}

$src = Join-Path $tmp "tools"
if (-not (Test-Path (Join-Path $src "python.exe"))) {
    Write-Host "[错误] 包里没有 tools\python.exe，布局和预期不符（版本号写错了？）。"
    exit 1
}

if (Test-Path $dst) {
    Write-Host "[Python] 覆盖旧的 .pyruntime\ ..."
    Remove-Item -Recurse -Force $dst
}
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force
Remove-Item -Recurse -Force $tmp
Remove-Item -Force $zip -ErrorAction SilentlyContinue

# 自举 pip：nuget 包不带 pip，ensurepip 的轮子是内置的，不需要联网
& $exe -c "import pip" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[Python] 初始化 pip ..."
    & $exe -m ensurepip --default-pip
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] pip 初始化失败。"
        exit 1
    }
}

$ver = Get-PyVersion $exe
$pipv = ""
try { $pipv = ((& $exe -m pip --version 2>&1) | Out-String).Trim() } catch { }
Write-Host "[Python] 完成：$ver"
if ($pipv) { Write-Host "         $pipv" }
Write-Host "         位置：$dst"
Write-Host "         许可：PSF（见 $dst\LICENSE.txt）"
exit 0
