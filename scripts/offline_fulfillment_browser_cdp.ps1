param(
    [string]$BaseUrl = 'http://192.168.10.10:30080',
    [Parameter(Mandatory=$true)][string]$FixtureFile,
    [ValidateSet('All', 'Provider', 'Buyer')][string]$Stage = 'All',
    [string]$OutputDir = "$env:TEMP\market-offline-fulfillment-browser"
)
# Fixture JSON: order_id, provider_token, buyer_token; optional acceptance_order_id
# for All runs with separate awaiting_start and pending_acceptance fixtures.
$ErrorActionPreference = 'Stop'
$socket = $null
$chrome = $null
$tokenJson = $null
$fixture = $null
$profile = Join-Path $env:TEMP ('market-offline-fulfillment-' + [Guid]::NewGuid().ToString('N'))
$script:commandId = 0
$script:cdpBlockedMutations = 0
$script:networkMutations = 0
$script:nativeDialogs = 0
$currentCheck = 'fixture validation'
$result = [ordered]@{
    status = 'pending'; mode = 'read-only'; checks = @(); screenshots = @(); samples = @()
    limits = @('Only supplied isolated fixture orders and tokens are used',
        'All non-read HTTP requests are blocked by page instrumentation and CDP',
        'Evidence covers real fixture API reads, UI dialogs, local rejection validation and viewport fit',
        'Lifecycle transitions, attachment uploads/downloads and clean scan enforcement are not executed',
        'Main worker owns fixture preparation and cleanup')
}

# Reuse only the existing CDP helper functions, without executing its workflow.
$helper = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'order_workflow_browser_cdp.ps1') -Raw
$helperStart = $helper.IndexOf('function Send-Cdp(')
$helperEnd = $helper.IndexOf('function Save-Result')
if ($helperStart -lt 0 -or $helperEnd -le $helperStart) { throw 'CDP helper functions unavailable' }
Invoke-Expression ($helper.Substring($helperStart, $helperEnd - $helperStart))
$helper = $null

function Save-Result {
    $result.cdpBlockedMutations = $script:cdpBlockedMutations
    $result.networkMutations = $script:networkMutations
    $result.nativeDialogs = $script:nativeDialogs
    $result | ConvertTo-Json -Depth 15 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')
}

function Screenshot([string]$name) {
    $shot = Cdp 'Page.captureScreenshot' @{format='png'; captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name), [Convert]::FromBase64String($shot.data))
    $result.screenshots += $name
}

function Check-Bounds([string]$selector, [bool]$checkVertical) {
    $selectorJson = $selector | ConvertTo-Json -Compress
    $bounds = Js @"
(() => {
  const n=document.querySelector($selectorJson),r=n.getBoundingClientRect();
  const controls=[...n.querySelectorAll('input,textarea,button')].filter(c=>c.offsetWidth&&c.offsetHeight).map(c=>c.getBoundingClientRect());
  return {width:innerWidth,height:innerHeight,left:r.left,top:r.top,right:r.right,bottom:r.bottom,
    visible:!!n.offsetWidth&&!!n.offsetHeight,overflow:n.scrollWidth>n.clientWidth+1,
    controlsOutsideX:controls.some(c=>c.left<r.left-1||c.right>r.right+1),
    controlsOutsideY:controls.some(c=>c.top<r.top-1||c.bottom>r.bottom+1)};
})()
"@
    if (-not $bounds.visible -or $bounds.left -lt -1 -or $bounds.right -gt $bounds.width+1 -or
        $bounds.top -lt -1 -or $bounds.bottom -gt $bounds.height+1 -or $bounds.overflow -or
        $bounds.controlsOutsideX -or ($checkVertical -and $bounds.controlsOutsideY)) {
        throw 'Fixture drawer or dialog exceeds viewport bounds'
    }
    return $bounds
}

function Assert-NoMutations {
    Assert-Browser 'window.__offlineTest.blockedMutations===0 && window.__offlineTest.nativeAttempts===0' 'Unexpected page mutation attempt or native dialog'
    if ($script:cdpBlockedMutations -ne 0 -or $script:networkMutations -ne 0 -or $script:nativeDialogs -ne 0) {
        throw 'Unexpected CDP/network mutation attempt or native dialog'
    }
}

