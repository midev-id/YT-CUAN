# YT-CUAN installer for Windows VPS, served by the hub. Run in PowerShell as Administrator:
#   irm https://ytcuan.autopreneur.id/install.ps1 | iex; Install-YtCuan KODE
function Install-YtCuan([string]$Code = "") {
  $ErrorActionPreference = "Stop"
  $ProgressPreference = "SilentlyContinue"
  $hub = "https://ytcuan.autopreneur.id"
  $tmp = Join-Path $env:TEMP "yt-cuan-release"
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory $tmp | Out-Null
  Write-Host "`nMengunduh YT-CUAN..." -ForegroundColor Cyan
  Invoke-WebRequest "$hub/download/yt-cuan-agent-win64.zip?t=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())" -OutFile "$tmp\r.zip" -UseBasicParsing
  Expand-Archive "$tmp\r.zip" $tmp
  & powershell -NoProfile -ExecutionPolicy Bypass -File "$tmp\yt-cuan.ps1" install -Code $Code -Source $tmp
}
