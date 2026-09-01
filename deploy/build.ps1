# 打包交付包。產物在 deploy\dist\invest-os\，整個資料夾壓縮起來就是要給出去的東西。
#
# 用法（PowerShell，不需要系統管理員）：
#
#     .\deploy\build.ps1
#     .\deploy\build.ps1 -SkipBuild     只重新組裝，不重跑 PyInstaller（快）
#
# ══ 這個腳本最重要的工作不是打包，是擋住機密 ══
#
# repo 裡有四種東西絕對不能交付出去：
#
#   .env          期貨帳密，可以直接下單
#   logs\         券商原始回覆（含期貨帳號）、群益元件自己寫的日誌（含身分證字號）
#   state\        部位與稽核歷史
#   工作區的 config\settings.yaml   很可能開著自動下單、口數也不是 1
#
# 最後那一項最容易出事，因為它「看起來就是該交付的檔案」。所以設定檔
# **從版控裡取**（git show HEAD:...），不是從工作區複製——版控裡那份有一條
# 測試守著開關必須是關的。
#
# 組裝完成後會再驗一次（見結尾的檢查），不通過就把產物刪掉、不留半成品。

[CmdletBinding()]
param(
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'

$Deploy = $PSScriptRoot
$Root = Split-Path -Parent $Deploy
$BuildVenv = Join-Path $Deploy '.build-venv'
$Dist = Join-Path $Deploy 'dist'
$Bundle = Join-Path $Dist 'invest-os'

Write-Host "`n=== 打包 invest-os 交付包 ===" -ForegroundColor Cyan

# ── 1. 建置用的虛擬環境 ──────────────────────────────────────────────
#
# 刻意**不用專案的 .venv**。那個環境是線上系統每天在跑的東西，
# 往裡面裝 PyInstaller 等於為了打包去動生產環境。
$BuildPy = Join-Path $BuildVenv 'Scripts\python.exe'
if (-not (Test-Path $BuildPy)) {
    Write-Host "  建立建置環境（第一次會久一點）…" -ForegroundColor DarkGray
    & (Join-Path $Root '.venv\Scripts\python.exe') -m venv $BuildVenv
    & $BuildPy -m pip install --quiet --upgrade pip
    & $BuildPy -m pip install --quiet -r (Join-Path $Root 'requirements.txt')
    & $BuildPy -m pip install --quiet pyinstaller
}

# ── 2. PyInstaller ───────────────────────────────────────────────────
if (-not $SkipBuild) {
    Write-Host "  打包中…" -ForegroundColor DarkGray

    # --python-option "X utf8"
    #   凍結後 stdout 會變成系統編碼（這台是 cp950），而程式裡有 22 處 emoji——
    #   print 到那些字會直接 UnicodeEncodeError。
    #   ⚠️ PYTHONUTF8=1 與 PYTHONIOENCODING 對凍結的 exe **無效**（實測過），
    #      因為 bootloader 用 isolated config 啟動直譯器。只有這個旗標有用。
    #
    # --exclude-module comtypes.gen.*
    #   不要把「這台機器產生的 SKCOM 介面定義」打包進去。
    #   打包進去的話，對方機器上的群益就算是別的版本也會沿用我們這份——
    #   而凍結會讓 comtypes 的版本自癒檢查失效（見 broker/capital.py 的
    #   discard_generated_com_wrapper）。排除掉，它就會照對方的 dll 現場產生。
    $args = @(
        '-m', 'PyInstaller', '--noconfirm', '--onedir', '--name', 'osmain',
        '--python-option', 'X utf8',
        '--exclude-module', 'comtypes.gen.SKCOMLib',
        '--exclude-module', 'comtypes.gen._75AAD71C_8F4F_4F1F_9AEE_3D41A8C9BA5E_0_1_0',
        '--distpath', (Join-Path $Deploy 'build-dist'),
        '--workpath', (Join-Path $Deploy 'build-work'),
        '--specpath', (Join-Path $Deploy 'build-spec'),
        (Join-Path $Root 'main.py')
    )
    Push-Location $Root
    try { & $BuildPy @args | Out-Null } finally { Pop-Location }
}

$Frozen = Join-Path $Deploy 'build-dist\osmain'
if (-not (Test-Path (Join-Path $Frozen 'osmain.exe'))) {
    throw "找不到 osmain.exe —— PyInstaller 沒有跑成功？"
}

# ── 3. 組裝交付包 ────────────────────────────────────────────────────
Write-Host "  組裝…" -ForegroundColor DarkGray
if (Test-Path $Bundle) { Remove-Item -Recurse -Force $Bundle }
New-Item -ItemType Directory -Path $Bundle -Force | Out-Null

Copy-Item (Join-Path $Frozen '*') $Bundle -Recurse

# 排程與執行的兩支腳本。setup_schedule.ps1 是同一份（它自己認得出兩種形狀），
# run_stage.cmd 分兩份——cmd 的編碼陷阱太多，不在裡面做分支。
Copy-Item (Join-Path $Root 'tools\setup_schedule.ps1') $Bundle
Copy-Item (Join-Path $Deploy 'templates\run_stage.cmd') $Bundle

# ⚠️ 設定檔取自版控，不是工作區。理由見檔頭。
New-Item -ItemType Directory -Path (Join-Path $Bundle 'config') -Force | Out-Null
#
# ⚠️ **編碼要自己顧。** PowerShell 5.1 會用主控台的編碼（這台是 cp950）
#    去解讀外部程式的輸出，所以 `git show ... | Set-Content` 會把 UTF-8 的
#    中文在進到 Set-Content 之前就毀掉——交付出去的設定檔說明會是一堆問號，
#    而那份檔案的價值有一半在那些說明上。
$prevEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Push-Location $Root
try {
    $yaml = & git show HEAD:config/settings.yaml
} finally {
    Pop-Location
    [Console]::OutputEncoding = $prevEnc
}
# 不寫 BOM：yaml 不需要，而有些解析器會把它當成內容的一部分
[System.IO.File]::WriteAllLines(
    (Join-Path $Bundle 'config\settings.yaml'), $yaml,
    (New-Object System.Text.UTF8Encoding $false))

Copy-Item (Join-Path $Root '.env.example') $Bundle
Copy-Item (Join-Path $Deploy 'templates\README.md') $Bundle
New-Item -ItemType Directory -Path (Join-Path $Bundle 'docs') -Force | Out-Null
foreach ($d in 'MESSAGES.md', 'LOGIN_SETUP.md') {
    Copy-Item (Join-Path $Root "docs\$d") (Join-Path $Bundle 'docs')
}

# 空的資料夾，讓對方一眼看得出東西會長在哪
foreach ($d in 'logs', 'state') {
    New-Item -ItemType Directory -Path (Join-Path $Bundle $d) -Force | Out-Null
}

# ── 4. 交付前的檢查。不通過就不留半成品 ──────────────────────────────
Write-Host "  檢查…" -ForegroundColor DarkGray
$problems = @()

if (Test-Path (Join-Path $Bundle '.env')) { $problems += '交付包裡有 .env（期貨帳密）' }

$settings = Join-Path $Bundle 'config\settings.yaml'
# YAML 的「真」不只有 true。settings.py 自己的註解就寫過：yes / on / True 都是 True。
# 只擋 true 的話，寫成 on 就整包帶著開著的開關出貨。
$armed = Select-String -Path $settings -Pattern '^\s*auto_enabled:\s*(true|yes|on)\s*(#.*)?$' -Quiet
if ($armed) { $problems += 'settings.yaml 的自動下單是開著的' }

# 出貨的口數必須是 1。這裡取的是版控裡那份，而版控裡那份會跟著開發時的
# 實驗跑（有人為了測試改成 2 又順手 commit 了）。對方拿到之後只要一開開關
# 就是那個口數的真單——不能讓它從我們的實驗值繼承過去。
# 抓不到時**不可以拋例外**。無匹配時 .Matches.Groups[1] 會擲 RuntimeException，
# 而 $ErrorActionPreference='Stop' 之下腳本當場中止——下面那個「檢查沒過就刪掉產物」
# 根本輪不到執行，半成品留在磁碟上，正好與檔頭寫的「不留半成品」相反。
$lotsHits = @(Select-String -Path $settings -Pattern '^\s*lots:\s*(\d+)')
if ($lotsHits.Count -ne 1) {
    $problems += "settings.yaml 裡找到 $($lotsHits.Count) 行 lots:，預期剛好 1 行"
} elseif ($lotsHits[0].Matches.Groups[1].Value -ne '1') {
    $problems += "settings.yaml 的口數是 $($lotsHits[0].Matches.Groups[1].Value)，出貨必須是 1"
}

# 中文說明有沒有在複製過程中被毀掉。那些說明是這個檔案的一半價值。
if (-not (Select-String -Path $settings -Pattern '自動下單總開關' -Quiet)) {
    $problems += 'settings.yaml 的中文說明壞掉了（編碼問題？）'
}

# 該在的東西在不在。run_stage.cmd 特別重要——**每日日誌完全來自它的 >> 重導向**，
# 程式裡沒有任何 FileHandler。少了它，排程照樣跑、照樣下單，但一個字都不會留下，
# 而唯一的故障偵測是「早上沒收到 Discord」，那不會為「有跑但跑歪」觸發。
foreach ($f in 'osmain.exe', 'run_stage.cmd', 'setup_schedule.ps1', 'README.md', '.env.example', 'config\settings.yaml') {
    if (-not (Test-Path (Join-Path $Bundle $f))) { $problems += "交付包裡少了 $f" }
}

foreach ($d in 'logs', 'state') {
    $files = Get-ChildItem (Join-Path $Bundle $d) -File -Recurse -ErrorAction SilentlyContinue
    if ($files) { $problems += "$d\ 不是空的（$($files.Count) 個檔案）" }
}

# 帳號有沒有跟著出去。用 .env 裡的實際值去比對，不是靠檔名判斷。
#
# ⚠️ **完整值與前綴要分開處理。** 分公司代碼只有 4 個字元，拿它去掃
#    幾 MB 的 .pyd / .dll 一定會隨機撞上（第一次跑就在 unicodedata.pyd
#    誤報）。誤報的代價不只是煩——會讓人習慣性忽略這道檢查，
#    然後真的漏了帳密那次也不會有人看。
#
#    所以：完整值（11 碼以上）到處掃，前綴只掃我們自己放進去的文字檔。
$TextLike = '.yaml', '.yml', '.md', '.txt', '.cmd', '.ps1', '.json', '.jsonl', '.example', '.env'

$envFile = Join-Path $Root '.env'
if (Test-Path $envFile) {
    $full = @(); $prefix = @()
    foreach ($line in Get-Content $envFile) {
        # 四個都要掃。只掃帳號不掃密碼與 webhook，與檔頭「.env 是可以直接下單的期貨帳密」
        # 那句話不符——漏掉的那兩個才是真正能直接拿去用的。
        if ($line -match '^\s*(CAPITAL_USER_ID|CAPITAL_PASSWORD|CAPITAL_FUTURES_ACCOUNT|DISCORD_WEBHOOK_URL)\s*=\s*(.+?)\s*$') {
            $v = $Matches[2]
            if ($v.Length -ge 8) { $full += $v }
            # ⚠️ **前綴只對期貨帳號有意義。** 那 4 碼是分公司代碼，是識別的那一段。
            #    對密碼取前 4 碼、對 webhook 取到 'http'，撞到設定檔是必然的
            #    （實測就撞了）。而誤報的代價是讓人習慣忽略這道檢查。
            if ($Matches[1] -eq 'CAPITAL_FUTURES_ACCOUNT' -and $v.Length -ge 4) {
                $prefix += $v.Substring(0, 4)
            }
        }
    }
    $allFiles = Get-ChildItem $Bundle -Recurse -File
    $textFiles = $allFiles | Where-Object {
        $TextLike -contains $_.Extension -or $_.Extension -eq ''
    }
    foreach ($s in $full) {
        $hit = $allFiles | Where-Object { $_.Length -lt 20MB } |
            Select-String -Pattern ([regex]::Escape($s)) -List -ErrorAction SilentlyContinue
        if ($hit) { $problems += "交付包裡出現完整帳號：$($hit[0].Path)" }
    }
    foreach ($s in $prefix) {
        $hit = $textFiles |
            Select-String -Pattern ([regex]::Escape($s)) -List -ErrorAction SilentlyContinue
        if ($hit) { $problems += "交付包的文字檔裡出現帳號前綴：$($hit[0].Path)" }
    }
}

if ($problems) {
    Remove-Item -Recurse -Force $Bundle
    Write-Host "`n=== 檢查沒過，產物已刪除 ===" -ForegroundColor Red
    $problems | ForEach-Object { Write-Host "  x $_" -ForegroundColor Red }
    throw '交付包沒有通過檢查'
}

$size = [math]::Round((Get-ChildItem $Bundle -Recurse -File |
    Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "`n=== 完成 ===" -ForegroundColor Cyan
Write-Host "  $Bundle" -ForegroundColor Green
Write-Host "  $size MB —— 整個資料夾壓縮起來就是要交付的東西"
Write-Host ""
Write-Host "  ⚠️ 對方仍然要自己做：註冊群益元件、申請憑證、填 .env" -ForegroundColor Yellow
Write-Host "     打包只省掉「安裝 Python」那一步。" -ForegroundColor Yellow
