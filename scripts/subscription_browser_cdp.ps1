param(
    [string]$BaseUrl = 'http://192.168.10.10:30080',
    [Parameter(Mandatory=$true)][string]$FixtureFile,
    [string]$OutputDir = "$env:TEMP\market-subscription-browser"
)
$ErrorActionPreference = 'Stop'
$socket = $null
$chrome = $null
$fixture = $null
$tokenJson = $null
$profile = Join-Path $env:TEMP ('market-subscription-' + [Guid]::NewGuid().ToString('N'))
$script:commandId = 0
$script:cdpBlockedMutations = 0
$script:networkMutations = 0
$script:nativeDialogs = 0
$stage = 'fixture validation'
$result = [ordered]@{
    status='pending'; mode='read-only'; checks=@(); screenshots=@(); samples=@(); failures=@()
    limits=@('Real dev API reads and isolated persisted buyer superadmin fixture',
        'Trusted delivery hook grants terms; no actual health or provisioning assertion',
        'All non-read requests blocked globally by CDP and page instrumentation',
        'Retry UI uses locally blocked requests, not server renewal responses or fake API success',
        'Fixture preparation and verified cleanup are separate Python CLI operations')
}

$helper = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'order_workflow_browser_cdp.ps1') -Raw
$start = $helper.IndexOf('function Send-Cdp(')
$end = $helper.IndexOf('function Save-Result')
if ($start -lt 0 -or $end -le $start) { throw 'Existing CDP helpers unavailable' }
Invoke-Expression ($helper.Substring($start, $end - $start))
$helper = $null

function Save-Result {
    $result.cdpBlockedMutations = $script:cdpBlockedMutations
    $result.networkMutationEvents = $script:networkMutations
    $result.nativeDialogs = $script:nativeDialogs
    $result | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')
}

function Screenshot([string]$name) {
    $shot = Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name), [Convert]::FromBase64String($shot.data))
    $result.screenshots += $name
}

function Diagnostic {
    return Js @'
(() => {
  const t=window.__subscriptionTest,p=document.querySelector('.subscription-terms-panel');
  return {width:innerWidth,secureContext:isSecureContext,randomUUIDType:typeof globalThis.crypto?.randomUUID,
    randomUUIDError:t?.randomUUIDError,session:t?.session,order:t?.order,detail:t?.detail,subscription:t?.subscription,
    reads:t?.reads,blockedMutationAttempts:t?.blocked.length,panelText:p?.textContent,
    renewalButton:!!p&&[...p.querySelectorAll('button')].some(b=>b.textContent.trim()==='\u7eed\u8d39')};
})()
'@
}

function Assert-Guard([int]$expected) {
    Assert-Browser "window.__subscriptionTest.blocked.length===$expected && window.__subscriptionTest.nativeAttempts===0" 'Unexpected mutation attempt or native dialog'
    if ($script:cdpBlockedMutations -ne 0 -or $script:networkMutations -ne 0 -or $script:nativeDialogs -ne 0) {
        throw 'Unexpected network mutation event or native dialog'
    }
}

function Bounds([string]$selector, [bool]$vertical) {
    $q = $selector | ConvertTo-Json -Compress
    $b = Js @"
(() => {
  const n=document.querySelector($q),r=n.getBoundingClientRect(),style=getComputedStyle(n);
  const controls=[...n.querySelectorAll('input,button')].filter(c=>c.offsetWidth&&c.offsetHeight).map(c=>c.getBoundingClientRect());
  return {width:innerWidth,height:innerHeight,left:r.left,right:r.right,top:r.top,bottom:r.bottom,
    visible:!!n.offsetWidth&&!!n.offsetHeight,overflow:n.scrollWidth>n.clientWidth+1,
    controlsOutsideX:controls.some(c=>c.left<r.left-1||c.right>r.right+1),
    controlsOutsideY:controls.some(c=>c.top<r.top-1||c.bottom>r.bottom+1),
    background:style.backgroundColor,padding:style.padding,borderRadius:style.borderRadius};
})()
"@
    if (-not $b.visible -or $b.left -lt -1 -or $b.right -gt $b.width+1 -or
        $b.overflow -or $b.controlsOutsideX -or ($vertical -and
        ($b.top -lt -1 -or $b.bottom -gt $b.height+1 -or $b.controlsOutsideY))) {
        throw 'Subscription panel or renewal controls exceed viewport'
    }
    return $b
}

