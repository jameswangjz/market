param(
    [Parameter(Mandatory=$true)][string]$FixtureFile,
    [string]$OutputDir = "$env:TEMP\market-version-change-quote-browser",
    [string]$BaseUrl,
    [string]$ExpectedOrderNo,
    [string]$DeploymentTag
)
$ErrorActionPreference = 'Stop'
$socket = $null
$chrome = $null
$tokenJson = $null
$fixture = $null
$profile = Join-Path $env:TEMP ('market-vcq-' + [Guid]::NewGuid().ToString('N'))
$script:commandId = 0
$script:blockedRequests = 0
$script:allowedPosts = 0
$script:unexpectedNetworkRequests = 0
$script:nativeDialogs = 0
$script:ownedOrderId = $null
$script:targetIds = @()
$script:stage = 'fixture validation'
$result = [ordered]@{status='pending';passed=$false;mode='read-only quote';checks=@();screenshots=@();samples=@();failures=@();
    limits=@('GET only, except strict quote/validate POSTs for the exact fixture order; downloads denied',
        'Browser prices follow real server time and are compared to backend DTOs, not fixed 50-percent amounts',
        'Paid/granted subscriptions and verified gateway records are fixtures, not live gateway or payment verification',
        'deploymentTag records supplied provenance, not a server image assertion')}

function Resolve-InputPath([string]$value) {
    if ($value.StartsWith('/')) {
        try {
            $converted = & wsl.exe --exec wslpath -w $value 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $converted) { throw 'Path conversion failed' }
            return [string]($converted | Select-Object -Last 1)
        } catch { throw 'Cannot resolve WSL path (details suppressed)' }
    }
    return [IO.Path]::GetFullPath($value)
}

function Allowed-Request([string]$method,[string]$url,[string]$body) {
    try {
        $uri = [Uri]$url
        if ($uri.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $uri.UserInfo -or $uri.Fragment) { return $false }
        if ($uri.AbsolutePath -match '^/api/files/[^/]+/download/?$') { return $false }
        if ($method -ceq 'GET') { return $true }
        if ($method -cne 'POST' -or -not $script:ownedOrderId -or $uri.Query) { return $false }
        $path = '/api/orders/' + [Uri]::EscapeDataString($script:ownedOrderId) + '/version-change-quote'
        $data = $body | ConvertFrom-Json
        $keys = @($data.PSObject.Properties.Name)
        if ($uri.AbsolutePath -ceq $path) {
            return $keys.Count -eq 2 -and 'target_version_id' -in $keys -and 'compatibility_confirmed' -in $keys -and
                $data.target_version_id -is [string] -and $data.target_version_id -cin $script:targetIds -and
                $data.compatibility_confirmed -is [bool]
        }
        if ($uri.AbsolutePath -ceq ($path + '/validate')) {
            return $keys.Count -eq 1 -and $keys[0] -ceq 'quote_token' -and $data.quote_token -is [string] -and
                $data.quote_token.Length -gt 0 -and $data.quote_token.Length -le 8192
        }
    } catch { }
    return $false
}

