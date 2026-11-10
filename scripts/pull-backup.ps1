# Copy the newest Pi backup to this PC. Run on the PC (after `sudo sh deploy/backup-pi.sh` on the Pi):
#   powershell -ExecutionPolicy Bypass -File scripts\pull-backup.ps1
param([string]$Pi = "ham@192.168.5.157", [string]$Dest = "$env:USERPROFILE\Backups\hamcontrol")
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $Dest | Out-Null
$latest = (ssh $Pi "ls -1t /var/backups/rigbroker/hamcontrol-*.tar.gz | head -1").Trim()
if (-not $latest) { throw "no backup on the Pi - run: sudo sh deploy/backup-pi.sh" }
scp "${Pi}:$latest" "$Dest\"
Write-Host "saved $Dest\$(Split-Path $latest -Leaf)"
