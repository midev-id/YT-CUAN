# YT-CUAN agent for Windows: install and daily commands. Lives in C:\YT-CUAN\yt-cuan.ps1 after install.
#   yt-cuan install [-Code KODE]   (run by install.ps1)
#   yt-cuan status | restart | update | pair KODE | password | uninstall
param(
  [Parameter(Position = 0)][string]$Command = "status",
  [Parameter(Position = 1)][string]$Code = "",
  [string]$Source = "",          # folder with the extracted release (install/update)
  [switch]$Pause                  # keep the window open (used by the tray's UAC launches)
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x slower with the progress bar
$HubUrl = "https://ytcuan.autopreneur.id"
$Home_ = "C:\YT-CUAN"
$Exe = "$Home_\app\yt-cuan-agent.exe"
$Task = "YT-CUAN Agent"
$FfmpegZip = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"

function Say($m) { Write-Host "`n$m" -ForegroundColor Cyan }
function Die($m) { Write-Host "`nGagal: $m" -ForegroundColor Red; if ($Pause) { Read-Host "Tekan Enter untuk menutup" }; exit 1 }
function Need-Admin {
  $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Die "jalankan PowerShell sebagai Administrator." }
}
function Health { try { Invoke-RestMethod "http://127.0.0.1:8000/healthz" -TimeoutSec 3 } catch { $null } }
function Wait-Healthy {
  foreach ($i in 1..30) { if (Health) { return $true }; Start-Sleep 2 }
  return $false
}
function Stop-Agent {
  Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
  Get-Process yt-cuan-agent -ErrorAction SilentlyContinue | Stop-Process -Force   # agent + tray
  Start-Sleep 2
}
function Start-Agent { Start-ScheduledTask -TaskName $Task }
function Start-Tray {
  # tray belongs to the logged-in desktop user, not to this elevated shell's session
  $u = (Get-CimInstance Win32_ComputerSystem).UserName
  if ($u) {
    $a = New-ScheduledTaskAction -Execute $Exe -Argument "tray"
    Register-ScheduledTask -TaskName "YT-CUAN Tray once" -Action $a -User $u -Force | Out-Null
    Start-ScheduledTask -TaskName "YT-CUAN Tray once"
    Start-Sleep 2
    Unregister-ScheduledTask -TaskName "YT-CUAN Tray once" -Confirm:$false
  }
}
function Copy-Release($src) {
  if (-not (Test-Path "$src\app\yt-cuan-agent.exe")) { Die "paket rilis tidak lengkap ($src)." }
  if (Test-Path "$Home_\app") { Remove-Item "$Home_\app.old" -Recurse -Force -ErrorAction SilentlyContinue; Rename-Item "$Home_\app" "app.old" }
  Copy-Item "$src\app" "$Home_\app" -Recurse
  Copy-Item "$src\yt-cuan.ps1", "$src\VERSION" $Home_ -Force
  Set-Acls
}
function Set-Acls {
  # tokens, keys and the dashboard password: SYSTEM and Administrators only.
  # The program itself must stay readable so the tray can run as the normal (non-elevated) desktop user.
  icacls $Home_ /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null
  icacls "$Home_\app" /grant "Users:(OI)(CI)RX" | Out-Null
  icacls "$Home_\VERSION" /grant "Users:R" | Out-Null
}
function Get-Release {
  $tmp = Join-Path $env:TEMP "yt-cuan-release"
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory $tmp | Out-Null
  Invoke-WebRequest "$HubUrl/download/yt-cuan-agent-win64.zip?t=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())" -OutFile "$tmp\r.zip" -UseBasicParsing
  Expand-Archive "$tmp\r.zip" $tmp
  return $tmp
}
function Random-Text($n) { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count $n | ForEach-Object { [char]$_ }) }