function Send-Cdp([string]$method, $parameters) {
    $script:commandId++
    $payload = @{id=$script:commandId;method=$method;params=$parameters} | ConvertTo-Json -Depth 30 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $timeout = [Threading.CancellationTokenSource]::new(30000)
    try { $null = $socket.SendAsync([ArraySegment[byte]]::new($bytes),[Net.WebSockets.WebSocketMessageType]::Text,$true,$timeout.Token).GetAwaiter().GetResult() }
    finally { $timeout.Dispose() }
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
                        $part = $socket.ReceiveAsync([ArraySegment[byte]]::new($buffer),$timeout.Token).GetAwaiter().GetResult()
                        if ($part.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close) { throw 'CDP closed' }
                        $memory.Write($buffer,0,$part.Count)
                    } while (-not $part.EndOfMessage)
                    $answer = [Text.Encoding]::UTF8.GetString($memory.ToArray()) | ConvertFrom-Json
                } finally { $memory.Dispose() }
                if ($answer.method -eq 'Fetch.requestPaused') {
                    $request = $answer.params.request
                    if (-not (Allowed-Request $request.method $request.url $request.postData)) {
                        $script:blockedRequests++
                        Send-Cdp 'Fetch.failRequest' @{requestId=$answer.params.requestId;errorReason='BlockedByClient'} | Out-Null
                    } else {
                        if ($request.method -ceq 'POST') { $script:allowedPosts++ }
                        Send-Cdp 'Fetch.continueRequest' @{requestId=$answer.params.requestId} | Out-Null
                    }
                } elseif ($answer.method -eq 'Network.requestWillBeSent') {
                    $request = $answer.params.request
                    if (-not (Allowed-Request $request.method $request.url $request.postData)) { $script:unexpectedNetworkRequests++ }
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
        $response = Cdp 'Runtime.evaluate' @{expression=$expression;awaitPromise=$true;returnByValue=$true}
        if ($response.exceptionDetails) { throw 'Evaluation failed' }
        return $response.result.value
    } catch { throw 'Browser evaluation failed (details suppressed)' }
}

function WaitJs([string]$expression) {
    for ($i=0;$i -lt 100;$i++) {
        if (Js $expression) { return }
        Start-Sleep -Milliseconds 300
    }
    throw 'Browser condition timed out (details suppressed)'
}

function Assert-Browser([string]$expression,[string]$message) {
    if (-not (Js $expression)) { throw $message }
}

function Save-Result {
    if (-not $script:outputReady) { return }
    $result.blockedRequests = $script:blockedRequests
    $result.allowedQuoteValidatePosts = $script:allowedPosts
    $result.unexpectedNetworkRequests = $script:unexpectedNetworkRequests
    $result.nativeDialogs = $script:nativeDialogs
    $result | ConvertTo-Json -Depth 25 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')
}

function Guard {
    Assert-Browser 'window.__vcqTest.guard()' 'Unexpected request, native dialog, hidden field or quote token leak'
    if ($script:blockedRequests -or $script:unexpectedNetworkRequests -or $script:nativeDialogs) {
        throw 'Request allowlist or native dialog guard failed'
    }
}

function Screenshot([string]$name) {
    Guard
    $shot = Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
    $result.screenshots += $name
}

function Modal-Bounds([int]$width,[int]$height) {
    $bounds = Js @'
(() => {const m=document.querySelector('[aria-labelledby=version-change-title]');if(!m)return null;
const r=m.getBoundingClientRect(),s=getComputedStyle(m);
return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,visible:!!m.offsetWidth&&!!m.offsetHeight,
styled:s.backgroundColor!=='rgba(0, 0, 0, 0)'&&parseFloat(s.paddingLeft)>0,
overflow:m.scrollWidth>m.clientWidth+1,tablesOverflow:[...m.querySelectorAll('table')].some(n=>n.scrollWidth>n.clientWidth+1)};})()
'@
    if (-not $bounds -or -not $bounds.visible -or -not $bounds.styled -or $bounds.left -lt -1 -or $bounds.top -lt -1 -or
        $bounds.right -gt $width+1 -or $bounds.bottom -gt $height+1 -or $bounds.overflow -or $bounds.tablesOverflow) {
        throw 'Version change modal or amount tables exceed viewport width/bounds'
    }
    return $bounds
}

function Check-Viewport([string]$name,[int]$width,[int]$height) {
    $script:stage = "$name buyer order and live subscription"
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=$height;deviceScaleFactor=1;mobile=($name -eq 'mobile')} | Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"} | Out-Null
    WaitJs '!!document.querySelector(".sidebar") && !!window.__vcqTest.order && !!window.__vcqTest.session && !!window.__vcqTest.enterprises'
    Assert-Browser @'
