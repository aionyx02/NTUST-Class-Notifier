<#
.SYNOPSIS
    直接執行 NTUST-Class-Notifier，不需要編譯或安裝。

.DESCRIPTION
    腳本旁邊（或上層）找得到 pyproject.toml 時就跑本機的原始碼；找不到時
    會直接從 GitHub 上這個版本的程式碼執行，所以單獨下載這個 .ps1 也能用。

    自動加選預設關閉。加 -AutoEnroll 才會登入選課系統。

    帳密的取得順序：-StudentId／提示輸入 > 加密儲存的帳密 > .env；
    -UseEnvSwitch 例外，它只用加密檔或 .env，不會提示輸入（除非同時加了
    -SaveCredential）。密碼一律
    用 Read-Host -AsSecureString 輸入，不會出現在 PowerShell 的歷史紀錄
    （ConsoleHost_history.txt）或行程命令列裡。加 -SaveCredential 才會存
    檔，且用 Windows DPAPI 加密——只有你這個 Windows 帳號、在這台機器上解
    得開。要刪除請用 -ForgetCredential。

.PARAMETER Mode
    要跑哪一支：watch（人數監看，預設）、alert（搶課提醒）、notify（Discord）。

.PARAMETER AutoEnroll
    啟用自動加選。需要帳密，缺的部分會提示輸入。

.PARAMETER StudentId
    選課系統學號。不敏感，可以直接寫在指令上。

.PARAMETER SaveCredential
    把這次輸入的帳密加密存起來，之後就不用再輸入。

.PARAMETER ForgetCredential
    刪除已儲存的帳密後結束。

.PARAMETER UseEnvSwitch
    不要覆寫 AUTO_ENROLL，完全照 .env 的設定。

.PARAMETER DryRun
    只印出實際會執行的指令，不真的執行，也不會提示輸入密碼。

.PARAMETER Rest
    其餘參數原樣傳給程式，例如篩選規則或 -i / -s。開頭是「-」的參數請放在
    「--」後面，PowerShell 才不會搶著解讀。

.EXAMPLE
    .\ntust.ps1 課號:TCG175302
    # 監看單一課程，不會自動加選

.EXAMPLE
    .\ntust.ps1 -AutoEnroll -StudentId B11415024 -SaveCredential 課號:TCG175302
    # 第一次：提示輸入密碼並記住

.EXAMPLE
    .\ntust.ps1 -AutoEnroll 課號:TCG175302
    # 之後：直接跑，帳密從加密檔讀出來

.EXAMPLE
    .\ntust.ps1 課號:CS 學制:大學部 -- -i 10 --list
    # 「--」後面的參數原樣交給程式
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [ValidateSet('watch', 'alert', 'notify')]
    [string]$Mode = 'watch',

    [switch]$AutoEnroll,

    [string]$StudentId,

    [switch]$SaveCredential,

    [switch]$ForgetCredential,

    [switch]$UseEnvSwitch,

    [switch]$DryRun,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

$ErrorActionPreference = 'Stop'

# 發布時由 release workflow 換成該版本的 tag。
$Version = 'main'
$Repository = 'https://github.com/aionyx02/NTUST-Class-Notifier'
$CredentialPath = Join-Path $env:LOCALAPPDATA 'ntust-class-notifier\credential.xml'


function Test-DotEnvCredentials {
    <#
    .SYNOPSIS
        判斷這些目錄下的 .env 是否已經備齊帳密。
    .DESCRIPTION
        備齊的話就不要打擾使用者——Python 端本來就會讀 .env，而且環境變數
        優先，所以只在 .env 缺帳密時才提示輸入。這裡只看鍵有沒有值，不會
        讀出或顯示密碼內容。
    #>
    param([string[]]$Directories)

    foreach ($directory in $Directories) {
        if (-not $directory) { continue }
        $path = Join-Path $directory '.env'
        if (-not (Test-Path $path)) { continue }

        $lines = Get-Content $path
        $hasId = $lines -match '^\s*STUDENT_ID\s*=\s*\S'
        $hasPassword = $lines -match '^\s*PASSWORD\s*=\s*\S'
        if ($hasId -and $hasPassword) { return $true }
    }
    return $false
}


if ($ForgetCredential) {
    if (Test-Path $CredentialPath) {
        Remove-Item $CredentialPath -Force
        Write-Host "已刪除儲存的帳密：$CredentialPath"
    }
    else {
        Write-Host '沒有儲存過的帳密。'
    }
    exit 0
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host @'
找不到 uv，請先安裝：
    winget install --id=astral-sh.uv -e
或參考 https://docs.astral.sh/uv/getting-started/installation/
'@ -ForegroundColor Red
    exit 1
}

