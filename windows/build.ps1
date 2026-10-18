# Builds build\yt-cuan-agent-win64.zip: the PyInstaller app folder + yt-cuan.ps1 + VERSION.
#   powershell -File windows\build.ps1 -Version v0.3.0
param([string]$Version = "dev")
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }

& $py -m pip install -q -r requirements.txt "pyinstaller>=6.11,<7" "pystray==0.19.5"
if ($LASTEXITCODE) { throw "pip install gagal" }

Remove-Item build -Recurse -Force -ErrorAction SilentlyContinue
# windowed: no console flashes for the service or the tray; `pair` attaches to the caller's console itself
& $py -m PyInstaller --noconfirm --clean --windowed --name yt-cuan-agent `
  --distpath build\dist --workpath build\work --specpath build `
  --paths $root --paths "$root\windows" `
  --add-data "$root\app\templates;app\templates" --add-data "$root\app\static;app\static" `
  --collect-submodules uvicorn --collect-submodules app --hidden-import tray `
  --collect-data googleapiclient --collect-data tzdata `
  "$root\windows\winmain.py"
if ($LASTEXITCODE) { throw "PyInstaller gagal" }

$rel = "build\release"
New-Item -ItemType Directory $rel | Out-Null
Copy-Item build\dist\yt-cuan-agent "$rel\app" -Recurse
Copy-Item windows\yt-cuan.ps1 $rel
Set-Content "$rel\VERSION" $Version -Encoding ascii -NoNewline
Compress-Archive "$rel\*" build\yt-cuan-agent-win64.zip -Force
Write-Host "OK build\yt-cuan-agent-win64.zip ($([math]::Round((Get-Item build\yt-cuan-agent-win64.zip).Length / 1MB)) MB)"
