param(
    [string]$BaseUrl = 'http://192.168.10.10:30080',
    [string]$Username = 'admin@market.local',
    [string]$Password = '',
    [string]$OutputDir = "$env:TEMP\market-trading-phase1-browser"
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force $OutputDir | Out-Null
$profile = "$env:TEMP\market-trading-phase1-chrome"
$chrome = Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList @('--headless=new','--remote-debugging-port=9223',"--user-data-dir=$profile",'about:blank') -PassThru
try {
    for ($i=0; $i -lt 60; $i++) {
        try { $null = Invoke-RestMethod 'http://127.0.0.1:9223/json/list'; break }
        catch { Start-Sleep -Milliseconds 500 }
    }
    $helper = Get-Content -Raw (Join-Path $PSScriptRoot 'message_center_browser_cdp.ps1')
    $start = $helper.IndexOf("`$ErrorActionPreference = 'Stop'")
    $end = $helper.IndexOf('New-Item -ItemType Directory -Force $OutputDir')
    Invoke-Expression ($helper.Substring($start, $end-$start).Replace('127.0.0.1:9222','127.0.0.1:9223'))
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false} | Out-Null
    Cdp 'Page.navigate' @{url=$BaseUrl} | Out-Null
    WaitJs '!!document.querySelector(".store-shell") && !document.querySelector(".store-shell [role=status]")'
    $public = Js '(async()=>{const r=await fetch("/api/storefront/products");if(!r.ok)throw Error("public API "+r.status);return await r.json()})()'
    if (-not $public.total) { throw 'No public products available for browser verification' }
    WaitJs 'document.querySelectorAll(".store-card").length > 0 && !document.querySelector(".store-shell [role=alert]")'
    Screenshot 'storefront-desktop.png'
    Js 'localStorage.removeItem("market_token");location.reload();true' | Out-Null
    WaitJs 'document.querySelectorAll(".store-card").length > 0'
    $productId = [string]$public.items[0].id
    Cdp 'Page.navigate' @{url="$BaseUrl/products/$productId"} | Out-Null
    WaitJs '!!document.querySelector(".store-product-heading") && !!document.querySelector(".store-version-tool")'
    Screenshot 'storefront-product-desktop.png'
    Cdp 'Page.reload' @{} | Out-Null
    WaitJs '!!document.querySelector(".store-product-heading")'
    $detail = Js '({title:document.querySelector(".store-product-heading h1").textContent,bodyWidth:document.documentElement.scrollWidth,width:innerWidth})'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=2;mobile=$true} | Out-Null
    Start-Sleep -Milliseconds 500
    Screenshot 'storefront-product-mobile.png'
    $mobile = Js '({width:innerWidth,bodyWidth:document.documentElement.scrollWidth,logos:Array.from(document.querySelectorAll(".store-logo img")).filter(i=>i.complete&&i.naturalWidth>0).length})'
    if ($mobile.bodyWidth -gt $mobile.width+1) { throw 'Mobile detail overflows horizontally' }
    Cdp 'Page.navigate' @{url=$BaseUrl} | Out-Null
    WaitJs 'document.querySelectorAll(".store-card").length > 0'
    Screenshot 'storefront-mobile.png'
    $list = Js '({width:innerWidth,bodyWidth:document.documentElement.scrollWidth,logos:Array.from(document.querySelectorAll(".store-logo img")).filter(i=>i.complete&&i.naturalWidth>0).length})'
    if ($list.bodyWidth -gt $list.width+1) { throw 'Mobile listing overflows horizontally' }
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false} | Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console"} | Out-Null
    WaitJs '!!document.querySelector("input[autocomplete=username]")'
    Cdp 'Page.reload' @{} | Out-Null
    WaitJs '!!document.querySelector("input[autocomplete=username]")'
    Screenshot 'console-login-desktop.png'
    $checks = @('anonymous listing','anonymous detail','listing refresh','detail refresh','console refresh','desktop/mobile bounds')
    if ($Password) {
        Cdp 'Page.navigate' @{url="$BaseUrl/console?returnTo=%2Fproducts%2F$productId"} | Out-Null
        WaitJs '!!document.querySelector("input[autocomplete=username]")'
        $u = $Username | ConvertTo-Json -Compress
        $p = $Password | ConvertTo-Json -Compress
        Js "(()=>{let u=document.querySelector('input[autocomplete=username]'),p=document.querySelector('input[autocomplete=current-password]');u.value=$u;u.dispatchEvent(new Event('input',{bubbles:true}));p.value=$p;p.dispatchEvent(new Event('input',{bubbles:true}));u.closest('form').requestSubmit();return true})()" | Out-Null
        WaitJs '!!document.querySelector(".store-product-heading")'
        $checks += 'authenticated login return to product'
        Cdp 'Page.navigate' @{url="$BaseUrl/console?view=development"} | Out-Null
        WaitJs '!!document.querySelector(".sidebar") && !document.querySelector(".loading-line")'
        Screenshot 'console-development-desktop.png'
        Cdp 'Page.reload' @{} | Out-Null
        WaitJs '!!document.querySelector(".sidebar") && !document.querySelector(".loading-line")'
        if (Js 'document.body.innerText.includes("\u6570\u636e\u5237\u65b0\u5931\u8d25")') { throw 'Authenticated console refresh failed' }
        $checks += 'authenticated console F5'
    }
    @{status='passed';checks=$checks;detail=$detail;mobile=$mobile;list=$list;publicProducts=$public.total;limits=@('no paid checkout','no new business orders','multi-enterprise selection covered by HTTP/unit tests')} | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')
    Write-Output 'Storefront browser checks passed'
} finally {
    if ($socket) { $socket.Dispose() }
    Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*market-trading-phase1-chrome*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
