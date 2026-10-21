#requires -Version 7.0
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host '== 运行测试 ==' -ForegroundColor Cyan
$env:UV_CACHE_DIR = Join-Path $PSScriptRoot '.uv-cache'
uv run --frozen python -m unittest discover -s tests -v

Write-Host '== 打包 ==' -ForegroundColor Cyan
if (Get-Command pyinstaller -ErrorAction SilentlyContinue) {
    pyinstaller OffCraft.spec --noconfirm --clean
} else {
    uv run --with pyinstaller pyinstaller OffCraft.spec --noconfirm --clean
}

$exe = Join-Path $PSScriptRoot 'dist\OffCraft.exe'
if (-not (Test-Path $exe)) { throw "打包失败：$exe 不存在" }
$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "== 完成：$exe ($size MB) ==" -ForegroundColor Green