if ($UseEnvSwitch -and $AutoEnroll) {
    Write-Host '-AutoEnroll 與 -UseEnvSwitch 不能同時使用，請擇一。' `
        -ForegroundColor Red
    exit 1
}

# $env: 改的是整個行程的環境變數，腳本結束後還會留在呼叫者的 PowerShell
# 工作階段裡——密碼會被之後每一個子行程繼承，AUTO_ENROLL 也會蓋掉同一個
# 視窗後續的執行。先記下原值，最後一定還原。
$originalEnv = @{}
foreach ($name in 'AUTO_ENROLL', 'STUDENT_ID', 'PASSWORD') {
    $originalEnv[$name] = [Environment]::GetEnvironmentVariable($name)
}

$exitCode = 0
try {

# 自動加選預設關閉，免得只是想看人數卻不小心送出加選。
if (-not $UseEnvSwitch) {
    $env:AUTO_ENROLL = if ($AutoEnroll) { 'true' } else { 'false' }
}

# 從腳本位置往上找專案根目錄。
$root = $PSScriptRoot
while ($root -and -not (Test-Path (Join-Path $root 'pyproject.toml'))) {
    $parent = Split-Path $root -Parent
    $root = if ($parent -eq $root) { $null } else { $parent }
}

# 只有跟自動加選有關時才碰帳密。-UseEnvSwitch 也算：帳密可能只存在加密檔裡
# （.env 沒有），不載入的話 .env 就算開了 AUTO_ENROLL 也不會送出。
$needsCredentials = $AutoEnroll -or $SaveCredential -or $UseEnvSwitch
$credentialSource = '不需要'
if ($needsCredentials) {
    $credential = $null
    if (Test-Path $CredentialPath) {
        try {
            $credential = Import-Clixml $CredentialPath
        }
        catch {
            Write-Warning "讀不到儲存的帳密：$($_.Exception.Message)"
        }
    }

    # 指定了別的學號，存起來的那組密碼就不是這個帳號的了。
    if ($StudentId -and $credential -and $credential.UserName -ne $StudentId) {
        $credential = $null
    }

    if ($credential) {
        $credentialSource = "加密檔（$($credential.UserName)）"
    }
    elseif (-not $StudentId -and
            (Test-DotEnvCredentials -Directories @($PWD.Path, $root))) {
        $credentialSource = '.env'
    }
    elseif ($UseEnvSwitch -and -not $SaveCredential) {
        # 照 .env 決定時不主動問密碼：.env 可能根本沒打開自動加選，為了一個
        # 不會用到的功能跳出提示很擾人。但 -SaveCredential 是明講「我要存帳
        # 密」，那就不能靜靜地什麼都不做——所以這裡要把它排除掉。
        $credentialSource = '依 .env'
    }
    elseif ($DryRun) {
        $credentialSource = '會提示輸入'
    }
    else {
        $id = if ($StudentId) { $StudentId } else { Read-Host '請輸入學號' }
        $secret = Read-Host "請輸入 $id 的選課系統密碼" -AsSecureString
        $credential = [System.Management.Automation.PSCredential]::new(
            $id, $secret)
        $credentialSource = "本次輸入（$id）"
    }

    if ($credential) {
        # 交給子行程用環境變數，不放在命令列上——命令列別的行程看得到。
        $env:STUDENT_ID = $credential.UserName
        $env:PASSWORD = [System.Net.NetworkCredential]::new(
            '', $credential.Password).Password

        if ($SaveCredential) {
            $directory = Split-Path $CredentialPath -Parent
            if (-not (Test-Path $directory)) {
                New-Item -ItemType Directory -Path $directory | Out-Null
            }
            $credential | Export-Clixml $CredentialPath
            Write-Host "已加密儲存至 $CredentialPath（只有你這個 Windows" `
                '帳號在這台機器上解得開）' -ForegroundColor DarkGray
        }
    }
}

if ($root) {
    $uvArgs = @('run', '--project', $root, "ntust-$Mode")
    $source = "本機原始碼：$root"
}
else {
    # 單獨下載這個 .ps1 的情況：直接跑 GitHub 上的這一版，不用先 clone。
    $uvArgs = @('tool', 'run', '--from', "git+$Repository@$Version",
                "ntust-$Mode")
    $source = "GitHub $Repository@$Version"
}
$uvArgs += $Rest

Write-Host "來源：$source" -ForegroundColor DarkGray
# 沒開自動加選就整行不印（連「關閉」都不印）：這支腳本是拿來下載執行、也拿
# 來展示的，畫面上不需要出現一個沒有在運作的功能。
if ($UseEnvSwitch -or $AutoEnroll) {
    $enrollState = if ($UseEnvSwitch) { '依 .env' } else { '啟用' }
    Write-Host "自動加選：$enrollState・帳密：$credentialSource" `
        -ForegroundColor DarkGray
}
elseif ($SaveCredential) {
    # 這一次只是存帳密、不會加選，那就連「關閉」都不必說。
    Write-Host "帳密：$credentialSource" -ForegroundColor DarkGray
}

if ($DryRun) {
    Write-Host "uv $($uvArgs -join ' ')"
}
else {
    # uv 用離開碼回報錯誤（規則寫錯是 2），別讓 PowerShell 7.4+ 把它變成
    # 一片紅色的 NativeCommandExitException。
    $PSNativeCommandUseErrorActionPreference = $false
    & uv @uvArgs
    $exitCode = $LASTEXITCODE
}

}
finally {
    foreach ($entry in $originalEnv.GetEnumerator()) {
        if ($null -eq $entry.Value) {
            Remove-Item "Env:$($entry.Key)" -ErrorAction SilentlyContinue
        }
        else {
            Set-Item "Env:$($entry.Key)" $entry.Value
        }
    }
}

exit $exitCode
