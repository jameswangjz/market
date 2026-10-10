param(
    [string]$BaseUrl = 'http://192.168.10.10:30080',
    [string]$Username = 'admin@market.local',
    [Parameter(Mandatory=$true)][string]$Password,
    [string]$OutputDir = "$env:TEMP\market-action-dialog-browser"
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force $OutputDir | Out-Null
$profile = "$env:TEMP\market-action-dialog-chrome"
$chrome = Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList @('--headless=new','--remote-debugging-port=9224',"--user-data-dir=$profile",'about:blank') -PassThru
try {
    for ($i=0; $i -lt 60; $i++) {
        try { $null = Invoke-RestMethod 'http://127.0.0.1:9224/json/list'; break }
        catch { Start-Sleep -Milliseconds 500 }
    }
    $helper = Get-Content -Raw (Join-Path $PSScriptRoot 'message_center_browser_cdp.ps1')
    $start = $helper.IndexOf("`$ErrorActionPreference = 'Stop'")
    $end = $helper.IndexOf('New-Item -ItemType Directory -Force $OutputDir')
    Invoke-Expression ($helper.Substring($start,$end-$start).Replace('127.0.0.1:9222','127.0.0.1:9224'))
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false} | Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=products"} | Out-Null
    WaitJs '!!document.querySelector(".login-shell") || !!document.querySelector(".sidebar")'
    $u = $Username | ConvertTo-Json -Compress
    $p = $Password | ConvertTo-Json -Compress
    Js "(async()=>{const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:$u,password:$p})});if(!r.ok)throw Error('Login '+r.status);const d=await r.json();localStorage.setItem('market_token',d.access_token);location.href='/console?view=products';return true})()" | Out-Null
    WaitJs '!!document.querySelector(".sidebar") && [...document.querySelectorAll("button")].some(b=>b.textContent.trim()==="\u5ba1\u6838")'
    Js '(()=>{window.prompt=window.confirm=()=>{throw Error("Native dialog used")};window.reviewRequests=0;const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.open=function(method,url,...args){this.testUrl=url;return open.call(this,method,url,...args)};XMLHttpRequest.prototype.send=function(...args){if(/\/products\/[^/]+\/(review|security-review)$/.test(this.testUrl)){window.reviewRequests++;throw Error("Review request blocked by read-only browser test")};return send.apply(this,args)};[...document.querySelectorAll("button")].find(b=>b.textContent.trim()==="\u5ba1\u6838").click();return true})()' | Out-Null
    WaitJs '!!document.querySelector(".review-action-bar")'
    Js 'document.querySelector(".review-action-bar .primary-btn").click();true' | Out-Null
    WaitJs '!!document.querySelector(".action-dialog textarea")'
    Screenshot 'review-approve-desktop.png'
    if (-not (Js 'document.querySelector(".action-dialog h2").textContent==="\u5ba1\u6838\u901a\u8fc7" && document.activeElement===document.querySelector(".action-dialog textarea")')) { throw 'Approval dialog title or focus incorrect' }
    Js 'document.querySelector(".action-dialog .secondary-btn").click();true' | Out-Null
    WaitJs '!document.querySelector(".action-dialog") && !document.querySelector(".review-action-bar .primary-btn").disabled'
    Js 'document.querySelector(".review-action-bar .secondary-btn").click();true' | Out-Null
    WaitJs '!!document.querySelector(".action-dialog textarea")'
    Js 'document.querySelector(".action-dialog").requestSubmit();true' | Out-Null
    WaitJs '!!document.querySelector(".action-dialog [role=alert]")'
    Screenshot 'review-reject-desktop.png'
    if (Js 'window.reviewRequests!==0') { throw 'Empty rejection unexpectedly sent review request' }
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=2;mobile=$true} | Out-Null
    Start-Sleep -Milliseconds 500
    $bounds = Js '(()=>{const r=document.querySelector(".action-dialog").getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:innerWidth,height:innerHeight}})()'
    if ($bounds.left -lt 0 -or $bounds.top -lt 0 -or $bounds.right -gt $bounds.width -or $bounds.bottom -gt $bounds.height) { throw 'Mobile dialog exceeds viewport' }
    Screenshot 'review-reject-mobile.png'
    Js 'document.querySelector(".action-dialog").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));true' | Out-Null
    WaitJs '!document.querySelector(".action-dialog") && !document.querySelector(".review-action-bar .primary-btn").disabled'
    if (Js 'window.reviewRequests!==0') { throw 'Cancellation sent review request' }
    @{status='passed';checks=@('styled approval/rejection','initial focus','empty rejection validation','cancel without request','Escape cancellation','390px mobile bounds');bounds=$bounds;reviewRequests=0;limits=@('read-only test; no actual product approval or rejection')} | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')
    Write-Output 'Action dialog browser checks passed'
} finally {
    if ($socket) { $socket.Dispose() }
    Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*market-action-dialog-chrome*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