(() => {const t=window.__vcqTest,o=t.order,u=t.session,e=t.enterprises.find(x=>x.id===o.buyer_enterprise_id);
const member=u.memberships.find(x=>x.enterprise_id===o.buyer_enterprise_id&&(x.status||x.membership_status)==='active');
return o.order_no===t.expectedOrderNo&&o.snapshot_version===1&&['api','model_api','tenant_access'].includes(o.delivery_method)&&
o.payment_status==='paid'&&o.main_status==='completed'&&o.delivery_status==='accepted'&&
!u.platformRole&&u.verifiedStatus==='verified'&&u.active!==false&&(!u.activationStatus||u.activationStatus==='active')&&
e?.verification_status==='verified'&&['super_admin','enterprise_admin'].includes(member?.role);})()
'@ 'Fixture must be a paid, completed monthly subscription owned by a verified buyer administrator'
    $script:ownedOrderId = [string](Js 'window.__vcqTest.order.id')
    Js @'
(() => {const n=[...document.querySelectorAll('.order-no')].filter(n=>n.textContent.trim()===window.__vcqTest.expectedOrderNo);
if(n.length!==1)throw Error('Exact fixture row missing');const b=n[0].closest('tr').querySelector('button[title="\u67e5\u770b\u8ba2\u5355"]');
if(!b)throw Error('Order detail button missing');b.scrollIntoView({block:'center',inline:'center'});b.click();return true;})()
'@ | Out-Null
    WaitJs '!!document.querySelector(".order-drawer") && !!window.__vcqTest.subscription && [...document.querySelectorAll(".subscription-heading button")].some(b=>b.textContent.trim()==="\u7248\u672c\u53d8\u66f4"&&!b.disabled)'
    Assert-Browser 'window.__vcqTest.subscription.available===true && window.__vcqTest.subscription.can_renew===true' 'Subscription is unavailable at the live server clock'
    Js '(()=>{const b=[...document.querySelectorAll(".subscription-heading button")].find(b=>b.textContent.trim()==="\u7248\u672c\u53d8\u66f4");b.click();return true})()' | Out-Null
    WaitJs '!!document.querySelector("[aria-labelledby=version-change-title] select") && !!window.__vcqTest.options && !document.querySelector("[aria-labelledby=version-change-title] [role=status]")'
    Assert-Browser @'