switch ($Command) {
  "install" {
    Need-Admin
    if (-not [Environment]::Is64BitOperatingSystem) { Die "butuh Windows 64-bit." }
    New-Item -ItemType Directory -Force "$Home_\data", "$Home_\logs", "$Home_\ffmpeg" | Out-Null

    if (-not (Test-Path "$Home_\ffmpeg\ffmpeg.exe")) {
      Say "Mengunduh ffmpeg (~100 MB)..."
      $z = Join-Path $env:TEMP "ffmpeg.zip"
      Invoke-WebRequest $FfmpegZip -OutFile $z -UseBasicParsing
      $x = Join-Path $env:TEMP "ffmpeg-x"; Remove-Item $x -Recurse -Force -ErrorAction SilentlyContinue
      Expand-Archive $z $x
      Get-ChildItem $x -Recurse -Include ffmpeg.exe, ffprobe.exe | Copy-Item -Destination "$Home_\ffmpeg"
      Remove-Item $z, $x -Recurse -Force
    }

    Stop-Agent
    if (-not $Source) { Say "Mengunduh YT-CUAN..."; $Source = Get-Release }
    Copy-Release $Source

    if (-not (Test-Path "$Home_\.env")) {
      Say "Membuat konfigurasi..."
      $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
      $key = [Convert]::ToBase64String($b).Replace('+', '-').Replace('/', '_')
      @(
        "# Dibuat installer $(Get-Date -Format s). SECRET_KEY jangan diganti: token channel tidak bisa dibaca lagi.",
        "SECRET_KEY=$key", "ADMIN_USER=admin", "ADMIN_PASSWORD=$(Random-Text 16)",
        "BASE_URL=http://localhost:8000", "HUB_URL=$HubUrl"
      ) | Set-Content "$Home_\.env" -Encoding ascii
    }
    Set-Acls

    if ($Code) {
      Say "Memasangkan ke hub..."
      & $Exe pair $Code | Out-Host
      if ($LASTEXITCODE -ne 0) { Die "kode pairing salah atau kedaluwarsa. Buat kode baru di hub, lalu: yt-cuan pair <KODE>" }
    }

    Say "Mendaftarkan service..."
    $act = New-ScheduledTaskAction -Execute $Exe -Argument "serve" -WorkingDirectory $Home_
    $trg = New-ScheduledTaskTrigger -AtStartup
    $pri = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
      -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $Task -Action $act -Trigger $trg -Principal $pri -Settings $set -Force | Out-Null
    # "Perbarui" from the hub: the agent runs this task; separate from the agent task so stopping the agent doesn't kill it
    $uact = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Home_\yt-cuan.ps1`" update"
    $uset = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries
    Register-ScheduledTask -TaskName "YT-CUAN Update" -Action $uact -Principal $pri -Settings $uset -Force | Out-Null
    Start-Agent

    # tray for every user at logon + desktop shortcut to the dashboard
    $sh = New-Object -ComObject WScript.Shell
    $lnk = $sh.CreateShortcut("$env:ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp\YT-CUAN.lnk")
    $lnk.TargetPath = $Exe; $lnk.Arguments = "tray"; $lnk.Save()
    "[InternetShortcut]`r`nURL=http://localhost:8000/" | Set-Content "$env:PUBLIC\Desktop\YT-CUAN Dashboard.url" -Encoding ascii

    # Windows Update: no automatic restarts between 05:00 and 23:00 (max window Windows allows is 18 hours)
    $wu = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
    if (Test-Path $wu) { Set-ItemProperty $wu ActiveHoursStart 5; Set-ItemProperty $wu ActiveHoursEnd 23 }

    # `yt-cuan` command in any new terminal
    "@powershell -NoProfile -ExecutionPolicy Bypass -File `"$Home_\yt-cuan.ps1`" %*" | Set-Content "$Home_\yt-cuan.cmd" -Encoding ascii
    $path = [Environment]::GetEnvironmentVariable("Path", "Machine")
    if ($path -notlike "*$Home_*") { [Environment]::SetEnvironmentVariable("Path", "$path;$Home_", "Machine") }

    Say "Menunggu aplikasi siap..."
    if (-not (Wait-Healthy)) { Die "agent tidak menyala. Cek log: $Home_\logs\agent.log" }
    Start-Tray
    $pw = (Get-Content "$Home_\.env" | Where-Object { $_ -like "ADMIN_PASSWORD=*" }) -replace "ADMIN_PASSWORD=", ""
    Write-Host "`nYT-CUAN terpasang." -ForegroundColor Green
    if ($Code) { Write-Host "Server ini sudah muncul di hub: $HubUrl" } else { Write-Host "Belum dipasangkan ke hub. Buat kode di $HubUrl lalu: yt-cuan pair <KODE>" }
    Write-Host "Dashboard lokal: ikon 'YT-CUAN Dashboard' di desktop (user admin, password $pw)."
    Write-Host "Ikon YT-CUAN di pojok taskbar menunjukkan status live."
  }
  "update" {
    Need-Admin
    Say "Mengunduh versi terbaru..."
    $src = Get-Release
    if ((Get-Content "$src\VERSION") -eq (Get-Content "$Home_\VERSION" -ErrorAction SilentlyContinue)) { Write-Host "Sudah versi terbaru."; break }
    Stop-Agent
    Copy-Release $src
    Start-Agent
    if (-not (Wait-Healthy)) {
      Say "Versi baru tidak menyala, kembali ke versi sebelumnya..."
      Stop-Agent; Remove-Item "$Home_\app" -Recurse -Force; Rename-Item "$Home_\app.old" "app"; Start-Agent
      Die "pembaruan dibatalkan. Cek log: $Home_\logs\agent.log"
    }
    Start-Tray
    Write-Host "Diperbarui ke $(Get-Content "$Home_\VERSION")." -ForegroundColor Green
  }
  "restart" { Need-Admin; Stop-Agent; Start-Agent; if (Wait-Healthy) { Start-Tray; Write-Host "Agent jalan lagi." -ForegroundColor Green } else { Die "agent tidak menyala. Cek $Home_\logs\agent.log" } }
  "status" {
    $h = Health
    if (-not $h) { Write-Host "Agent tidak jalan." -ForegroundColor Red } else {
      Write-Host "Versi $($h.version) | $($h.live) live | $($h.keys_running) key jalan | $($h.keys_error) key error"
      Write-Host ("Hub: " + $(if ($h.hub.connected) { "tersambung" } else { $h.hub.error }))
    }
  }
  "pair" { Need-Admin; if (-not $Code) { Die "pakai: yt-cuan pair <KODE>" }; & $Exe pair $Code | Out-Host; if ($LASTEXITCODE -eq 0) { Stop-Agent; Start-Agent; Start-Tray } }
  "password" { Need-Admin; Get-Content "$Home_\.env" | Where-Object { $_ -match "^ADMIN_(USER|PASSWORD)=" } }
  "uninstall" {
    Need-Admin
    if ((Read-Host "Hapus YT-CUAN dari server ini? Data dan media di $Home_ ikut terhapus. Ketik HAPUS") -ne "HAPUS") { break }
    Stop-Agent
    Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp\YT-CUAN.lnk", "$env:PUBLIC\Desktop\YT-CUAN Dashboard.url" -ErrorAction SilentlyContinue
    $path = ([Environment]::GetEnvironmentVariable("Path", "Machine") -split ";" | Where-Object { $_ -ne $Home_ }) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $path, "Machine")
    Set-Location $env:TEMP; Remove-Item $Home_ -Recurse -Force
    Write-Host "YT-CUAN dihapus."
  }
  default { Write-Host "Pakai: yt-cuan {status|restart|update|pair <KODE>|password|uninstall}" }
}
if ($Pause) { Read-Host "`nTekan Enter untuk menutup" }