function Open-Fixture([string]$orderId, [string]$partyToken, [string]$expectedStatus) {
    $idJson = $orderId | ConvertTo-Json -Compress
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false} | Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"} | Out-Null
    WaitJs 'document.readyState==="complete" && (!!document.querySelector(".login-shell") || !!document.querySelector(".sidebar"))'
    $tokenJson = $partyToken | ConvertTo-Json -Compress
    $runJson = [Guid]::NewGuid().ToString('N') | ConvertTo-Json -Compress
    Js "(()=>{localStorage.setItem('market_token',$tokenJson);sessionStorage.setItem('__offline_fixture_id',$idJson);sessionStorage.setItem('__offline_run_id',$runJson);return true})()" | Out-Null
    $tokenJson = $null
    Assert-NoMutations
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"} | Out-Null
    WaitJs "window.__offlineTest?.runId===$runJson && !!document.querySelector('.sidebar') && !!window.__offlineTest.orders?.find(o=>o.id===$idJson)"
    Js "window.__offlineTest.order=window.__offlineTest.orders.find(o=>o.id===$idJson);true" | Out-Null
    $statusJson = $expectedStatus | ConvertTo-Json -Compress
    Assert-Browser "window.__offlineTest.order.main_status===$statusJson && window.__offlineTest.order.delivery_status===$statusJson && window.__offlineTest.order.payment_status==='paid' && window.__offlineTest.order.snapshot_version===1 && ['training','consulting','custom'].includes(window.__offlineTest.order.delivery_method)" 'Fixture is not a paid snapshot offline order in the required lifecycle stage'
    WaitJs '[...document.querySelectorAll(".order-no")].filter(n=>n.textContent.trim()===window.__offlineTest.order.order_no).length===1'
    Js '(()=>{const n=[...document.querySelectorAll(".order-no")].find(n=>n.textContent.trim()===window.__offlineTest.order.order_no);n.closest("tr").querySelector("button[title=\"\u67e5\u770b\u8ba2\u5355\"]").click();return true})()' | Out-Null
    WaitJs '!!document.querySelector(".order-drawer") && document.querySelector(".order-drawer h2")?.textContent.trim()===window.__offlineTest.order.order_no && window.__offlineTest.detailReads>0'
    WaitJs 'window.__offlineTest.attachmentReads>0 && [...document.querySelectorAll(".order-drawer .drawer-section")].some(n=>n.querySelector(".drawer-section-title")?.textContent.includes("\u4ea4\u4ed8\u9644\u4ef6") && !n.querySelector(".error-text") && !n.querySelector("[role=status]"))'
    Assert-Browser 'window.__offlineTest.detail.main_status===window.__offlineTest.order.main_status && window.__offlineTest.detail.delivery_status===window.__offlineTest.order.delivery_status' 'Fixture detail differs from list state'
    $result.checks += "$expectedStatus fixture opened through exact order row; real detail and attachment endpoints read"
}

