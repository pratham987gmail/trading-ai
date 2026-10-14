Get-ScheduledTask -TaskPath "\TradingBot\" -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
Write-Output "TradingBot scheduled tasks removed."
