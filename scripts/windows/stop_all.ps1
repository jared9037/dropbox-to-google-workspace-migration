# stop_all.ps1 -- stops every account (closes the rclone windows). Safe: re-run run_all.ps1 later to resume.
#   powershell -ExecutionPolicy Bypass -File C:\migration\scripts\windows\stop_all.ps1
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match 'run_account\.ps1|run_all\.ps1' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Get-Process rclone -ErrorAction SilentlyContinue | Stop-Process -Force
"Stopped. rclone processes left: " + @(Get-Process rclone -ErrorAction SilentlyContinue).Count
