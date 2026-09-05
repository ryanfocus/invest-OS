# 建立（或重建）OS 策略的每日排程。
#
# 這個檔案存在的理由：在它之前，「什麼時候跑」只存在於這台電腦的工作排程器裡，
# 而那是手動設的、沒有任何紀錄。電腦壞了、重灌了、換一台，那件事就消失了。
#
# 用法（PowerShell，不需要系統管理員）：
#
#     .\tools\setup_schedule.ps1              建立／覆蓋兩個排程
#     .\tools\setup_schedule.ps1 -Show        只看現況，不改東西
#     .\tools\setup_schedule.ps1 -Remove      全部移除
#
#     交付包裡這個腳本在最外層，用法一樣，路徑少一層 tools\
#
# ⚠️ **排程不管自動下單的開關。** 兩班一律每天叫起來，由程式自己讀
#    config/settings.yaml 決定要不要下單：
#      開關開 → 進場下單、出場平倉
#      開關關 → 只發訊號；出場那班讀不到部位記錄就安靜結束
#    把開關的狀態編進排程，等於多一個「開開關時會忘記」的地方。
#
# ⚠️ **排程掛了怎麼發現：平日早上沒收到訊號訊息。**
#    刻意不做心跳或偵測機制——每天那則訊號本身就是心跳，再加一則是噪音。
#    發現掛了就重跑這個腳本。

[CmdletBinding()]
param(
    [switch]$Show,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

# 這個腳本要在兩種形狀下都能用：
#
#   開發：invest-os\tools\setup_schedule.ps1  → 跑 .venv 裡的 python
#   交付：invest-os\setup_schedule.ps1        → 跑旁邊的 osmain.exe
#
# 做成一份而不是兩份，是因為兩份會漂——而漂掉的那份會在使用者的機器上
# 默默壞掉，而這套系統唯一的故障偵測是「早上沒收到 Discord」。
$Packaged = Test-Path (Join-Path $PSScriptRoot 'osmain.exe')
if ($Packaged) { $Root = $PSScriptRoot } else { $Root = Split-Path -Parent $PSScriptRoot }
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'

# 兩班的時間來自 SPEC：進場 08:50（開盤 08:45 之後五分鐘，讓報價就緒）、
# 出場 13:40（收盤 13:45 之前五分鐘的緩衝）。
$Jobs = @(
    @{ Name = 'invest-os 進場'; Stage = 'entry'; Time = '08:50' },
    @{ Name = 'invest-os 出場'; Stage = 'exit';  Time = '13:40' }
)

function Show-Jobs {
    $found = Get-ScheduledTask | Where-Object { $_.TaskName -like 'invest-os*' }
    if (-not $found) { Write-Host '  （目前沒有任何 invest-os 排程）'; return }
    foreach ($t in $found) {
        $info = $t | Get-ScheduledTaskInfo
        '  {0,-18} {1,-7} 下次={2}  上次={3} 結果={4}' -f `
            $t.TaskName, $t.State, $info.NextRunTime, $info.LastRunTime, $info.LastTaskResult
    }
}

if ($Show) {
    Write-Host "`n=== 目前的排程 ===" -ForegroundColor Cyan
    # 把判斷結果印出來。排查「排程好像跑錯地方」時第一個要看的就是它，
    # 而且這讓上面那個分支變得驗得到——2026-09-01 那個分支曾經是死碼
    # （舊的 $Root 賦值沒刪，無條件蓋掉判斷結果），而 -Show 剛好看不出來。
    $shape = if ($Packaged) { "交付包" } else { "開發" }
    Write-Host "  形狀：$shape    根目錄：$Root" -ForegroundColor DarkGray
    Show-Jobs
    return
}

# 先清掉舊的。重跑這個腳本時要能收斂到同一個結果，而不是越積越多——
# 過去手動設的那些用過即棄的排程就是這樣堆起來的。
Get-ScheduledTask | Where-Object { $_.TaskName -like 'invest-os*' } | ForEach-Object {
    Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
    Write-Host "  移除舊排程：$($_.TaskName)" -ForegroundColor DarkGray
}

if ($Remove) {
    Write-Host "`n已全部移除。" -ForegroundColor Yellow
    return
}

if (-not $Packaged -and -not (Test-Path $Python)) {
    throw "找不到 $Python —— 虛擬環境還沒建立？請看 docs/DEPLOY.md"
}
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# ⚠️ **真的寫一個檔案試試看。** 只檢查目錄存不存在是不夠的——交付包自己
#    帶了一個空的 logs\，所以解壓在 C:\Program Files\ 或磁碟根目錄這種
#    不可寫的位置時，目錄檢查會過、排程會建立、畫面會說「完成」，
#    然後每天靜默空轉——run_stage.cmd 的重導向失敗，一個字都不留。
$probe = Join-Path $LogDir '.write-test'
try {
    [System.IO.File]::WriteAllText($probe, 'x')
    Remove-Item $probe -Force
} catch {
    throw (
        "$LogDir 寫不進去（$($_.Exception.Message)）。" +
        "請把整個資料夾移到寫得進去的位置再重試，例如 C:\Users\<你>\invest-os。" +
        "留在這裡的話排程會建立成功，但每天什麼都不會發生、也不會留下紀錄。"
    )
}

$Runner = Join-Path $PSScriptRoot 'run_stage.cmd'
if (-not (Test-Path $Runner)) { throw "找不到 $Runner" }

foreach ($job in $Jobs) {
    # 實際執行的是 run_stage.cmd（開發在 tools\，交付包在最外層）——
    # 它自己算當天的日誌檔名。
    # 排程的參數是建立時就固定的字串，在這裡算日期會讓每天都寫進同一個檔。
    $action = New-ScheduledTaskAction -Execute $Runner -Argument $job.Stage -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Daily -At $job.Time
    # StartWhenAvailable：電腦當下沒開機的話，開機後補跑。
    #   ⚠️ 補跑對「訊號」有意義（至少留下觀測記錄），對「下單」意義不大——
    #      但程式自己會擋：開盤價的新鮮度檢查會發現那不是當日報價。
    # ExecutionTimeLimit：20 分鐘。取開盤價最多重試三次、每次隔一分鐘，
    #   加上對帳打三次期交所，正常不會超過五分鐘。
    # AllowStartIfOnBatteries / DontStopIfGoingOnBatteries：
    #   工作排程器的**預設是不跑**（DisallowStartIfOnBatteries=True）。
    #   筆電沒插電的話兩班都不會啟動——而 13:40 那班是刻意靜默的
    #   （「沒消息就是好消息」），所以部位會過夜而且**零通知**。
    #   那正是這套系統開宗明義要避免的事。跑到一半被拔電也一樣會被砍掉。
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

    Register-ScheduledTask -TaskName $job.Name -Action $action -Trigger $trigger `
        -Settings $settings -RunLevel Limited | Out-Null
    Write-Host "  建立：$($job.Name)  每天 $($job.Time)" -ForegroundColor Green
}

Write-Host "`n=== 完成 ===" -ForegroundColor Cyan
Show-Jobs

Write-Host "`n提醒：" -ForegroundColor Yellow
Write-Host '  排程只負責準時叫程式起床。要不要真的下單，由 config/settings.yaml'
Write-Host '  的 order.auto_enabled 決定——那是唯一的切換點。'
Write-Host '  平日早上沒收到訊號訊息 = 排程掛了，重跑這個腳本即可。'
