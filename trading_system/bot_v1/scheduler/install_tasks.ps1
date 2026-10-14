# Creates the bot's Windows scheduled tasks (current user, runs while you are logged in).
#   TradingBot\Login reminder   weekdays 09:10  opens login.bat (Upstox needs a fresh login daily)
#   TradingBot\Afternoon 1510   weekdays 15:10  month-start paper rebalance + expiry-eve condor scan
#   TradingBot\Close 1540       weekdays 15:40  daily mark + condor settlement
# Re-running replaces them. Remove with uninstall_tasks.ps1.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$days = "Monday","Tuesday","Wednesday","Thursday","Friday"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 25) -MultipleInstances IgnoreNew
function Add-BotTask($name, $time, $file) {
  $action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$file`"`"" -WorkingDirectory $here
  $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $time
  Register-ScheduledTask -TaskPath "\TradingBot\" -TaskName $name -Action $action -Trigger $trigger `
                         -Settings $settings -Description "bot_v1 (paper trading) - $name" -Force | Out-Null
  Write-Output "installed: \TradingBot\$name at $time on weekdays"
}
Add-BotTask "Login reminder" "09:10" (Join-Path $here "login.bat")
Add-BotTask "Afternoon 1510" "15:10" (Join-Path $here "run_auto.bat")
Add-BotTask "Close 1540"     "15:40" (Join-Path $here "run_auto.bat")
Get-ScheduledTask -TaskPath "\TradingBot\" | Select-Object TaskName, State | Format-Table -AutoSize