(() => {const t=window.__vcqTest,m=document.querySelector('[aria-labelledby=version-change-title]');
const options=[...m.querySelectorAll('select option')].filter(n=>n.value);
return m.getAttribute('role')==='dialog'&&m.getAttribute('aria-modal')==='true'&&m.textContent.includes(t.expectedOrderNo)&&
t.options.order_id===t.order.id&&t.options.versions.length===options.length&&!m.querySelector('[role=alert]')&&
t.options.versions.every(v=>options.some(o=>o.value===v.id&&o.textContent.includes(v.version_code)))&&
['up','down','cent'].every(code=>t.options.versions.some(v=>v.version_code===code));})()
'@ 'Real version options, exact order or expected fixture target versions differ from UI'
    $script:targetIds = @(Js 'window.__vcqTest.options.versions.map(v=>v.id)')
    $bounds = Modal-Bounds $width $height
    Screenshot "version-change-options-$name.png"
    foreach ($code in @('up','down','cent')) {
        $script:stage = "$name $code quote and compatibility"
        $codeJson = $code | ConvertTo-Json -Compress
        Js "window.__vcqTest.chooseTarget($codeJson);true" | Out-Null
        WaitJs '(()=>{const m=document.querySelector("[aria-labelledby=version-change-title]");return !!m.querySelector(".version-change-notes")&&!m.querySelector(".version-change-summary")})()'
        if (Js 'window.__vcqTest.selectedTarget().compatibility_mode==="breaking"') {
            Assert-Browser 'document.querySelector("[aria-labelledby=version-change-title] button[type=submit]").disabled' 'Breaking target must require compatibility confirmation'
            Js 'document.querySelector("[aria-labelledby=version-change-title] input[type=checkbox]").click();true' | Out-Null
        }
        WaitJs '!document.querySelector("[aria-labelledby=version-change-title] button[type=submit]").disabled'
        Js 'window.__vcqTest.quote=null;window.__vcqTest.validation=null;document.querySelector("[aria-labelledby=version-change-title] button[type=submit]").click();true' | Out-Null
        WaitJs '!!window.__vcqTest.quote && !!document.querySelector("[aria-labelledby=version-change-title] .version-change-summary") && !document.querySelector("[aria-labelledby=version-change-title] [role=alert]")'
        Assert-Browser 'window.__vcqTest.matchesUI()' 'Total, per-segment amount tables, ratios or currency differ from backend quote'
        Assert-Browser "window.__vcqTest.quote.action===('$code'==='down'?'downgrade':'upgrade') && window.__vcqTest.quote.execution_ready===false" 'Unexpected action or executable quote'
        Assert-Browser @'
(() => {const q=window.__vcqTest.quote;return q.segments.length===2&&q.segments[0].order_no===window.__vcqTest.expectedOrderNo&&
q.segments[0].subscription_months===3&&q.segments[1].subscription_months===1&&Number(q.segments[0].remaining_ratio)>0&&
Number(q.segments[0].remaining_ratio)<1&&Number(q.segments[1].remaining_ratio)===1;})()
'@ 'Quote must include the live current term and untouched future paid term'
        $bounds = Modal-Bounds $width $height
        Js 'document.querySelector("[aria-labelledby=version-change-title]").scrollTop=0;true' | Out-Null
        Screenshot "version-change-$code-$name.png"
        Js '(()=>{const m=document.querySelector("[aria-labelledby=version-change-title]");m.scrollTop=m.scrollHeight;return true})()' | Out-Null
        Screenshot "version-change-$code-future-$name.png"
        $script:stage = "$name $code validate quote"
        Js @'
(() => {const m=document.querySelector('[aria-labelledby=version-change-title]');
const b=[...m.querySelectorAll('.modal-actions button')].find(b=>b.textContent.trim()==='\u6821\u9a8c\u62a5\u4ef7');
if(!b||b.disabled)throw Error('Validate control unavailable');b.click();return true;})()
'@ | Out-Null
        WaitJs '!!window.__vcqTest.validation && document.querySelector("[aria-labelledby=version-change-title] .status-pill")?.textContent.trim()==="\u5df2\u6821\u9a8c"'
        Assert-Browser 'window.__vcqTest.validationMatches() && window.__vcqTest.matchesUI()' 'Validation changed the frozen backend quote or UI amounts'
        Js 'document.querySelector("[aria-labelledby=version-change-title]").scrollTop=0;true' | Out-Null
        Screenshot "version-change-$code-validated-$name.png"
        Guard
        $sample = Js 'window.__vcqTest.sample()'
        $result.samples += @{viewport=$name;target=$code;width=$width;height=$height;status='passed';modal=$bounds;backend=$sample}
    }
    $result.checks += "$name exact buyer order; live subscription; real options/quote/validate; compatibility gate; backend/UI amounts and future paid segment; modal bounds"
    Js 'document.querySelector("[aria-labelledby=version-change-title] .drawer-head button").click();true' | Out-Null
    WaitJs '!document.querySelector("[aria-labelledby=version-change-title]")'
    Guard
}

