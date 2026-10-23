# 打包 Windows 安装包（Inno Setup 6）。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_installer.ps1
#   powershell ... -Version 0.5.4        # 手动指定版本
#
# 前置：先跑 pyinstaller CrMCDownload.spec（产物 dist\CrMCDownload）；
#       本机需安装 Inno Setup 6（默认 C:\Program Files (x86)\Inno Setup 6\ISCC.exe）。
param(
    [string]$Version = '',
    [string]$Iscc = '',
    [string]$Payload = '',
    [switch]$SkipPayloadCheck
)
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$iss = Join-Path $PSScriptRoot 'CrMCDownload.iss'

if (-not $Version) {
    $init = Get-Content (Join-Path $root 'offcraft\__init__.py') -Encoding UTF8 -Raw
    $m = [regex]::Match($init, '__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"')
    if (-not $m.Success) { throw '无法从 offcraft/__init__.py 读取版本号' }
    $Version = $m.Groups[1].Value
}

if (-not $Iscc) {
    $candidates = @(
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    )
    $Iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Iscc) { throw '未找到 ISCC.exe（请安装 Inno Setup 6）' }
}

if (-not $Payload) { $Payload = Join-Path $root 'dist\CrMCDownload' }
$exe = Join-Path $Payload 'CrMCDownload.exe'
if (-not $SkipPayloadCheck) {
    if (-not (Test-Path $exe)) {
        throw ('未找到 {0}，先执行：cd user; pyinstaller CrMCDownload.spec --noconfirm --clean' -f $exe)
    }
    $fv = (Get-Item $exe).VersionInfo.FileVersion
    if ($fv -ne $Version) {
        throw ('exe 版本是 {0}，与脚本版本 {1} 不一致，请先重新打包' -f $fv, $Version)
    }
}

$outDir = Join-Path $root 'dist\installer'
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }

Write-Host ('== 编译安装包：版本 {0}，payload {1} ==' -f $Version, $Payload)
& $Iscc ('/DMyAppVersion=' + $Version) ('/DPayloadDir=' + $Payload) $iss
if ($LASTEXITCODE -ne 0) { throw ('ISCC 失败，退出码 {0}' -f $LASTEXITCODE) }

$setup = Join-Path $outDir ('CrMCDownload-{0}-win64-setup.exe' -f $Version)
if (-not (Test-Path $setup)) { throw ('未生成 {0}' -f $setup) }
$item = Get-Item $setup
$vi = $item.VersionInfo
Write-Host ('== 完成：{0}' -f $setup) -ForegroundColor Green
Write-Host ('   size={0:N0} bytes  FileVersion={1}  ProductVersion={2}  Company={3}' -f $item.Length, $vi.FileVersion, $vi.ProductVersion, $vi.CompanyName)
