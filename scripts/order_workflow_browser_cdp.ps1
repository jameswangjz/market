param(
    [string]$BaseUrl = 'http://192.168.10.10:30080',
    [string]$FixtureFile = "$env:TEMP\order-workflow-fixture.json",
    [string]$OutputDir = "$env:TEMP\market-order-workflow-browser"
)
$ErrorActionPreference = 'Stop'
$socket = $null
$chrome = $null
$providerTokenJson = $null
$profile = Join-Path $env:TEMP ('market-order-workflow-' + [Guid]::NewGuid().ToString('N'))
$script:commandId = 0
$script:cdpBlockedMutations = 0
$script:networkMutations = 0
$script:nativeDialogs = 0
$stage = 'fixture validation'
$result = [ordered]@{
    status = 'pending'; checks = @(); screenshots = @()
    limits = @('Read-only browser acceptance: no approvals, rejections or payments',
        'Buyer admin and platform manual confirmation browser samples not supplied; permissions have unit coverage',
        'FE007 remains open pending BE008 fulfillment',
        'Fixture creation and offline fixture cleanup are owned by the main worker')
}

function Send-Cdp([string]$method, $parameters) {
    $script:commandId++
    $payload = @{ id=$script:commandId; method=$method; params=$parameters } | ConvertTo-Json -Depth 30 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $timeout = [Threading.CancellationTokenSource]::new(30000)
    try {
        $null = $socket.SendAsync([ArraySegment[byte]]::new($bytes), [Net.WebSockets.WebSocketMessageType]::Text, $true, $timeout.Token).GetAwaiter().GetResult()
    } finally { $timeout.Dispose() }
    return $script:commandId
}

function Cdp([string]$method, $parameters) {
    try {
        $id = Send-Cdp $method $parameters
        $timeout = [Threading.CancellationTokenSource]::new(30000)
        try {
            while ($true) {
                $memory = [IO.MemoryStream]::new()
                try {
                    do {
                        $buffer = New-Object byte[] 65536
                        $part = $socket.ReceiveAsync([ArraySegment[byte]]::new($buffer), $timeout.Token).GetAwaiter().GetResult()
                        if ($part.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close) { throw 'CDP closed' }
                        $memory.Write($buffer, 0, $part.Count)
                    } while (-not $part.EndOfMessage)
                    $answer = [Text.Encoding]::UTF8.GetString($memory.ToArray()) | ConvertFrom-Json
                } finally { $memory.Dispose() }
                if ($answer.method -eq 'Fetch.requestPaused') {
                    if ($answer.params.request.method -notin @('GET', 'HEAD', 'OPTIONS')) {
                        $script:cdpBlockedMutations++
                        Send-Cdp 'Fetch.failRequest' @{requestId=$answer.params.requestId; errorReason='BlockedByClient'} | Out-Null
                    } else {
                        Send-Cdp 'Fetch.continueRequest' @{requestId=$answer.params.requestId} | Out-Null
                    }
                } elseif ($answer.method -eq 'Network.requestWillBeSent') {
                    if ($answer.params.request.method -notin @('GET', 'HEAD', 'OPTIONS')) { $script:networkMutations++ }
                } elseif ($answer.method -eq 'Page.javascriptDialogOpening') {
                    $script:nativeDialogs++
                    Send-Cdp 'Page.handleJavaScriptDialog' @{accept=$false} | Out-Null
                }
                if ($answer.id -eq $id) {
                    if ($answer.error) { throw 'CDP command failed' }
                    return $answer.result
                }
            }
        } finally { $timeout.Dispose() }
    } catch { throw 'CDP operation failed (details suppressed)' }
}

function Js([string]$expression) {
    try {
        $response = Cdp 'Runtime.evaluate' @{expression=$expression; awaitPromise=$true; returnByValue=$true}
        if ($response.exceptionDetails) { throw 'Evaluation failed' }
        return $response.result.value
    } catch { throw 'Browser evaluation failed (details suppressed)' }
}

function WaitJs([string]$expression) {
    for ($i=0; $i -lt 100; $i++) {
        if (Js $expression) { return }
        Start-Sleep -Milliseconds 300
    }
    throw 'Browser condition timed out (details suppressed)'
}

function Assert-Browser([string]$expression, [string]$message) {
    if (-not (Js $expression)) { throw $message }
}

function Save-Result {
    $result.cdpBlockedMutations = $script:cdpBlockedMutations
    $result.networkMutations = $script:networkMutations
    $result.nativeDialogs = $script:nativeDialogs
    $result | ConvertTo-Json -Depth 12 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')
}

