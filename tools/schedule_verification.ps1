# 明天（交易日）兩項零風險驗證的排程設定。
#
#   .\tools\schedule_verification.ps1                    建立排程
#   .\tools\schedule_verification.ps1 -Date 2026-08-18   指定日期
#   .\tools\schedule_verification.ps1 -Remove            移除排程
#
# ⚠️ 這兩支工具**都不會下單**。verify_order_path 啟動時會掃自己的語法樹確認
#    沒有呼叫任何送單函式；compare_open 只讀報價。
#
# ⚠️ 兩支都會登入群益，所以**刻意錯開時間**——同一組帳密兩個連線可能互踢。
#    compare_open 約一分鐘跑完，listener 六分鐘後才開始。
#
# 排程用「只在使用者登入時執行」：群益登入需要**安裝在這個使用者底下的憑證**，
# 跑在 session 0 未必找得到。代價是電腦要保持登入狀態（可以鎖螢幕）。

param(
    [string]$Date = "",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$Root       = Split-Path -Parent $PSScriptRoot
$Python     = Join-Path $Root ".venv\Scripts\python.exe"
$LogDir     = Join-Path $Root "logs"
$TaskCompare = "invest-os 開盤價比對"
$TaskListen  = "invest-os 回報監聽"

if ($Remove) {
    foreach ($name in @($TaskCompare, $TaskListen)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "  已移除：$name"
        } else {
            Write-Host "  不存在：$name"
        }
    }
    exit 0
}

if (-not (Test-Path $Python)) { throw "找不到 venv python：$Python" }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# 預設抓「下一個平日」。手動指定日期時直接用指定的。
if ($Date -eq "") {
    $target = (Get-Date).Date.AddDays(1)
    while ($target.DayOfWeek -eq "Saturday" -or $target.DayOfWeek -eq "Sunday") {
        $target = $target.AddDays(1)
    }
} else {
    $target = [datetime]::ParseExact($Date, "yyyy-MM-dd", $null)
}

# 兩項工作與各自的起跑時間。
#   08:44 開盤價比對——要趕在 08:45 開盤那一刻前後把報價抓下來
#   08:50 回報監聽——等前一支跑完再開始，避免兩個連線互踢
#            聽到 13:45 收盤，這樣你**盤中任何時候**下單都會被抓到
$jobs = @(
    @{ Name = $TaskCompare
       Time = $target.Date.AddHours(8).AddMinutes(44)
       Script = "tools\compare_open.py"
       Args = ""
       Log = "compare_open" },
    @{ Name = $TaskListen
       Time = $target.Date.AddHours(8).AddMinutes(50)
       Script = "tools\verify_order_path.py"
       # 08:50 → 13:45 收盤，約 17700 秒。聽整個盤，不必你配合時間下單。
       Args = "--listen 17700"
       Log = "verify_order_path" }
)

Write-Host ""
Write-Host "目標日期：$($target.ToString('yyyy-MM-dd (dddd)'))"
Write-Host ""

foreach ($job in $jobs) {
    $stamp = $target.ToString("yyyyMMdd")
    $log = Join-Path $LogDir "$($job.Log)-$stamp.log"

    # 用 cmd 包一層才能把 stdout/stderr 都導到檔案——排程的主控台輸出沒人看得到，
    # 不導的話跑完等於沒跑。用 >> 附加而不是 > 覆寫：重試時要看得到前一次
    # 失敗的原因，覆寫掉就等於把線索丟了。
    $inner = "`"$Python`" -X utf8 `"$(Join-Path $Root $job.Script)`" $($job.Args)"
    $cmd = "/c chcp 65001 >nul & $inner >> `"$log`" 2>&1"

    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Once -At $job.Time
    # 失敗就重試——這是無人值守的一天，沒有人會發現它沒跑起來。
    # 「螢幕鎖住時群益 COM 能不能正常運作」目前**沒有驗證過**，
    # 萬一第一次失敗，重試讓它還有機會在盤中補上。
    $settings = New-ScheduledTaskSettingsSet `
        -WakeToRun `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 20) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 6)

    if (Get-ScheduledTask -TaskName $job.Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $job.Name -Confirm:$false
    }
    Register-ScheduledTask -TaskName $job.Name -Action $action -Trigger $trigger `
        -Settings $settings -Description "invest-os 零風險驗證，不會下單" | Out-Null

    Write-Host ("  {0,-22} {1}  →  logs\{2}" -f $job.Name, $job.Time.ToString("HH:mm"), (Split-Path $log -Leaf))
}

Write-Host ""
Write-Host "完成。注意事項："
Write-Host "  1. 電腦保持**登入狀態**（可以鎖螢幕）——群益登入要用你帳號底下的憑證"
Write-Host "  2. 插著電，你這台 AC 模式設定為永不睡眠"
Write-Host "  3. 監聽那支會跑到收盤。**盤中任何時候**在群益 APP 下單都會被抓到"
Write-Host "  4. 移除：.\tools\schedule_verification.ps1 -Remove"
Write-Host ""
