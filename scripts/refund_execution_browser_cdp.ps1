param(
    [string]$BaseUrl = 'http://127.0.0.1:5188',
    [string]$OutputDir = "$env:TEMP\market-refund-execution-browser"
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force $OutputDir | Out-Null
$profile = "$env:TEMP\market-refund-execution-chrome"
$chrome = Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList @('--headless=new','--remote-debugging-port=9237',"--user-data-dir=$profile",'about:blank') -PassThru
try {
    for ($i=0; $i -lt 60; $i++) {
        try { $null = Invoke-RestMethod 'http://127.0.0.1:9237/json/list'; break }
        catch { Start-Sleep -Milliseconds 500 }
    }
    $helper = Get-Content -Raw (Join-Path $PSScriptRoot 'message_center_browser_cdp.ps1')
    $start = $helper.IndexOf("`$ErrorActionPreference = 'Stop'")
    $end = $helper.IndexOf('New-Item -ItemType Directory -Force $OutputDir')
    Invoke-Expression ($helper.Substring($start,$end-$start).Replace('127.0.0.1:9222','127.0.0.1:9237'))
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false} | Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/tests/fixtures/refund-execution-browser.html"} | Out-Null
    WaitJs '!!document.querySelector(".refund-list tbody .task-code")'
    Js 'window.prompt=window.confirm=()=>{throw Error("Native dialog forbidden")};document.querySelector(".refund-list tbody .task-code").click();true' | Out-Null
    WaitJs '!!document.querySelector(".refund-detail .primary-btn")'
    Screenshot 'refund-execution-desktop.png'
    Js 'document.querySelector(".refund-detail .primary-btn").click();true' | Out-Null
    WaitJs '!!document.querySelector(".action-dialog textarea")'
    Js 'document.querySelector(".action-dialog").requestSubmit();true' | Out-Null
    WaitJs '!!document.querySelector(".action-dialog [role=alert]")'
    if (Js 'window.refundExecutionFixture.calls.length!==0') { throw 'Empty reason sent a mutation' }
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=2;mobile=$true} | Out-Null
    Start-Sleep -Milliseconds 400
    $bounds = Js '(()=>{const r=document.querySelector(".action-dialog").getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:innerWidth,height:innerHeight}})()'
    if ($bounds.left -lt 0 -or $bounds.top -lt 0 -or $bounds.right -gt $bounds.width+1 -or $bounds.bottom -gt $bounds.height+1) { throw 'Mobile execution dialog outside viewport' }
    Screenshot 'refund-execution-mobile-dialog.png'
    Js '(()=>{const t=document.querySelector(".action-dialog textarea");t.value="Owned browser simulated execution";t.dispatchEvent(new Event("input",{bubbles:true}));document.querySelector(".action-dialog").requestSubmit();return true})()' | Out-Null
    WaitJs 'window.refundExecutionFixture.calls.length===1 && !!document.querySelector(".refund-execution") && !document.querySelector(".action-dialog")'
    if (-not (Js 'window.refundExecutionFixture.calls[0].path==="/refunds/refund/execute" && window.refundExecutionFixture.calls[0].body.expected_version===4')) { throw 'Execution contract mismatch' }
    if (Js 'document.body.textContent.includes("PRIVATE_SECRET")') { throw 'Private receipt data rendered' }
    if (-not (Js 'document.querySelectorAll(".refund-execution-legs li").length===2')) { throw 'Funding legs not rendered' }
    Js 'document.querySelector(".refund-execution").scrollIntoView({block:"start"});true' | Out-Null
    Start-Sleep -Milliseconds 250
    Screenshot 'refund-execution-mobile-failure.png'
    Js '[...document.querySelectorAll(".refund-detail button")].find(b=>b.textContent.trim()==="\u91cd\u8bd5\u6267\u884c").click();true' | Out-Null
    WaitJs '!!document.querySelector(".action-dialog textarea")'
    Js '(()=>{const t=document.querySelector(".action-dialog textarea");t.value="Owned browser retry";t.dispatchEvent(new Event("input",{bubbles:true}));document.querySelector(".action-dialog").requestSubmit();return true})()' | Out-Null
    WaitJs 'window.refundExecutionFixture.calls.length===2 && !document.querySelector(".action-dialog")'
    if (-not (Js 'window.refundExecutionFixture.calls[1].path==="/refunds/refund/retry" && window.refundExecutionFixture.calls[1].body.expected_version===5')) { throw 'Retry contract mismatch' }
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false} | Out-Null
    Js 'document.querySelector(".refund-execution").scrollIntoView({block:"start"});true' | Out-Null
    Start-Sleep -Milliseconds 250
    Screenshot 'refund-execution-desktop-failure.png'
    Js 'window.refundExecutionFixture.enterprise();true' | Out-Null
    WaitJs '!!document.querySelector(".refund-list tbody .task-code") && !document.querySelector(".refund-detail")'
    Js 'document.querySelector(".refund-list tbody .task-code").click();true' | Out-Null
    WaitJs '!!document.querySelector(".refund-detail")'
    if (Js '[...document.querySelectorAll(".refund-detail button")].some(b=>["\u6267\u884c\u9000\u6b3e","\u91cd\u8bd5\u6267\u884c"].includes(b.textContent.trim()))') { throw 'Enterprise actor sees execution command' }
    @{status='passed';checks=@('styled dialog','empty reason blocked','mobile dialog bounds','execute version contract','retry version contract','safe funding legs','enterprise commands hidden');bounds=$bounds;fixture_mutations=2;platform_mutations=0;limits=@('real browser and Vue components; mocked API; no actual refund or resource call')} | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')
    Write-Output 'Refund execution isolated browser checks passed'
} finally {
    if ($socket) { $socket.Dispose() }
    Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*market-refund-execution-chrome*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
