param(
    [string]$BaseUrl = 'http://192.168.10.10:30080',
    [Parameter(Mandatory=$true)][string]$Password,
    [string]$OutputDir = "$env:TEMP\market-audit-e2e-results"
)
$shared = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'message_center_browser_cdp.ps1')
$start = $shared.IndexOf('$ErrorActionPreference')
$end = $shared.IndexOf('New-Item -ItemType Directory')
Invoke-Expression $shared.Substring($start, $end-$start)
New-Item -ItemType Directory -Force $OutputDir | Out-Null
try {
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false} | Out-Null
    Cdp 'Page.navigate' @{url=$BaseUrl} | Out-Null
    WaitJs 'document.readyState === "complete"'
    Js 'localStorage.removeItem("market_token");location.reload();true' | Out-Null
    WaitJs '!!document.querySelector("input[autocomplete=username]")'
    $p = $Password | ConvertTo-Json -Compress
    Js "(()=>{let u=document.querySelector('input[autocomplete=username]'),p=document.querySelector('input[autocomplete=current-password]');u.value='admin@market.local';u.dispatchEvent(new Event('input',{bubbles:true}));p.value=$p;p.dispatchEvent(new Event('input',{bubbles:true}));u.closest('form').requestSubmit();return true})()" | Out-Null
    WaitJs '!!document.querySelector(".sidebar")'
    Js 'Array.from(document.querySelectorAll(".nav-item")).find(n=>n.textContent.includes("\u5ba1\u8ba1\u65e5\u5fd7")).click();true' | Out-Null
    WaitJs '!!document.querySelector(".audit-panel .table-wrap[aria-busy=false]") && !!document.querySelector(".audit-table tbody tr")'
    if ((Js 'document.querySelectorAll(".audit-filter-grid input[type=datetime-local]").length') -ne 2) { throw 'Date filters missing' }
    Js '(()=>{let f=document.querySelectorAll(".audit-filter-grid input[type=datetime-local]");f[0].value="2000-01-01T00:00";f[1].value="2099-01-01T00:00";f.forEach(n=>n.dispatchEvent(new Event("input",{bubbles:true})));document.querySelector(".audit-panel form").requestSubmit();return true})()' | Out-Null
    WaitJs '!!document.querySelector(".audit-panel .table-wrap[aria-busy=false]")'
    Js 'window.scrollTo(0,0);true' | Out-Null
    Start-Sleep -Milliseconds 500
    Screenshot 'audit-filters-desktop.png'
    Js 'window.__originalCreateObjectURL=URL.createObjectURL;URL.createObjectURL=function(blob){window.__auditExport=blob;return window.__originalCreateObjectURL.call(URL,blob)};Array.from(document.querySelectorAll(".audit-panel button")).find(b=>b.textContent.includes("CSV")).click();true' | Out-Null
    WaitJs '!!window.__auditExport'
    $csvCheck = Js '(async()=>{let text=await window.__auditExport.text();return {valid:text.includes("order_no")&&text.includes("before")&&text.includes("after"),bytes:window.__auditExport.size}})()'
    if (!$csvCheck.valid) { throw 'CSV export missing expected columns' }
    Js 'document.querySelector(".audit-table tbody tr").click();true' | Out-Null
    WaitJs '!!document.querySelector(".audit-drawer") && !document.querySelector(".audit-drawer [role=status]")'
    WaitJs 'document.querySelector(".audit-drawer").textContent.includes("\u5173\u8054\u4e8b\u4ef6\u65f6\u95f4\u7ebf")'
    $errors = Js 'Array.from(document.querySelectorAll(".audit-drawer [role=alert],.audit-panel [role=alert]")).map(n=>n.textContent)'
    if ($errors.Count) { throw ($errors -join ';') }
    Screenshot 'audit-detail-desktop.png'
    Js 'document.querySelector(".audit-drawer .drawer-head button").click();true' | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=2;mobile=$true} | Out-Null
    Start-Sleep -Milliseconds 700
    Js 'window.scrollTo(0,0);true' | Out-Null
    Screenshot 'audit-mobile.png'
    $geometry = Js '({width:innerWidth,bodyWidth:document.documentElement.scrollWidth})'
    if ($geometry.bodyWidth -gt $geometry.width+1) { throw 'Audit page overflows mobile viewport' }
    @{status='passed';csv=$csvCheck;geometry=$geometry;checks=@('login','time filters','CSV export','detail timeline','desktop/mobile layout')} | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'audit-browser-results.json')
    Write-Output 'Audit browser checks passed'
} finally { $socket.Dispose() }