function Check-Viewport([string]$party, [int]$width, [int]$height) {
    $mobile = $width -eq 390
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=$height;deviceScaleFactor=1;mobile=$mobile} | Out-Null
    WaitJs "innerWidth===$width && !!document.querySelector('.order-drawer')"
    Start-Sleep -Milliseconds 350
    $drawerBounds = Check-Bounds '.order-drawer' $false
    Js '[...document.querySelectorAll(".order-drawer .drawer-section")].find(n=>n.querySelector(".drawer-section-title")?.textContent.includes("\u4ea4\u4ed8\u9644\u4ef6")).scrollIntoView({block:"center"});true' | Out-Null
    Screenshot "$party-attachments-$width.png"
    if ($party -eq 'provider') {
        Assert-Browser '!!document.querySelector(".order-drawer input[type=file]") && !document.querySelector(".order-drawer input[type=file]").matches(":disabled")' 'Provider awaiting_start attachment upload control missing or disabled'
        Js '[...document.querySelectorAll(".order-drawer .action-list button")].find(b=>b.textContent.includes("\u5f00\u59cb\u5c65\u7ea6")).click();true' | Out-Null
        WaitJs '!!document.querySelector(".action-dialog")'
        Assert-Browser 'document.querySelector(".action-dialog h2").textContent==="\u5f00\u59cb\u5c65\u7ea6" && !document.querySelector(".action-dialog textarea") && document.querySelector(".action-dialog-description").textContent.includes(window.__offlineTest.order.order_no)' 'Provider start confirmation does not identify the exact fixture'
    } else {
        Assert-Browser '!document.querySelector(".order-drawer input[type=file]") && [...document.querySelectorAll(".order-drawer .action-list button")].some(b=>b.textContent.includes("\u9a8c\u6536\u901a\u8fc7")) && ![...document.querySelectorAll(".order-drawer .action-list button")].some(b=>b.textContent.includes("\u63d0\u4ea4\u4ea4\u4ed8\u7269"))' 'Buyer attachment or acceptance permissions are incorrect'
        Js '[...document.querySelectorAll(".order-drawer .action-list button")].find(b=>b.textContent.includes("\u62d2\u7edd\u5e76\u9000\u56de\u6574\u6539")).click();true' | Out-Null
        WaitJs '!!document.querySelector(".action-dialog textarea")'
        Assert-Browser 'document.querySelector(".action-dialog h2").textContent==="\u62d2\u7edd\u5e76\u9000\u56de\u6574\u6539" && !!document.querySelector(".action-dialog-required") && document.activeElement===document.querySelector(".action-dialog textarea")' 'Buyer rejection reason is not required or focused'
        Js 'document.querySelector(".action-dialog").requestSubmit();true' | Out-Null
        WaitJs '!!document.querySelector(".action-dialog [role=alert]") && document.querySelector(".action-dialog textarea").getAttribute("aria-invalid")==="true"'
        Js '(()=>{const n=document.querySelector(".action-dialog textarea");n.value="   ";n.dispatchEvent(new Event("input",{bubbles:true}));document.querySelector(".action-dialog").requestSubmit();return true})()' | Out-Null
        WaitJs '!!document.querySelector(".action-dialog [role=alert]")'
        Assert-NoMutations
    }
    Assert-Browser 'document.querySelector(".action-dialog").getAttribute("role")==="dialog" && document.querySelector(".action-dialog").getAttribute("aria-modal")==="true" && getComputedStyle(document.querySelector(".action-dialog-scrim")).position==="fixed"' 'Lifecycle dialog is not a styled modal'
    $dialogBounds = Check-Bounds '.action-dialog' $true
    Screenshot "$party-dialog-$width.png"
    Js 'document.querySelector(".action-dialog").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));true' | Out-Null
    WaitJs '!document.querySelector(".action-dialog") && [...document.querySelectorAll(".order-drawer .action-list button")].every(b=>!b.disabled)'
    Assert-NoMutations
    $result.samples += @{party=$party; viewport=$width; drawer=$drawerBounds; dialog=$dialogBounds;
        reads=(Js '({detail:window.__offlineTest.detailReads,attachments:window.__offlineTest.attachmentReads})')}
    $result.checks += "$party $width px attachment section, styled dialog, bounds and Escape cancellation; no mutations"
}