function Check-Viewport([int]$width, [int]$height) {
    $script:stage = "$width viewport and exact owned order"
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=$height;deviceScaleFactor=1;mobile=($width -eq 390)} | Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"} | Out-Null
    WaitJs '!!window.__subscriptionTest.order && [...document.querySelectorAll(".order-no")].filter(n=>n.textContent.trim()===window.__subscriptionTest.order.order_no).length===1'
    Js '(()=>{const t=window.__subscriptionTest,n=[...document.querySelectorAll(".order-no")].find(n=>n.textContent.trim()===t.order.order_no);n.closest("tr").querySelector("button[title=\"\u67e5\u770b\u8ba2\u5355\"]").click();return true})()' | Out-Null
    WaitJs '!!window.__subscriptionTest.subscription && !!document.querySelector(".subscription-summary")'
    $script:stage = "$width real subscription terms"
    Assert-Browser "innerWidth===$width" 'Viewport width differs from requested device metrics'
    $result.api = Js '(()=>{const t=window.__subscriptionTest;return {session:t.session,order:t.order,detail:t.detail,subscription:t.subscription,reads:t.reads}})()'
    Assert-Browser @'
(() => {
  const t=window.__subscriptionTest,s=t.subscription,p=document.querySelector('.subscription-terms-panel');
  const rows=[...p.querySelectorAll('tbody tr')],labels=[...p.querySelectorAll('dt')].map(n=>n.textContent.trim());
  return t.detail?.id===t.id && t.detail.payment_status==='paid' &&
    t.detail.buyer_enterprise_id===t.expected.enterprise_id && s.id===t.expected.combination_id &&
    s.product_id===t.expected.product_id && s.enterprise_id===t.expected.enterprise_id &&
    s.current_period?.index===0 && s.version_code==='v1' && s.terms.length===2 && rows.length===2 &&
    [17,123,456].every((v,i)=>s.limits[['rate_limit_per_minute','daily_quota','monthly_quota'][i]]===v) &&
    rows.every((r,i)=>r.cells[0].textContent.trim()===s.terms[i].order_no &&
      r.cells[1].textContent.trim()===s.terms[i].version_code && Number(r.cells[4].textContent)===s.terms[i].months) &&
    s.terms.every(term=>t.expected.order_ids.includes(term.order_id)) &&
    s.terms.reduce((n,term)=>n+term.months,0)===5 &&
    rows[1].cells[5].textContent.trim()==='\u5f85\u751f\u6548' &&
    ['\u5f53\u524d\u7248\u672c','\u5230\u671f\u65f6\u95f4','\u8ba2\u9605\u72b6\u6001','\u5f53\u524d\u5468\u671f','\u6bcf\u5206\u949f\u9650\u6d41','\u6bcf\u65e5\u989d\u5ea6','\u6bcf\u6708\u989d\u5ea6'].every(v=>labels.includes(v)) &&
    document.querySelector('.order-drawer h2')?.textContent.trim()===t.expected.order_no;
})()
'@ 'Real subscription API identity, terms, labels or quota differ from fixture'
    Js 'document.querySelector(".subscription-terms-panel").scrollIntoView({block:"start"});true' | Out-Null
    Start-Sleep -Milliseconds 300
    $panel = Bounds '.subscription-terms-panel' $false
    $table = Js '(()=>{const n=document.querySelector(".subscription-table-wrap");return {width:n.clientWidth,scrollWidth:n.scrollWidth,overflow:getComputedStyle(n).overflowX}})()'
    if ($table.scrollWidth -gt $table.width -and $table.overflow -ne 'auto') { throw 'Terms table lacks controlled horizontal scrolling' }
    Screenshot "subscription-terms-$width.png"
    Assert-Guard 0
    $sample = [ordered]@{viewport=$width;panel=$panel;table=$table;api=$result.api;status='terms-passed'}
    $result.samples += $sample
    $script:stage = "$width subscription refresh and recovery"
    Js 'window.__subscriptionTest.failRead=true;document.querySelector("button[aria-label=\"\u5237\u65b0\u8ba2\u9605\u4fe1\u606f\"]").click();true' | Out-Null
    WaitJs 'document.querySelector(".subscription-terms-panel [role=alert]")?.textContent.includes("\u91cd\u8bd5")'
    Screenshot "subscription-read-error-$width.png"
    Js 'document.querySelector("button[aria-label=\"\u5237\u65b0\u8ba2\u9605\u4fe1\u606f\"]").click();true' | Out-Null
    WaitJs '!!document.querySelector(".subscription-summary") && window.__subscriptionTest.reads.subscription===2'
    $result.checks += "$width real list/detail/subscription, owned terms and locally failed read followed by real refresh"
    $script:stage = "$width renewal eligibility and labels"
    Screenshot "subscription-refresh-$width.png"
    Assert-Browser '[...document.querySelectorAll(".subscription-heading button")].some(b=>b.textContent.trim()==="\u7eed\u8d39"&&!b.disabled)' 'Renewal button unavailable for real owned buyer admin fixture'
    $script:stage = "$width renewal dialog opening"
    Js '[...document.querySelectorAll(".subscription-heading button")].find(b=>b.textContent.trim()==="\u7eed\u8d39").click();true' | Out-Null
    WaitJs '!!document.querySelector(".renewal-modal") || window.__subscriptionTest.randomUUIDError'
    Assert-Browser '!!document.querySelector(".renewal-modal")' 'Renewal dialog failed to open; see runtime diagnostic'
    $script:stage = "$width renewal labels and styling"
    Assert-Browser @'
