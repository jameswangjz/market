param(
    [string]$BaseUrl = 'http://127.0.0.1:5188',
    [string]$OutputDir = "$env:TEMP\market-refund-execution-state-browser",
    [int]$ChromePort = 9247
)
$ErrorActionPreference = 'Stop'
if ($ChromePort -eq 9237 -or $ChromePort -eq 9222) { throw 'Use an independent state-driver Chrome port' }
if (([Uri]$BaseUrl).Host -notin @('127.0.0.1', 'localhost', '[::1]')) { throw 'Only a local fixture server is supported' }
New-Item -ItemType Directory -Force $OutputDir | Out-Null
$profile = Join-Path $env:TEMP ('market-refund-execution-state-' + [Guid]::NewGuid().ToString('N'))
$socket = $null
$chrome = $null
try {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $ChromePort)
    try { $listener.Start() } finally { $listener.Stop() }
    $chrome = Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList @('--headless=new',"--remote-debugging-port=$ChromePort", "--user-data-dir=`"$profile`"",'about:blank') -PassThru
    $ready = $false
    for ($i=0; $i -lt 60; $i++) {
        try { $null = Invoke-RestMethod "http://127.0.0.1:$ChromePort/json/list"; $ready = $true; break }
        catch { Start-Sleep -Milliseconds 500 }
    }
    if (!$ready) { throw 'Isolated Chrome did not start' }
    $helper = Get-Content -Raw (Join-Path $PSScriptRoot 'message_center_browser_cdp.ps1')
    $start = $helper.IndexOf("`$ErrorActionPreference = 'Stop'")
    $end = $helper.IndexOf('New-Item -ItemType Directory -Force $OutputDir')
    if ($start -lt 0 -or $end -le $start) { throw 'CDP helper boundaries changed' }
    Invoke-Expression ($helper.Substring($start,$end-$start).Replace('127.0.0.1:9222',"127.0.0.1:$ChromePort"))
    function Check([string]$expression, [string]$message) { if (!(Js $expression)) { throw $message } }
    function OpenDetail {
        WaitJs '!!document.querySelector(".refund-list tbody .task-code")'
        Js 'document.querySelector(".refund-list tbody .task-code").click();true' | Out-Null
        WaitJs '!!document.querySelector(".refund-detail h3")'
    }
    function SubmitAction([string]$action) {
        $label = if ($action -eq 'execute') { '\u6267\u884c\u9000\u6b3e' } else { '\u91cd\u8bd5\u6267\u884c' }
        Js "[...document.querySelectorAll('.refund-detail button')].find(b=>b.textContent.trim()==='$label').click();true" | Out-Null
        WaitJs '!!document.querySelector(".action-dialog textarea")'
        Js '(()=>{const f=document.querySelector(".action-dialog"),t=f.querySelector("textarea");t.value="Owned state fixture confirmation";t.dispatchEvent(new Event("input",{bubbles:true}));f.requestSubmit();f.requestSubmit();return true})()' | Out-Null
        WaitJs '!document.querySelector(".action-dialog")'
    }
    function SafeProgress {
        Check '!/PRIVATE|SECRET_COST/.test(document.body.textContent)' 'Private failure or receipt rendered'
        Check 'document.querySelectorAll(".refund-execution-legs li").length===2' 'Funding legs missing'
    }
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false} | Out-Null
    Cdp 'Page.navigate' @{url="$($BaseUrl.TrimEnd('/'))/tests/fixtures/refund-execution-browser.html?scenario=states"} | Out-Null
    WaitJs '!!window.refundExecutionFixture'
    Check 'window.refundExecutionFixture.stateScenario' 'State scenario not selected'
    Js 'window.prompt=window.confirm=()=>{throw Error("Native dialog forbidden")};true' | Out-Null
    OpenDetail
    Check '!!document.querySelector(".refund-detail .primary-btn")' 'Approved execute command missing'
    SubmitAction 'execute'
    Check 'window.refundExecutionFixture.calls.length===1 && document.querySelector(".refund-detail .primary-btn").disabled' 'Duplicate execute was not blocked'
    Js 'window.refundExecutionFixture.release(true);true' | Out-Null
    WaitJs '!!document.querySelector(".refund-detail [role=alert]")'
    Check '!document.body.textContent.includes("PRIVATE")' 'Raw network failure rendered'
    SubmitAction 'execute'
    Check 'window.refundExecutionFixture.calls.length===2 && window.refundExecutionFixture.calls[0].body.idempotency_key===window.refundExecutionFixture.calls[1].body.idempotency_key' 'Network resubmission did not reuse key'
    Check 'window.refundExecutionFixture.calls[1].path==="/refunds/refund/execute" && window.refundExecutionFixture.calls[1].body.expected_version===4' 'Execute contract mismatch'
    Js 'window.refundExecutionFixture.release();true' | Out-Null
    WaitJs '!!document.querySelector(".refund-execution") && !document.querySelector(".refund-detail [role=status]")'
    Check 'document.querySelector(".refund-execution").textContent.includes("\u5904\u7406\u4e2d")' 'Executing progress missing'
    foreach ($step in @(@('resources_applied','\u8d44\u6e90\u5df2\u56de\u6536'), @('funds_pending','\u7b49\u5f85\u8d44\u91d1\u5904\u7406'), @('failed','\u8d44\u91d1\u5904\u7406\u5931\u8d25'))) {
        Js "window.refundExecutionFixture.advance('$($step[0])');document.querySelector('.refund-execution button').click();true" | Out-Null
        WaitJs "document.querySelector('.refund-execution')?.textContent.includes('$($step[1])')"
        SafeProgress
        Screenshot "refund-state-$($step[0]).png"
    }
    Js 'window.refundExecutionFixture.enterprise();true' | Out-Null
    OpenDetail
    SafeProgress
    Check '![...document.querySelectorAll(".refund-detail button")].some(b=>["\u6267\u884c\u9000\u6b3e","\u91cd\u8bd5\u6267\u884c"].includes(b.textContent.trim()))' 'Enterprise sees mutation commands'
    Check 'document.querySelector(".refund-execution").textContent.includes("\u8d44\u91d1\u5904\u7406\u5931\u8d25")' 'Enterprise cannot see progress'
    Js 'window.refundExecutionFixture.admin();true' | Out-Null
    OpenDetail
    SubmitAction 'retry'
    Check 'window.refundExecutionFixture.calls.length===3 && window.refundExecutionFixture.calls[2].path==="/refunds/refund/retry" && window.refundExecutionFixture.calls[2].body.expected_version===8 && window.refundExecutionFixture.calls[2].body.idempotency_key!==window.refundExecutionFixture.calls[1].body.idempotency_key' 'Retry contract or duplicate prevention failed'
    Js 'window.refundExecutionFixture.release();true' | Out-Null
    WaitJs '(()=>{const detail=document.querySelector(".refund-detail"),progress=detail?.querySelector(".refund-execution");if(!progress || detail.querySelector("[role=status]"))return false;const version=detail.querySelector("dl.state-grid > div:nth-child(4) dd")?.textContent.trim(),status=progress.querySelector("dl.state-grid > div:first-child dd")?.textContent.trim(),legs=[...progress.querySelectorAll(".refund-execution-legs li")];return version==="\u5df2\u9000\u6b3e / 9" && status==="\u5df2\u5b8c\u6210" && legs.length===2 && legs.every(leg=>leg.querySelectorAll("span")[1]?.textContent.trim()==="\u5df2\u5b8c\u6210")})()'
    SafeProgress
    Screenshot 'refund-state-completed-desktop.png'
    $completed = Js '(()=>{const p=document.querySelector(".refund-execution");return {errors:[...p.querySelectorAll(".error-text")].map(n=>n.textContent),unfinished:p.textContent.includes("\u6267\u884c\u672a\u5b8c\u6210"),text:p.textContent}})()'
    if ($completed.errors.Count -gt 0 -or $completed.unfinished) {
        throw "Completed execution or legs retain stale failure wording: $($completed | ConvertTo-Json -Depth 4 -Compress)"
    }
    Check '!document.querySelector(".refund-detail .primary-btn") && document.querySelector(".refund-execution").textContent.includes("\u4e0d\u4ee3\u8868\u94f6\u884c\u5230\u8d26")' 'Completion command or bank settlement wording unsafe'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=2;mobile=$true} | Out-Null
    Js 'document.querySelector(".refund-execution").scrollIntoView();true' | Out-Null
    Screenshot 'refund-state-completed-mobile.png'
    Check 'document.documentElement.scrollWidth<=innerWidth+1' 'Mobile page overflow'
    @{status='passed';states=@('approved','executing','resources_applied','funds_pending','failed','retry','completed');checks=@('duplicate submissions blocked','network key reused','execute/retry version contracts','enterprise progress visible, commands hidden','safe failure projection','simulated completion','mobile width');fixture_posts=3;platform_mutations=0;limits=@('Actual RefundCenter Vue module with explicitly mocked API; no live app mutations')} | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')
    Write-Output 'Refund execution state fixture checks passed'
} finally {
    if ($socket) { $socket.Dispose() }
    Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like "*$profile*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if ($chrome) { Stop-Process -Id $chrome.Id -Force -ErrorAction SilentlyContinue }
    if (Test-Path $profile) { Remove-Item -Recurse -Force $profile -ErrorAction SilentlyContinue }
}
