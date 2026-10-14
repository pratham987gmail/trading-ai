# Creates the bot's Windows scheduled tasks (current user, runs while you are logged in).
#   -Mode Laptop (default): the bot runs on this laptop
#     TradingBot\Login reminder   weekdays 09:10  opens login.bat (Upstox needs a fresh login daily)
#     TradingBot\Afternoon 1510   weekdays 15:10  month-start paper rebalance + expiry-eve condor scan
#     TradingBot\Close 1540       weekdays 15:40  daily mark + condor settlement
#     TradingBot\Azure pull 1615  weekdays 16:15  copies new Azure recorder data into data\cloud
#   -Mode Vm: the bot runs on the Azure VM (cron); the laptop keeps only
#     Login reminder (login.bat also copies the token to the VM) and Azure pull (also pulls the bot's state)
# Re-running replaces them. Remove all with uninstall_tasks.ps1.
param([ValidateSet("Laptop", "Vm")] [string] $Mode = "Laptop")
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
Add-BotTask "Azure pull 1615" "16:15" (Join-Path $here "pull_azure.bat")
if ($Mode -eq "Laptop") {
  Add-BotTask "Afternoon 1510" "15:10" (Join-Path $here "run_auto.bat")
  Add-BotTask "Close 1540"     "15:40" (Join-Path $here "run_auto.bat")
} else {
  foreach ($n in "Afternoon 1510", "Close 1540") {
    Unregister-ScheduledTask -TaskPath "\TradingBot\" -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "removed: \TradingBot\$n (the bot now runs on the Azure VM)"
  }
}
Get-ScheduledTask -TaskPath "\TradingBot\" | Select-Object TaskName, State | Format-Table -AutoSize