try {
    $OutputDir = Resolve-InputPath $OutputDir
    New-Item -ItemType Directory -Force $OutputDir | Out-Null
    $script:outputReady = $true
    $FixtureFile = Resolve-InputPath $FixtureFile
    try { $fixture = Get-Content -LiteralPath $FixtureFile -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw 'Cannot read fixture JSON (details suppressed)' }
    if (-not $BaseUrl) { $BaseUrl = [string]$fixture.baseUrl }
    if (-not $ExpectedOrderNo) { $ExpectedOrderNo = [string]$fixture.expectedOrderNo }
    if (-not $DeploymentTag) { $DeploymentTag = [string]$fixture.deploymentTag }
    $secret = [string]$fixture.buyerAdminToken
    if ([string]::IsNullOrWhiteSpace($secret) -or [string]::IsNullOrWhiteSpace($ExpectedOrderNo) -or
        [string]::IsNullOrWhiteSpace($DeploymentTag) -or [string]::IsNullOrWhiteSpace($BaseUrl)) {
        throw 'Fixture requires buyerAdminToken, expectedOrderNo, deploymentTag and baseUrl'
    }
    $site = [Uri]$BaseUrl
    if ($site.Scheme -notin @('http','https') -or $site.UserInfo -or $site.Query -or $site.Fragment -or $site.AbsolutePath -ne '/') {
        throw 'BaseUrl must be an HTTP(S) origin'
    }
    $BaseUrl = $site.GetLeftPart([UriPartial]::Authority)
    $tokenJson = $secret | ConvertTo-Json -Compress
    $secret = $null; $fixture = $null
    $result.baseUrl = $BaseUrl
    $result.expectedOrderNo = $ExpectedOrderNo
    $result.deploymentTag = $DeploymentTag
    $orderNoJson = $ExpectedOrderNo | ConvertTo-Json -Compress
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $listener.Start(); $port = $listener.LocalEndpoint.Port; $listener.Stop()
    $script:stage = 'Chrome startup'
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
    $script:stage = 'CDP connection'
    $socket = [Net.WebSockets.ClientWebSocket]::new()
    $timeout = [Threading.CancellationTokenSource]::new(30000)
    try { $null = $socket.ConnectAsync([Uri]$target.webSocketDebuggerUrl,$timeout.Token).GetAwaiter().GetResult() }
    finally { $timeout.Dispose() }
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Network.enable' @{} | Out-Null
    Cdp 'Network.setCacheDisabled' @{cacheDisabled=$true} | Out-Null
    Cdp 'Network.setBypassServiceWorker' @{bypass=$true} | Out-Null
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})} | Out-Null
    Cdp 'Browser.setDownloadBehavior' @{behavior='deny'} | Out-Null
    $instrumentation = @'