try {
    New-Item -ItemType Directory -Force $OutputDir | Out-Null
    $site = [Uri]$BaseUrl
    if ($site.Scheme -notin @('http', 'https') -or $site.UserInfo -or $site.Query -or $site.Fragment -or $site.AbsolutePath -ne '/') {
        throw 'BaseUrl must be an HTTP(S) origin'
    }
    $BaseUrl = $site.GetLeftPart([UriPartial]::Authority)
    try { $fixture = Get-Content -LiteralPath $FixtureFile -Raw | ConvertFrom-Json }
    catch { throw 'Cannot read fixture JSON (details suppressed)' }
    if ($fixture.order_id -notmatch '^[A-Za-z0-9_-]{1,80}$') { throw 'Fixture needs order_id' }
    if ($Stage -ne 'Buyer' -and [string]::IsNullOrWhiteSpace($fixture.provider_token)) { throw 'Fixture needs provider_token' }
    if ($Stage -ne 'Provider' -and [string]::IsNullOrWhiteSpace($fixture.buyer_token)) { throw 'Fixture needs buyer_token' }
    $buyerOrderId = if ($fixture.acceptance_order_id) { [string]$fixture.acceptance_order_id } else { [string]$fixture.order_id }
    if ($buyerOrderId -notmatch '^[A-Za-z0-9_-]{1,80}$') { throw 'Invalid acceptance_order_id' }
    $result.stage = $Stage
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start(); $port = $listener.LocalEndpoint.Port; $listener.Stop()
    $currentCheck = 'Chrome startup'
    $chrome = Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList @(
        '--headless=new', "--remote-debugging-port=$port", "--user-data-dir=`"$profile`"", '--no-first-run', 'about:blank') -PassThru
    $target = $null
    for ($i=0; $i -lt 60; $i++) {
        try {
            $pages = Invoke-RestMethod "http://127.0.0.1:$port/json/list"
            $target = $pages | Where-Object { $_.type -eq 'page' } | Select-Object -First 1
            if ($target) { break }
        }
        catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $target) { throw 'Chrome startup timed out' }
    $currentCheck = 'CDP connection'
    $socket = [Net.WebSockets.ClientWebSocket]::new()
    $timeout = [Threading.CancellationTokenSource]::new(30000)
    try { $null = $socket.ConnectAsync([Uri]$target.webSocketDebuggerUrl, $timeout.Token).GetAwaiter().GetResult() }
    finally { $timeout.Dispose() }
    $currentCheck = 'CDP page setup'
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Network.enable' @{} | Out-Null
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})} | Out-Null
    $instrumentation = @'
(() => {
  const t=window.__offlineTest={runId:sessionStorage.getItem('__offline_run_id'),fixtureId:sessionStorage.getItem('__offline_fixture_id'),orders:[],detail:null,detailReads:0,attachmentReads:0,blockedMutations:0,nativeAttempts:0};
  const allowed=method=>{if(['GET','HEAD','OPTIONS'].includes(String(method).toUpperCase()))return true;t.blockedMutations++;return false};
  for(const name of ['prompt','confirm','alert'])window[name]=()=>{t.nativeAttempts++;throw Error('Native dialog blocked')};
  const summary=o=>({id:o.id,order_no:o.order_no,main_status:o.main_status,delivery_status:o.delivery_status,payment_status:o.payment_status,snapshot_version:o.snapshot_version,delivery_method:o.delivery_method});
  const observe=(url,status,data)=>{
    if(status!==200)return;const p=new URL(url,location.origin).pathname;
    if(p==='/api/orders')t.orders=(data.items||[]).map(summary);
    if(/^\/api\/orders\/[^/]+$/.test(p)&&data.order&&data.order.id===t.fixtureId){t.detail=summary(data.order);t.detailReads++}
    if(/^\/api\/delivery-tasks\/[^/]+\/attachments$/.test(p))t.attachmentReads++;
  };
  const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(method,url,...args){this.testMethod=method;this.testUrl=url;return open.call(this,method,url,...args)};
  XMLHttpRequest.prototype.send=function(body){if(!allowed(this.testMethod))throw Error('Mutation blocked');this.addEventListener('load',()=>{try{observe(this.testUrl,this.status,JSON.parse(this.responseText))}catch{}});return send.call(this,body)};
  const originalFetch=window.fetch;
  window.fetch=function(input,init){const req=new Request(input,init);if(!allowed(req.method))return Promise.reject(Error('Mutation blocked'));return originalFetch.call(this,input,init).then(r=>{r.clone().json().then(d=>observe(req.url,r.status,d)).catch(()=>{});return r})};
  navigator.sendBeacon=()=>{t.blockedMutations++;return false};
})();
'@
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$instrumentation} | Out-Null
    if ($Stage -ne 'Buyer') {
        $currentCheck = 'provider awaiting_start fixture'
        Open-Fixture ([string]$fixture.order_id) ([string]$fixture.provider_token) 'awaiting_start'
        Check-Viewport 'provider' 1440 1000
        Check-Viewport 'provider' 390 844
    }
    if ($Stage -ne 'Provider') {
        $currentCheck = 'buyer pending_acceptance fixture'
        Open-Fixture $buyerOrderId ([string]$fixture.buyer_token) 'pending_acceptance'
        Check-Viewport 'buyer' 1440 1000
        Check-Viewport 'buyer' 390 844
        $result.checks += 'Blank and whitespace rejection reasons stay in dialog with validation errors and zero requests'
    }
    Assert-NoMutations
    $result.status = 'passed'
    Save-Result
    Write-Output 'Offline fulfillment read-only browser checks passed; results and screenshots saved'
} catch {
    $result.status = 'failed'
    $result.failedStage = $currentCheck
    if (Test-Path -LiteralPath $OutputDir) { Save-Result }
    throw "Offline fulfillment checks failed at: $currentCheck (details suppressed; see browser-results.json)"
} finally {
    $fixture = $null
    $tokenJson = $null
    if ($socket) {
        try { Js 'localStorage.removeItem("market_token");sessionStorage.removeItem("__offline_fixture_id");sessionStorage.removeItem("__offline_run_id");true' | Out-Null } catch { }
        $socket.Dispose()
    }
    if ($chrome) {
        Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like "*$profile*" } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    if (Test-Path -LiteralPath $profile) { Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue }
}