function Screenshot([string]$name) {
    $shot = Cdp 'Page.captureScreenshot' @{format='png'; captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name), [Convert]::FromBase64String($shot.data))
    $result.screenshots += $name
}

function Modal-Bounds {
    return Js @'
(() => {
  const modal = document.querySelector('.provider-review-modal'), r = modal.getBoundingClientRect();
  const rect = n => {const b=n.getBoundingClientRect();return {left:b.left,top:b.top,right:b.right,bottom:b.bottom}};
  const controls = [...modal.querySelectorAll('input,textarea,button')].map(rect);
  return {viewportWidth:innerWidth,viewportHeight:innerHeight,modal:rect(modal),
    controls,visible:!!modal.offsetWidth&&!!modal.offsetHeight,
    controlsOutside:controls.some(b=>b.left<r.left-1||b.right>r.right+1||b.top<r.top-1||b.bottom>r.bottom+1),
    horizontalOverflow:modal.scrollWidth>modal.clientWidth+1};
})()
'@
}

function Assert-Bounds($bounds) {
    $r = $bounds.modal
    if (-not $bounds.visible -or $r.left -lt -1 -or $r.top -lt -1 -or
        $r.right -gt $bounds.viewportWidth+1 -or $r.bottom -gt $bounds.viewportHeight+1 -or
        $bounds.controlsOutside -or $bounds.horizontalOverflow) { throw 'Provider modal or controls exceed viewport bounds' }
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
    if ([string]::IsNullOrWhiteSpace($fixture.token) -or [string]::IsNullOrWhiteSpace($fixture.provider_token) -or
        $fixture.order_id -notmatch '^[A-Za-z0-9_-]{1,80}$' -or $fixture.product_id -notmatch '^[A-Za-z0-9_-]{1,80}$') {
        throw 'Fixture needs token, provider_token, product_id and order_id'
    }
    $result.orderId = [string]$fixture.order_id
    $result.productId = [string]$fixture.product_id
    $orderIdJson = [string]$fixture.order_id | ConvertTo-Json -Compress
    $providerTokenJson = [string]$fixture.provider_token | ConvertTo-Json -Compress
    $fixture = $null
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    $stage = 'Chrome CDP startup'
    $chrome = Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList @(
        '--headless=new', "--remote-debugging-port=$port", "--user-data-dir=`"$profile`"", '--no-first-run', 'about:blank') -PassThru
    $target = $null
    for ($i=0; $i -lt 60; $i++) {
        try {
            $targets = Invoke-RestMethod "http://127.0.0.1:$port/json/list"
            $target = $targets | Where-Object { $_.type -eq 'page' } | Select-Object -First 1
            if ($target) { break }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $target) { throw 'Chrome startup timed out' }
    $socket = [Net.WebSockets.ClientWebSocket]::new()
    $timeout = [Threading.CancellationTokenSource]::new(30000)
    try { $null = $socket.ConnectAsync([Uri]$target.webSocketDebuggerUrl, $timeout.Token).GetAwaiter().GetResult() }
    finally { $timeout.Dispose() }
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Network.enable' @{} | Out-Null
    # CDP intercepts all requests, including form navigation; credentials never enter process arguments.
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})} | Out-Null
    $instrumentation = @'
(() => {
  const id = __ORDER_ID__;
  const t = window.__orderWorkflowTest = {order:null,quote:null,quoteReads:0,detailReads:0,blockedMutations:0,nativeAttempts:0};
  const path = url => new URL(url,location.origin).pathname;
  const allowed = method => {
    if (['GET','HEAD','OPTIONS'].includes(String(method).toUpperCase())) return true;
    t.blockedMutations++; return false;
  };
  for (const name of ['prompt','confirm','alert']) window[name] = () => {t.nativeAttempts++;throw Error('Native dialog blocked')};
  const observe = (url,status,data) => {
    if (status!==200) return;
    const p=path(url);
    if (p==='/api/orders') {
      const o=data.items?.find(o=>o.id===id);
      if(o) t.order={id:o.id,order_no:o.order_no,main_status:o.main_status,payment_status:o.payment_status};
    }
    if (p==='/api/orders/'+id) t.detailReads++;
    if (p==='/api/orders/'+id+'/provider-quote') {
      t.quoteReads++;
      t.quote={order_id:data.order_id,amount:data.amount,cost:data.cost,expected_updated_at:data.expected_updated_at,
        main_status:data.main_status,payment_status:data.payment_status};
    }
  };
  const open=XMLHttpRequest.prototype.open, send=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(method,url,...args){this.workflowMethod=method;this.workflowUrl=url;return open.call(this,method,url,...args)};
  XMLHttpRequest.prototype.send=function(body){
    if(!allowed(this.workflowMethod)) throw Error('Mutation blocked');
    this.addEventListener('load',()=>{try{observe(this.workflowUrl,this.status,JSON.parse(this.responseText))}catch{}});
    return send.call(this,body);
  };
  const originalFetch=window.fetch;
  window.fetch=function(input,init){
    const req=new Request(input,init);
    if(!allowed(req.method)) return Promise.reject(Error('Mutation blocked'));
    return originalFetch.call(this,input,init).then(response=>{
      response.clone().json().then(data=>observe(req.url,response.status,data)).catch(()=>{});return response;
    });
  };
  navigator.sendBeacon=()=>{t.blockedMutations++;return false};
})();
'@
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$instrumentation.Replace('__ORDER_ID__', $orderIdJson)} | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false} | Out-Null
    $stage = 'provider session injection'
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"} | Out-Null
    WaitJs '!!document.querySelector(".login-shell")'
    Js "(()=>{localStorage.setItem('market_token',$providerTokenJson);return true})()" | Out-Null
    $providerTokenJson = $null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"} | Out-Null
    $stage = 'exact fixture order row'
    WaitJs '!!document.querySelector(".sidebar") && !!window.__orderWorkflowTest.order && [...document.querySelectorAll(".order-no")].filter(n=>n.textContent.trim()===window.__orderWorkflowTest.order.order_no).length===1'
    Assert-Browser 'window.__orderWorkflowTest.order.main_status==="pending_provider_review" && window.__orderWorkflowTest.order.payment_status==="unpaid"' 'Fixture must await provider review and remain unpaid'
    Js '(()=>{const n=[...document.querySelectorAll(".order-no")].find(n=>n.textContent.trim()===window.__orderWorkflowTest.order.order_no);n.closest("tr").querySelector("button[title=\"\u67e5\u770b\u8ba2\u5355\"]").click();return true})()' | Out-Null
    WaitJs '!!document.querySelector(".order-drawer") && document.querySelector(".order-drawer h2")?.textContent.trim()===window.__orderWorkflowTest.order.order_no && [...document.querySelectorAll(".order-drawer .action-list button")].some(b=>b.textContent.includes("\u63d0\u4f9b\u65b9\u5ba1\u6838"))'
    $result.checks += 'Exact fixture order fetched through list UI and opened through its row'
    $stage = 'provider review quote and styled modal'
    Js '[...document.querySelectorAll(".order-drawer .action-list button")].find(b=>b.textContent.includes("\u63d0\u4f9b\u65b9\u5ba1\u6838")).click();true' | Out-Null
    WaitJs '!!document.querySelector(".provider-review-modal") && !!window.__orderWorkflowTest.quote'
    Assert-Browser @'
(() => {
  const t=window.__orderWorkflowTest,m=document.querySelector('.provider-review-modal'),q=t.quote;
  const inputs=m.querySelectorAll('input[type=number]'),style=getComputedStyle(m),scrim=getComputedStyle(m.parentElement);
  const format=n=>Number(n).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
  return q.order_id===t.order.id && typeof q.amount==='string' && typeof q.cost==='string' &&
    Number(inputs[0].value)===Number(q.amount) && Number(inputs[1].value)===Number(q.cost) &&
    m.querySelector('p')?.textContent.trim()===t.order.order_no &&
    [...m.querySelectorAll('small')].every((n,i)=>n.textContent.includes(format(i===0?q.amount:q.cost))) &&
    typeof q.expected_updated_at==='string' && Number.isFinite(Date.parse(q.expected_updated_at)) &&
    q.main_status==='pending_provider_review' && q.payment_status==='unpaid' &&
    t.quoteReads===1 && t.detailReads===1 && m.getAttribute('role')==='dialog' && m.getAttribute('aria-modal')==='true' &&
    m.classList.contains('modal-card') && m.parentElement.classList.contains('modal-scrim') &&
    style.backgroundColor!=='rgba(0, 0, 0, 0)' && parseFloat(style.padding)>0 && scrim.position==='fixed' &&
    t.nativeAttempts===0;
})()
'@ 'Modal fields, expected timestamp or styled dialog differ from the real quote'
    $result.quote = Js 'window.__orderWorkflowTest.quote'
    $result.checks += 'Real string amount/cost and expected_updated_at captured; styled modal matches quote and existing row number'
    $result.desktop = Modal-Bounds
    Assert-Bounds $result.desktop
    Screenshot 'provider-review-desktop.png'
    $stage = 'invalid rejection without request'
    Js '(()=>{const m=document.querySelector(".provider-review-modal"),r=m.querySelector("textarea");r.value="";r.dispatchEvent(new Event("input",{bubbles:true}));[...m.querySelectorAll("button")].find(b=>b.textContent.trim()==="\u62d2\u7edd").click();return true})()' | Out-Null
    WaitJs '!!document.querySelector(".provider-review-modal") && document.querySelector(".toast")?.textContent.includes("\u8bf7\u586b\u5199\u62d2\u7edd\u539f\u56e0")'
    Assert-Browser 'window.__orderWorkflowTest.blockedMutations===0 && window.__orderWorkflowTest.nativeAttempts===0' 'Blank rejection attempted a request or native dialog'
    Screenshot 'provider-review-reject-required-desktop.png'
    $result.checks += 'Blank rejection keeps modal open and displays required reason with zero mutation attempts'
    $stage = 'invalid approval without request'
    Js '(()=>{const m=document.querySelector(".provider-review-modal"),i=m.querySelectorAll("input[type=number]");["0","1"].forEach((v,n)=>{i[n].value=v;i[n].dispatchEvent(new Event("input",{bubbles:true}))});m.querySelector("form").requestSubmit();return true})()' | Out-Null
    WaitJs '!!document.querySelector(".provider-review-modal") && document.querySelector(".toast")?.textContent.includes("\u8ba2\u5355\u91d1\u989d\u4e0d\u80fd\u4f4e\u4e8e\u6210\u672c")'
    Assert-Browser 'window.__orderWorkflowTest.blockedMutations===0' 'Invalid approval attempted a request'
    Js '(()=>{const m=document.querySelector(".provider-review-modal"),i=m.querySelectorAll("input[type=number]"),q=window.__orderWorkflowTest.quote;[q.amount,q.cost].forEach((v,n)=>{i[n].value=v;i[n].dispatchEvent(new Event("input",{bubbles:true}))});return true})()' | Out-Null
    $result.checks += 'Price below cost rejected locally with zero mutation attempts; original quote restored'
    $stage = '390px mobile modal bounds'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=2;mobile=$true} | Out-Null
    WaitJs 'innerWidth===390 && !!document.querySelector(".provider-review-modal")'
    Start-Sleep -Milliseconds 500
    $result.mobile = Modal-Bounds
    Assert-Bounds $result.mobile
    Screenshot 'provider-review-mobile-390.png'
    $result.checks += 'Desktop and 390px modal and all controls remain within viewport'
    $stage = 'cancel without mutation'
    Js 'document.querySelector(".provider-review-modal").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));true' | Out-Null
    WaitJs '!document.querySelector(".provider-review-modal")'
    $result.pageCounters = Js '(()=>{const t=window.__orderWorkflowTest;return {blockedMutations:t.blockedMutations,nativeAttempts:t.nativeAttempts,quoteReads:t.quoteReads,detailReads:t.detailReads}})()'
    if ($result.pageCounters.blockedMutations -ne 0 -or $result.pageCounters.nativeAttempts -ne 0 -or
        $script:cdpBlockedMutations -ne 0 -or $script:networkMutations -ne 0 -or $script:nativeDialogs -ne 0) {
        throw 'Unexpected mutation attempt or native dialog'
    }
    $result.checks += 'Escape cancels modal; zero page/CDP/network mutation attempts and zero native dialogs'
    $result.status = 'passed'
    Save-Result
    Write-Output 'Order workflow browser checks passed; redacted results and screenshots saved'
} catch {
    $result.status = 'failed'
    $result.failedStage = $stage
    if (Test-Path -LiteralPath $OutputDir) { Save-Result }
    # Never serialize exceptions, evaluated source, fixture contents, headers or localStorage.
    throw "Order workflow browser checks failed at: $stage (details suppressed; see browser-results.json)"
} finally {
    $fixture = $null
    $providerTokenJson = $null
    if ($socket) {
        try { Js 'localStorage.removeItem("market_token");true' | Out-Null } catch { }
        $socket.Dispose()
    }
    if ($chrome) {
        Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like "*$profile*" } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    if (Test-Path -LiteralPath $profile) { Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue }
}
