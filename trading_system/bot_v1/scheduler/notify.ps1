# Windows toast notification (no extra modules). Usage: notify.ps1 -Title "..." -Text "..."
param([string]$Title = "Trading bot", [string]$Text = "")
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
  $t = $xml.GetElementsByTagName("text")
  $t.Item(0).AppendChild($xml.CreateTextNode($Title)) | Out-Null
  $t.Item(1).AppendChild($xml.CreateTextNode($Text)) | Out-Null
  $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
} catch { Write-Output "notify failed: $_" }