(() => {
const t=window.__vcqTest={expectedOrderNo:__ORDER_NO__,order:null,session:null,enterprises:null,subscription:null,
options:null,quote:null,validation:null,blocked:0,nativeAttempts:0,hiddenFields:0,quotePosts:0,validatePosts:0};
const privateTokens=new Set();let selectedId='';
const modal=()=>document.querySelector('[aria-labelledby=version-change-title]');
const hidden=value=>{if(!value||typeof value!=='object')return false;
return Object.entries(value).some(([key,child])=>/cost|profit|secret|upstream|settlement_rule/i.test(key)||hidden(child));};
const clean=data=>{const {quote_token,quote_id,...publicData}=data;return publicData;};
const allowed=(method,url,body)=>{try{const u=new URL(url,location.origin),verb=String(method).toUpperCase();
if(u.origin!==location.origin||u.hash||/^\/api\/files\/[^/]+\/download\/?$/.test(u.pathname))return false;
if(verb==='GET')return true;if(verb!=='POST'||!t.order||u.search)return false;
const prefix='/api/orders/'+encodeURIComponent(t.order.id)+'/version-change-quote',data=JSON.parse(body),keys=Object.keys(data);
if(u.pathname===prefix)return keys.length===2&&keys.includes('target_version_id')&&keys.includes('compatibility_confirmed')&&
typeof data.target_version_id==='string'&&t.options?.versions.some(v=>v.id===data.target_version_id)&&typeof data.compatibility_confirmed==='boolean';
if(u.pathname===prefix+'/validate')return keys.length===1&&keys[0]==='quote_token'&&privateTokens.has(data.quote_token);
}catch{}return false;};
const observe=(url,status,data,authenticated)=>{const p=new URL(url,location.origin).pathname;
if(p==='/api/auth/me'&&status===200)t.session={platformRole:data.user?.platform_role,verifiedStatus:data.user?.verified_status,
active:data.user?.is_active,activationStatus:data.user?.activation_status,memberships:data.user?.enterprise_memberships||[]};
if(p==='/api/storefront/buyer-enterprises'&&status===200)t.enterprises=data.items;
if(p==='/api/orders'&&status===200){const matches=data.items?.filter(o=>o.order_no===t.expectedOrderNo)||[];if(matches.length===1)t.order=matches[0];}
if(!t.order)return;const prefix='/api/orders/'+encodeURIComponent(t.order.id);
if(p===prefix+'/subscription'&&status===200)t.subscription=data.item;
if(p===prefix+'/version-change-options'&&status===200){if(!authenticated||hidden(data))t.hiddenFields++;t.options=data;}
if(p===prefix+'/version-change-quote'||p===prefix+'/version-change-quote/validate'){
if(status!==200||!authenticated){t.hiddenFields++;return;}if(hidden(data))t.hiddenFields++;
try{const base64=data.quote_token.split('.')[1].replace(/-/g,'+').replace(/_/g,'/');
const claims=JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(base64.padEnd(Math.ceil(base64.length/4)*4,'=')),c=>c.charCodeAt(0))));
if(hidden(claims)||claims.order_id!==t.order.id)t.hiddenFields++;privateTokens.add(data.quote_token);}catch{t.hiddenFields++;}
if(p.endsWith('/validate')){t.validation=clean(data);t.validatePosts++;}else{t.quote=clean(data);t.quotePosts++;}}};
const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send,header=XMLHttpRequest.prototype.setRequestHeader;
XMLHttpRequest.prototype.open=function(method,url,...rest){this.qaMethod=method;this.qaUrl=url;this.qaAuthenticated=false;return open.call(this,method,url,...rest)};
XMLHttpRequest.prototype.setRequestHeader=function(name,value){if(String(name).toLowerCase()==='authorization')this.qaAuthenticated=value==='Bearer '+localStorage.getItem('market_token');return header.call(this,name,value)};
XMLHttpRequest.prototype.send=function(body){if(!allowed(this.qaMethod,this.qaUrl,body)){t.blocked++;throw Error('Request blocked locally');}
this.addEventListener('load',()=>{try{observe(this.qaUrl,this.status,JSON.parse(this.responseText),this.qaAuthenticated)}catch{t.hiddenFields++;}});return send.call(this,body)};
const fetch=window.fetch;window.fetch=function(input,init){const req=new Request(input,init);
return req.clone().text().then(body=>{if(!allowed(req.method,req.url,body)){t.blocked++;throw Error('Request blocked locally');}
const authenticated=req.headers.get('authorization')==='Bearer '+localStorage.getItem('market_token');
return fetch.call(this,req).then(async r=>{if(new URL(req.url).pathname.startsWith('/api/')){
try{observe(req.url,r.status,await r.clone().json(),authenticated)}catch{t.hiddenFields++;}}return r;});});};
navigator.sendBeacon=()=>{t.blocked++;return false};
for(const name of ['alert','confirm','prompt'])window[name]=()=>{t.nativeAttempts++;throw Error('Native dialog blocked')};
t.selectedTarget=()=>t.options.versions.find(v=>v.id===selectedId);
t.chooseTarget=code=>{const v=t.options.versions.find(v=>v.version_code===code),s=modal().querySelector('select');
if(!v)throw Error('Fixture target missing');selectedId=v.id;t.quote=null;t.validation=null;s.value=v.id;s.dispatchEvent(new Event('change',{bubbles:true}));};
t.guard=()=>t.blocked===0&&t.nativeAttempts===0&&t.hiddenFields===0&&[...privateTokens].every(token=>
!document.documentElement.outerHTML.includes(token)&&!Object.keys(localStorage).some(key=>(localStorage.getItem(key)||'').includes(token)));
t.validationMatches=()=>t.quote&&t.validation&&JSON.stringify(t.quote)===JSON.stringify(t.validation);
t.matchesUI=()=>{const q=t.quote,m=modal();if(!q||!m||hidden(q)||q.execution_ready!==false||m.querySelector('[role=alert]'))return false;
if(q.target_version_code!==t.selectedTarget()?.version_code)return false;
const money=v=>new Intl.NumberFormat('zh-CN',{style:'currency',currency:q.currency}).format(v);
const summary=[...m.querySelectorAll('.version-change-summary dd')];
const total=q.action==='upgrade'?q.payment_amount:q.action==='downgrade'?q.refund_amount:q.net_difference;
if(summary.length!==4||summary[2].textContent.trim()!==money(total)||
!summary[0].textContent.includes(q.current_version_code)||!summary[0].textContent.includes(q.target_version_code))return false;
const segments=[...m.querySelectorAll('.version-change-segment')];if(segments.length!==q.segments.length)return false;
return q.segments.every((s,i)=>{const section=segments[i],dd=[...section.querySelectorAll('dd')],rows=[...section.querySelectorAll('tbody tr')];
if(!section.querySelector('h3')?.textContent.includes(s.order_no)||dd.length!==4||rows.length!==2||
dd[2].textContent.trim()!==(Number(s.remaining_ratio)*100).toFixed(4)+'%'||dd[3].textContent.trim()!==money(s.net_difference))return false;
const full=[...rows[0].querySelectorAll('td')],remaining=[...rows[1].querySelectorAll('td')];
return full.length===2&&remaining.length===2&&full[0].textContent.trim()===money(s.original_amount)&&
full[1].textContent.trim()===money(s.target_full_amount)&&remaining[0].textContent.trim()===money(s.original_remaining_amount)&&
remaining[1].textContent.trim()===money(s.target_remaining_amount);});};
t.sample=()=>({orderId:t.order.id,orderNo:t.expectedOrderNo,quotePosts:t.quotePosts,validatePosts:t.validatePosts,
action:t.quote.action,net_difference:t.quote.net_difference,payment_amount:t.quote.payment_amount,refund_amount:t.quote.refund_amount,
currency:t.quote.currency,execution_ready:t.quote.execution_ready,segments:t.quote.segments,
assets:performance.getEntriesByType('resource').map(r=>new URL(r.name).pathname).filter(p=>p.startsWith('/assets/')&&p.endsWith('.js'))});
})();
'@
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$instrumentation.Replace('__ORDER_NO__',$orderNoJson)} | Out-Null
    $script:stage = 'private buyer session injection'
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"} | Out-Null
    WaitJs '!!document.querySelector(".login-shell")'
    Js "localStorage.setItem('market_token',$tokenJson);true" | Out-Null
    $tokenJson = $null
    foreach ($viewport in @(@{name='desktop';width=1440;height=900},@{name='mobile';width=390;height=844})) {
        try { Check-Viewport $viewport.name $viewport.width $viewport.height }
        catch {
            $result.failures += @{viewport=$viewport.name;stage=$script:stage;reason=$_.Exception.Message}
            # Failure screenshots are guarded too: never capture leaked tokens.
            try { Screenshot "version-change-failed-$($viewport.name).png" } catch { }
            Save-Result
        }
    }
    if ($result.failures.Count) { throw 'Viewport acceptance failed' }
    Guard
    $result.status = 'passed'; $result.passed = $true
    Save-Result
    Write-Output ($result | ConvertTo-Json -Depth 25)
} catch {
    $result.status = 'failed'; $result.failedStage = $script:stage
    Save-Result
    throw "Version change quote acceptance failed at $script:stage; details suppressed; inspect browser-results.json"
} finally {
    $fixture = $null; $secret = $null; $tokenJson = $null
    if ($socket) { try { Js 'localStorage.removeItem("market_token");true' | Out-Null } catch { }; $socket.Dispose() }
    if ($chrome) {
        Get-CimInstance Win32_Process | Where-Object {$_.Name -eq 'chrome.exe' -and $_.CommandLine -like "*$profile*"} |
            ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}
    }
    if (Test-Path -LiteralPath $profile) { Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue }
}