(() => {
  const m=document.querySelector('.renewal-modal'),i=m.querySelector('input'),s=getComputedStyle(m);
  return m.getAttribute('role')==='dialog' && m.getAttribute('aria-modal')==='true' &&
    m.getAttribute('aria-labelledby')==='renewal-title' && document.querySelector('#renewal-title').textContent==='\u7eed\u8d39\u8ba2\u9605' &&
    i.type==='number' && i.min==='1' && i.max==='36' && i.step==='1' && i.required && i.value==='1' &&
    m.querySelector('label').textContent.trim()==='\u8ba2\u9605\u6708\u6570' &&
    m.querySelector('button[type=submit]').textContent.trim()==='\u521b\u5efa\u7eed\u8d39\u8ba2\u5355' &&
    getComputedStyle(m.parentElement).position==='fixed' && parseFloat(s.padding)>0 &&
    s.backgroundColor!=='rgba(0, 0, 0, 0)' && parseFloat(s.borderRadius)<=8;
})()
'@ 'Renewal labels, 1..36 constraints or styled modal differ from source'
    $modal = Bounds '.renewal-modal' $true
    Screenshot "subscription-renewal-$width.png"
    foreach ($value in @('0','37','1.5','')) {
        $v = $value | ConvertTo-Json -Compress
        Js "(()=>{const m=document.querySelector('.renewal-modal'),i=m.querySelector('input');i.value=$v;i.dispatchEvent(new Event('input',{bubbles:true}));m.querySelector('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return true})()" | Out-Null
        WaitJs 'document.querySelector(".renewal-modal [role=alert]")?.textContent.includes("1\u81f336")'
        Assert-Guard 0
    }
    foreach ($value in @('1','36')) {
        $v = $value | ConvertTo-Json -Compress
        Js "(()=>{const i=document.querySelector('.renewal-modal input');i.value=$v;i.dispatchEvent(new Event('input',{bubbles:true}));return true})()" | Out-Null
        Assert-Browser "document.querySelector('.renewal-modal input').checkValidity() && !document.querySelector('.renewal-modal [role=alert]')" 'Valid month boundary rejected locally'
    }
    Screenshot "subscription-months-36-$width.png"
    $script:stage = "$width blocked renewal and retry UI"
    Js 'document.querySelector(".renewal-modal form").requestSubmit();true' | Out-Null
    WaitJs 'document.querySelector(".renewal-modal button[type=submit]")?.textContent.trim()==="\u91cd\u8bd5\u521b\u5efa\u8ba2\u5355"'
    Assert-Guard 1
    Assert-Browser 'document.querySelector(".renewal-modal input").disabled && document.querySelector(".renewal-modal [role=alert]").textContent.includes("\u53ef\u91cd\u8bd5")' 'Blocked renewal lacks locked months and retry error'
    Js 'document.querySelector(".renewal-modal form").requestSubmit();true' | Out-Null
    WaitJs 'window.__subscriptionTest.blocked.length===2 && !document.querySelector(".renewal-modal button[type=submit]").disabled'
    Assert-Guard 2
    Assert-Browser '(()=>{const t=window.__subscriptionTest,[a,b]=t.blocked;return a.method==="POST"&&a.path==="/api/orders/"+t.id+"/renewal"&&b.path===a.path&&a.body===b.body&&JSON.parse(a.body).subscription_months===36&&!!JSON.parse(a.body).idempotency_key})()' 'Blocked retry changed endpoint, months or idempotency payload'
    Screenshot "subscription-blocked-retry-$width.png"
    Js 'document.querySelector(".renewal-modal").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));true' | Out-Null
    WaitJs '!document.querySelector(".renewal-modal")'
    $sample.status = 'passed'
    $sample.modal = $modal
    $sample.counters = Js '({reads:window.__subscriptionTest.reads,locallyBlockedRenewals:window.__subscriptionTest.blocked.length,locallyFailedReads:window.__subscriptionTest.failedReads})'
    $result.checks += "$width modal styling/bounds, month validation, blocked-request retry and Escape; no HTTP mutations sent"
}

try {
    New-Item -ItemType Directory -Force $OutputDir | Out-Null
    $site = [Uri]$BaseUrl
    if ($site.Scheme -notin @('http','https') -or $site.UserInfo -or $site.Query -or $site.Fragment -or $site.AbsolutePath -ne '/') { throw 'BaseUrl must be an HTTP(S) origin' }
    $BaseUrl = $site.GetLeftPart([UriPartial]::Authority)
    $fixture = Get-Content -LiteralPath $FixtureFile -Raw | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($fixture.token) -or $fixture.order_id -notmatch '^[A-Za-z0-9_-]{1,80}$' -or $fixture.order_ids.Count -ne 2) { throw 'Invalid isolated fixture' }
    $tokenJson = [string]$fixture.token | ConvertTo-Json -Compress
    $expected = $fixture | Select-Object order_id,order_no,order_ids,order_nos,product_id,enterprise_id,combination_id | ConvertTo-Json -Compress
    $result.orderId = $fixture.order_id
    $fixture = $null
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $listener.Start(); $port = $listener.LocalEndpoint.Port; $listener.Stop()
    $stage = 'Chrome CDP startup'
    $chrome = Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList @(
        '--headless=new',"--remote-debugging-port=$port","--user-data-dir=`"$profile`"",'--no-first-run','about:blank') -PassThru
    $target = $null
    for ($i=0;$i -lt 60;$i++) {
        try {
            $pages = Invoke-RestMethod "http://127.0.0.1:$port/json/list"
            $target = $pages | Where-Object {$_.type -eq 'page'} | Select-Object -First 1
            if ($target) { break }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $target) { throw 'Chrome startup timed out' }
    $stage = 'CDP connection'
    $socket = [Net.WebSockets.ClientWebSocket]::new()
    $timeout = [Threading.CancellationTokenSource]::new(30000)
    try { $null = $socket.ConnectAsync([Uri]$target.webSocketDebuggerUrl,$timeout.Token).GetAwaiter().GetResult() } finally { $timeout.Dispose() }
    $stage = 'CDP page setup'
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Network.enable' @{} | Out-Null
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})} | Out-Null
    $instrumentation = @'
(() => {
  const expected=__EXPECTED__,id=expected.order_id;
  const t=window.__subscriptionTest={expected,id,session:null,order:null,detail:null,subscription:null,
    reads:{list:0,detail:0,subscription:0},blocked:[],nativeAttempts:0,failRead:false,failedReads:0,randomUUIDError:false};
  const consoleError=console.error;
  console.error=function(...args){
    if(args.some(a=>a instanceof Error&&String(a.message).includes('randomUUID')))t.randomUUIDError=true;
    return consoleError.apply(this,args);
  };
  const path=url=>new URL(url,location.origin).pathname;
  const allowed=(method,url,body)=>{
    if(['GET','HEAD','OPTIONS'].includes(String(method).toUpperCase()))return true;
    t.blocked.push({method:String(method).toUpperCase(),path:path(url),body:typeof body==='string'?body:null});return false;
  };
  const failRead=url=>{if(t.failRead&&path(url)==='/api/orders/'+id+'/subscription'){t.failRead=false;t.failedReads++;return true}return false};
  const summary=o=>({id:o.id,order_no:o.order_no,payment_status:o.payment_status,buyer_enterprise_id:o.buyer_enterprise_id,
    main_status:o.main_status,delivery_status:o.delivery_status,product_version_code:o.product_version_code});
  const observe=(url,status,data)=>{
    if(status!==200)return;const p=path(url);
    if(p==='/api/auth/me')t.session={user:{id:data.user?.id,verified_status:data.user?.verified_status,
      activation_status:data.user?.activation_status,platform_role:data.user?.platform_role,
      is_active:data.user?.is_active,enterprise_memberships:data.user?.enterprise_memberships},
      enterprise:data.enterprise,role:data.role,membership:data.membership,membership_status:data.membership_status};
    if(p==='/api/orders'){t.reads.list++;const o=data.items?.find(o=>o.id===id);if(o)t.order=summary(o)}
    if(p==='/api/orders/'+id&&data.order?.id===id){t.reads.detail++;t.detail=summary(data.order)}
    if(p==='/api/orders/'+id+'/subscription'){t.reads.subscription++;t.subscription=data.item}
  };
  for(const name of ['alert','confirm','prompt'])window[name]=()=>{t.nativeAttempts++;throw Error('Native dialog blocked')};
  const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(method,url,...rest){this.testMethod=method;this.testUrl=url;return open.call(this,method,url,...rest)};
  XMLHttpRequest.prototype.send=function(body){
    if(!allowed(this.testMethod,this.testUrl,body))throw Error('Mutation blocked locally');
    if(failRead(this.testUrl))throw Error('Read failed locally for retry UI');
    this.addEventListener('load',()=>{try{observe(this.testUrl,this.status,JSON.parse(this.responseText))}catch{}});return send.call(this,body);
  };
  const fetch=window.fetch;
  window.fetch=function(input,init){const req=new Request(input,init);
    if(!allowed(req.method,req.url,init?.body))return Promise.reject(Error('Mutation blocked locally'));
    if(failRead(req.url))return Promise.reject(Error('Read failed locally for retry UI'));
    return fetch.call(this,input,init).then(r=>{r.clone().json().then(d=>observe(req.url,r.status,d)).catch(()=>{});return r});
  };
  navigator.sendBeacon=url=>{t.blocked.push({method:'POST',path:path(url),body:null});return false};
})();
'@
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$instrumentation.Replace('__EXPECTED__',$expected)} | Out-Null
    $stage = 'buyer session injection'
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"} | Out-Null
    WaitJs '!!document.querySelector(".login-shell")'
    Js "localStorage.setItem('market_token',$tokenJson);true" | Out-Null
    $tokenJson = $null
    foreach ($viewport in @(@{width=1440;height=1000},@{width=390;height=844})) {
        try { Check-Viewport $viewport.width $viewport.height }
        catch {
            $failure = [ordered]@{viewport=$viewport.width;stage=$stage;type=$_.Exception.GetType().FullName;reason=$_.Exception.Message}
            try { $failure.diagnostic = Diagnostic; Screenshot "subscription-failure-$($viewport.width).png" } catch { }
            $result.failures += $failure
            Save-Result
        }
    }
    if ($result.failures.Count -gt 0) { $stage = 'viewport acceptance (see failures)'; throw 'One or more viewports failed acceptance' }
    $result.status = 'passed'
    Save-Result
    Write-Output 'Subscription read-only browser checks passed; results and screenshots saved'
} catch {
    $result.status = 'failed'
    $result.failedStage = $stage
    $result.failureType = $_.Exception.GetType().FullName
    if ($stage -eq 'Chrome CDP startup') { $result.startupError = $_.Exception.Message }
    if ($socket) {
        try {
            $result.diagnostic = Diagnostic
            Screenshot 'subscription-failure.png'
        } catch { }
    }
    Save-Result
    throw "Subscription checks failed at: $stage (details suppressed; see browser-results.json)"
} finally {
    $fixture = $null
    $tokenJson = $null
    if ($socket) {
        try { Js 'localStorage.removeItem("market_token");true' | Out-Null } catch { }
        $socket.Dispose()
    }
    if ($chrome) {
        Get-CimInstance Win32_Process | Where-Object {$_.Name -eq 'chrome.exe' -and $_.CommandLine -like "*$profile*"} |
            ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}
    }
    if (Test-Path -LiteralPath $profile) {Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
}
