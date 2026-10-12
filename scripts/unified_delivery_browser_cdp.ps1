param(
    [string]$FixtureFile,
    [string]$ManifestFile,
    [switch]$StartupOnly,
    [string]$OutputDir = "$env:TEMP\market-unified-delivery-browser",
    [string]$ChromePath = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
)
$ErrorActionPreference = 'Stop'
$socket = $null
$chrome = $null
$fixture = $null
$tokenJson = $null
$profile = Join-Path $env:TEMP ('market-uda-' + [Guid]::NewGuid().ToString('N'))
$script:commandId = 0
$script:blockedRequests = 0
$script:managementAttempts = 0
$script:nativeDialogs = 0
$script:stage = 'private fixture validation'
$script:outputReady = $false
$script:suppressedBackground = [ordered]@{stream=0;senderContext=0}
$result = [ordered]@{
    status='pending'; mode='browser acceptance'; checks=@(); screenshots=@(); samples=@(); failures=@();
    startupDiagnostics=[ordered]@{executableFound=$false;processStarted=$false;portFileFound=$false;
        portFileValid=$false;pageTargetCount=0;targetType='none';websocketLoopback=$false;socketConnected=$false;cdpReady=$false};
    managementNative='not tested; api-credentials GET explicitly denied';
    databaseReadonly='not measured by browser; separate PG acceptance required';
    positiveSSO='not tested'; deploymentProvenance='supplied tag, not server image verification';
    backgroundNotifications='stream and sender-context GET intentionally rejected locally; no native SSE/Redis/sender-context proof';
    limits=@('Synthetic fixtures only; no APISIX, payment, scan, storage or positive SSO proof',
        'GET-only scoped network; no management click, entitlement write, POST launch or download',
        'SaaS negative only; malformed URL isolation and missing-config/error UI deferred')
}

function Resolve-PrivatePosixFile([string]$value) {
    if (-not $value.StartsWith('/') -or $value.Contains("`n") -or $value.Contains("`r")) {
        throw 'Private inputs must be absolute WSL POSIX paths'
    }
    $description = & wsl.exe --exec stat --printf '%a|%F' -- $value 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]$description -cne '600|regular file') {
        throw 'Private input must be a regular 0600 file; symlinks refused'
    }
    $parent = $value.Substring(0, $value.LastIndexOf('/'))
    $directoryMode = & wsl.exe --exec stat --printf '%a|%F' -- $parent 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]$directoryMode -cne '700|directory') {
        throw 'Private fixture directory must be 0700'
    }
    $converted = & wsl.exe --exec wslpath -w $value 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $converted) { throw 'Private path conversion failed' }
    return [string]($converted | Select-Object -Last 1)
}

function Allowed-Request([string]$method, [string]$url) {
    try {
        $uri = [Uri]$url
        if ($method -cne 'GET' -or $uri.GetLeftPart([UriPartial]::Authority) -cne $script:baseUrl -or
            $uri.UserInfo -or $uri.Fragment) { return $false }
        $path = $uri.AbsolutePath
        # This GET can touch Redis/gateway metadata. Never permit it, even owned.
        if ($path -match '/api-credentials(?:/|$)' -or $path -match '^/gateway(?:/|$)' -or
            $path -match '^/api/files/' -or $path -match '^/api/.*(?:entitlement|payment|delivery-launch)') { return $false }
        if ($path.StartsWith('/api/')) {
            if ($uri.Query) { return $false }
            if ($path -cin @('/api/auth/me','/api/storefront/buyer-enterprises','/api/notifications/summary','/api/orders')) {
                return $true
            }
            return $path -cin $script:accessPaths
        }
        if ($path -ceq '/console') { return $uri.Query -ceq '?view=orders' }
        return (-not $uri.Query -and ($path -match '^/assets/[A-Za-z0-9_./-]+$' -or
            $path -cin @('/favicon.ico','/logo.svg')))
    } catch { return $false }
}

function Send-Cdp([string]$method, $parameters) {
    $script:commandId++
    $payload = @{id=$script:commandId; method=$method; params=$parameters} | ConvertTo-Json -Depth 30 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $timeout = [Threading.CancellationTokenSource]::new(30000)
    try {
        $null = $socket.SendAsync([ArraySegment[byte]]::new($bytes),
            [Net.WebSockets.WebSocketMessageType]::Text,$true,$timeout.Token).GetAwaiter().GetResult()
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
                        $part = $socket.ReceiveAsync([ArraySegment[byte]]::new($buffer),$timeout.Token).GetAwaiter().GetResult()
                        if ($part.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close) { throw 'CDP closed' }
                        $memory.Write($buffer,0,$part.Count)
                    } while (-not $part.EndOfMessage)
                    $answer = [Text.Encoding]::UTF8.GetString($memory.ToArray()) | ConvertFrom-Json
                } finally { $memory.Dispose() }
                if ($answer.method -eq 'Fetch.requestPaused') {
                    $request = $answer.params.request
                    if (Allowed-Request $request.method $request.url) {
                        Send-Cdp 'Fetch.continueRequest' @{requestId=$answer.params.requestId} | Out-Null
                    } else {
                        $script:blockedRequests++
                        if ([string]$request.url -match '/api-credentials(?:/|\?|$)') { $script:managementAttempts++ }
                        Send-Cdp 'Fetch.failRequest' @{requestId=$answer.params.requestId;errorReason='BlockedByClient'} | Out-Null
                    }
                } elseif ($answer.method -eq 'Page.javascriptDialogOpening') {
                    $script:nativeDialogs++
                    Send-Cdp 'Page.handleJavaScriptDialog' @{accept=$false} | Out-Null
                }
                if ($answer.id -eq $id) {
                    if ($answer.error) { throw 'CDP command rejected' }
                    return $answer.result
                }
            }
        } finally { $timeout.Dispose() }
    } catch { throw 'CDP operation failed; private details suppressed' }
}

function Js([string]$expression) {
    try {
        $response = Cdp 'Runtime.evaluate' @{expression=$expression;awaitPromise=$true;returnByValue=$true}
        if ($response.exceptionDetails) { throw 'Evaluation rejected' }
        return $response.result.value
    } catch { throw 'Browser evaluation failed; private details suppressed' }
}

function WaitJs([string]$expression) {
    for ($i=0; $i -lt 80; $i++) {
        if (Js $expression) { return }
        Start-Sleep -Milliseconds 250
    }
    throw 'Browser condition timed out'
}

function Assert-Browser([string]$expression, [string]$message) {
    if (-not (Js $expression)) { throw $message }
}

function Failure-Probe {
    # Only schema-authored booleans, counters and HTTP statuses leave the page.
    return Js @'
(() => {
const t=window.__udaProbe;
return {probePresent:!!t,sidebarPresent:!!document.querySelector('.sidebar'),loginPresent:!!document.querySelector('.login-shell'),
ordersTablePresent:!!document.querySelector('.order-no'),orderHeadingPresent:[...document.querySelectorAll('h1')].some(n=>n.textContent.trim()==='\u8ba2\u5355\u4e2d\u5fc3'),
consolePath:location.pathname==='/console',ordersQuery:new URLSearchParams(location.search).get('view')==='orders',
sessionOK:t?.sessionOK===true,ordersOK:t?.ordersOK===true,enterprisesOK:t?.enterprisesOK===true,
blocked:Number(t?.blocked||0),unsafe:t?.unsafe===true,
suppressedBackground:t?.suppressedBackground||{stream:0,senderContext:0},
responses:t?.diagnostics||{}};
})()
'@
}

function Collect-SuppressedBackground {
    if (-not $socket -or $StartupOnly) { return }
    try {
        $counts = Js 'window.__udaProbe?.takeBackgroundCounts?.() || {stream:0,senderContext:0}'
        $script:suppressedBackground.stream += [int]$counts.stream
        $script:suppressedBackground.senderContext += [int]$counts.senderContext
    } catch { }
}

function Guard {
    Assert-Browser 'window.__udaProbe.guard()' 'Private DOM, session, response or request guard failed'
    if ($script:blockedRequests -or $script:nativeDialogs) { throw 'Unexpected network or native-dialog attempt' }
}

function Screenshot([string]$name) {
    Guard
    $shot = Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $script:artifactDir $name),[Convert]::FromBase64String($shot.data))
    $result.screenshots += $name
}

function Connect-OwnedChrome {
    $script:stage = 'Chrome executable validation'
    if (-not (Test-Path -LiteralPath $ChromePath -PathType Leaf)) { throw 'Chrome executable missing; supply ChromePath' }
    $result.startupDiagnostics.executableFound = $true
    $script:stage = 'owned Chrome profile permissions'
    New-Item -ItemType Directory -Path $profile | Out-Null
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true,$false)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.SetOwner($sid)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
    Set-Acl -LiteralPath $profile -AclObject $acl
    $script:stage = 'owned Chrome process launch'
    $script:chrome = Start-Process $ChromePath -ArgumentList @('--headless=new','--remote-debugging-port=0',
        "--user-data-dir=`"$profile`"",'--no-first-run','--no-default-browser-check','--disable-background-networking',
        '--disable-sync','--disable-extensions','about:blank') -PassThru
    $result.startupDiagnostics.processStarted = $true
    $target = $null
    $port = $null
    $script:stage = 'owned Chrome DevTools port discovery'
    for ($i=0; $i -lt 60; $i++) {
        try {
            $portPath = Join-Path $profile 'DevToolsActivePort'
            if (Test-Path -LiteralPath $portPath) { $result.startupDiagnostics.portFileFound = $true }
            $activePort = @(Get-Content -LiteralPath $portPath)
            if ($activePort.Count -eq 2 -and $activePort[0] -match '^\d{1,5}$' -and
                [int]$activePort[0] -ge 1 -and [int]$activePort[0] -le 65535 -and
                $activePort[1] -match '^/devtools/browser/[A-Za-z0-9-]+$') {
                $port = [int]$activePort[0]
                $result.startupDiagnostics.portFileValid = $true
                $script:stage = 'owned Chrome page target discovery'
                # Windows PowerShell does not enumerate Invoke-RestMethod arrays
                # directly in a pipeline. Assign first to select one page object.
                $pages = Invoke-RestMethod "http://127.0.0.1:$port/json/list" -TimeoutSec 2
                $pageTargets = @($pages | Where-Object {$_.type -ceq 'page' -and $_.url -ceq 'about:blank'})
                $result.startupDiagnostics.pageTargetCount = $pageTargets.Count
                $target = $pageTargets | Select-Object -First 1
            }
        } catch { }
        if ($target) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $target) { throw 'Owned Chrome startup discovery timed out' }
    $script:stage = 'owned Chrome WebSocket target validation'
    $result.startupDiagnostics.targetType = $target.GetType().Name
    $debugUri = [Uri]$target.webSocketDebuggerUrl
    if ($debugUri.Scheme -cne 'ws' -or $debugUri.Host -cne '127.0.0.1' -or $debugUri.Port -ne $port -or
        $debugUri.UserInfo -or $debugUri.Query -or $debugUri.Fragment -or
        $debugUri.AbsolutePath -notmatch '^/devtools/page/[A-Za-z0-9-]+$') { throw 'Unexpected owned debug target' }
    $result.startupDiagnostics.websocketLoopback = $true
    $script:stage = 'owned Chrome WebSocket connection'
    $script:socket = [Net.WebSockets.ClientWebSocket]::new()
    $timeout = [Threading.CancellationTokenSource]::new(30000)
    try { $null = $socket.ConnectAsync($debugUri,$timeout.Token).GetAwaiter().GetResult() }
    finally { $timeout.Dispose() }
    $result.startupDiagnostics.socketConnected = $true
    $script:stage = 'owned Chrome CDP initialization'
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Network.enable' @{} | Out-Null
    Cdp 'Network.setCacheDisabled' @{cacheDisabled=$true} | Out-Null
    Cdp 'Network.setBypassServiceWorker' @{bypass=$true} | Out-Null
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})} | Out-Null
    Cdp 'Browser.setDownloadBehavior' @{behavior='deny'} | Out-Null
    $result.startupDiagnostics.cdpReady = $true
}

function Check-Viewport([string]$name, [int]$width, [int]$height) {
    Collect-SuppressedBackground
    $script:stage = "$name exact buyer console"
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=$height;deviceScaleFactor=1;mobile=($name -eq 'mobile')} | Out-Null
    Cdp 'Page.navigate' @{url="$script:baseUrl/console?view=orders"} | Out-Null
    WaitJs '!!document.querySelector(".sidebar") && window.__udaProbe.sessionOK && window.__udaProbe.ordersOK && window.__udaProbe.enterprisesOK'
    Assert-Browser 'window.__udaProbe.columnButtons()' 'Exact owned second-column labels or controls differ'
    Guard
    Screenshot "unified-delivery-orders-$name.png"
    foreach ($method in @('api','model_api','file','tenant_access')) {
        $script:stage = "$name $method GET access modal"
        $methodJson = $method | ConvertTo-Json -Compress
        Js "window.__udaProbe.open($methodJson);true" | Out-Null
        WaitJs "window.__udaProbe.loaded($methodJson)"
        Assert-Browser "window.__udaProbe.matches($methodJson)" 'Real GET DTO or modal metadata differs'
        Assert-Browser 'window.__udaProbe.fits()' 'Page or modal has horizontal overflow or controls outside viewport'
        # File and SaaS validate metadata/reason only. Never click their actions.
        if ($method -in @('api','model_api')) { Screenshot "unified-delivery-$method-$name.png" }
        $sample = Js "window.__udaProbe.sample($methodJson)"
        $result.samples += @{viewport=$name;method=$method;status='passed';metadata=$sample}
        Js 'document.querySelector("[aria-labelledby=delivery-access-title] .drawer-head button").click();true' | Out-Null
        WaitJs '!document.querySelector("[aria-labelledby=delivery-access-title]")'
        Guard
    }
    $result.checks += "$name second-column labels; actual GET DTO; API 30082/zero quotas; model missing consumer management control; file metadata; SaaS negative; no overflow"
}

try {
    if ($StartupOnly) {
        $result.mode = 'startup-only; no fixtures, tokens, API or acceptance navigation'
        if ($FixtureFile -or $ManifestFile) { throw 'Startup-only refuses fixture inputs' }
        Connect-OwnedChrome
        $script:stage = 'owned Chrome about:blank probe'
        Assert-Browser 'location.href === "about:blank"' 'Startup probe must stay on about:blank'
        $result.checks += 'owned Chrome about:blank CDP bootstrap only'
        $result.status = 'passed'
    } else {
    if (-not $FixtureFile -or -not $ManifestFile) { throw 'Acceptance requires FixtureFile and ManifestFile' }
    if (-not $FixtureFile.EndsWith('.browser.json') -or
        $ManifestFile -cne ($FixtureFile.Substring(0,$FixtureFile.Length-13) + '.json')) {
        throw 'Fixture must be the sidecar of the supplied ownership manifest'
    }
    $fixturePath = Resolve-PrivatePosixFile $FixtureFile
    $manifestPath = Resolve-PrivatePosixFile $ManifestFile
    try {
        $fixture = Get-Content -LiteralPath $fixturePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch { throw 'Private JSON read failed; details suppressed' }
    $runId = [string]$manifest.run_id
    if ($manifest.schema -ne 1 -or $manifest.phase -cne 'prepared' -or
        $runId -cnotmatch '^qa-uda-[0-9a-f]{12}$' -or $fixture.schema -ne 1 -or
        $fixture.profile -cne 'unified-delivery-browser-negative-saas' -or $fixture.runId -cne $runId -or
        $fixture.userId -cne ($runId + '-u-buyer') -or $fixture.enterpriseId -cne ($runId + '-e-buyer') -or
        $manifest.users.buyer -cne $fixture.userId -or $manifest.enterprises[0] -cne $fixture.enterpriseId) {
        throw 'New synthetic fixture ownership mismatch'
    }
    $serializedScope = $manifest | ConvertTo-Json -Depth 30 -Compress
    if ($serializedScope -match 'ORD-261012005144E1B3|productqa-vcq-a044ea9ea181-p2') { throw 'Protected scope refused' }
    $script:baseUrl = [string]$fixture.baseUrl
    $site = [Uri]$script:baseUrl
    if ($site.Scheme -notin @('http','https') -or $site.UserInfo -or $site.Query -or $site.Fragment -or
        $site.AbsolutePath -ne '/' -or $script:baseUrl -cne $site.GetLeftPart([UriPartial]::Authority) -or
        [string]$fixture.gatewayOrigin -cne 'http://192.168.10.10:30082' -or
        [string]$fixture.deploymentTag -cnotmatch '^[A-Za-z0-9_.-]{1,100}$') { throw 'Invalid origin or provenance' }
    $script:accessPaths = @()
    $ownedOrderIds = @()
    $methods = @('file','api','model_api','tenant_access')
    for ($i=0; $i -lt $methods.Count; $i++) {
        $method = $methods[$i]
        $entry = $fixture.orders.$method
        $expected = $runId + '-o' + $i + '-source'
        if ($entry.id -cne $expected -or $entry.orderNo -cne $expected -or $entry.method -cne $method -or
            $manifest.orders.$method.source -cne $expected -or $manifest.products.$method -cne ($runId + '-p' + $i)) {
            throw 'Exact owned browser order mismatch'
        }
        $script:accessPaths += '/api/orders/' + $expected + '/delivery-access'
        foreach ($role in @('source','current','future')) {
            $id = $runId + '-o' + $i + '-' + $role
            if ($manifest.orders.$method.$role -cne $id -or
                $manifest.versions.$method.$role -cne ($runId + '-v' + $i + '-' + $role)) { throw 'Changed fixture identifiers' }
            $ownedOrderIds += $id
        }
    }
    if ($fixture.orders.api.ready -ne $true -or $fixture.orders.model_api.ready -ne $false -or
        $fixture.orders.file.ready -ne $true -or $fixture.orders.tenant_access.ready -ne $false -or
        $fixture.orders.api.limits.daily_quota -ne 0 -or $fixture.orders.api.limits.monthly_quota -ne 0 -or
        $fixture.orders.api.limits.quota_amount -ne 0) { throw 'Wrong synthetic browser scenario profile' }
    $secret = [string]$fixture.buyerToken
    if ($secret -cnotmatch '^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$') { throw 'Private buyer JWT required' }
    $tokenJson = $secret | ConvertTo-Json -Compress
    $secret = $null
    # Public instrumentation has exact IDs and synthetic sentinels, never the JWT.
    $spec = @{runId=$runId;userId=$fixture.userId;enterpriseId=$fixture.enterpriseId;orders=$fixture.orders;
        ownedOrderIds=$ownedOrderIds;accessPaths=$script:accessPaths;sensitiveSentinels=$fixture.sensitiveSentinels}
    $specJson = $spec | ConvertTo-Json -Depth 25 -Compress
    $result.deploymentTag = [string]$fixture.deploymentTag
    $fixture = $null; $manifest = $null; $serializedScope = $null
    $script:artifactDir = Join-Path $OutputDir ($runId + '-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $script:artifactDir -Force | Out-Null
    $result.artifactDirectory = $script:artifactDir
    $script:outputReady = $true
    Connect-OwnedChrome
    $instrumentation = @'
(() => {
const spec=__SPEC__;
const t=window.__udaProbe={sessionOK:false,ordersOK:false,enterprisesOK:false,access:{},blocked:0,unsafe:false,
suppressedBackground:{stream:0,senderContext:0},
diagnostics:{auth:{seen:0,status:0},enterprises:{seen:0,status:0},orders:{seen:0,status:0},notifications:{seen:0,status:0},parseFailures:0}};
const modal=()=>document.querySelector('[aria-labelledby=delivery-access-title]');
const row=method=>[...document.querySelectorAll('.order-no')].find(n=>n.textContent.trim()===spec.orders[method].orderNo)?.closest('tr');
const labels={file:'\u4e0b\u8f7d\u6587\u4ef6',api:'\u67e5\u770b\u63a5\u5165\u4fe1\u606f',model_api:'\u67e5\u770b\u63a5\u5165\u4fe1\u606f',tenant_access:'\u8bbf\u95ee\u5e94\u7528'};
const allowed=(method,url)=>{try{const u=new URL(url,location.origin);return method==='GET'&&u.origin===location.origin&&!u.username&&!u.password&&!u.search&&!u.hash&&
['/api/auth/me','/api/storefront/buyer-enterprises','/api/notifications/summary','/api/orders',...spec.accessPaths].includes(u.pathname);}catch{return false;}};
const suppressBackground=(method,url)=>{try{const u=new URL(url,location.origin);
if(method!=='GET'||u.origin!==location.origin||u.username||u.password||u.search||u.hash)return false;
const key=u.pathname==='/api/notifications/stream'?'stream':u.pathname==='/api/notifications/sender-context'?'senderContext':null;
if(!key)return false;t.suppressedBackground[key]++;return true;}catch{return false;}};
t.takeBackgroundCounts=()=>{const counts={...t.suppressedBackground};t.suppressedBackground.stream=0;t.suppressedBackground.senderContext=0;return counts;};
const hidden=value=>value&&typeof value==='object'&&Object.entries(value).some(([key,child])=>/secret|upstream|cost|hash|token|key_prefix|apisix_consumer/i.test(key)||['key','api_key'].includes(key)||hidden(child));
const observe=(url,status,data,noStore,authenticated)=>{
const p=new URL(url,location.origin).pathname;
if(p==='/api/auth/me'){
const u=data.user,e=data.enterprise,member=(u?.enterprise_memberships||[]).find(m=>m.enterprise_id===spec.enterpriseId);
t.diagnostics.auth={seen:t.diagnostics.auth.seen+1,status,authenticated:!!authenticated,userPresent:!!u,enterprisePresent:!!e,
userIdMatches:u?.id===spec.userId,verified:u?.verified_status==='verified',emailVerified:u?.email_verified===true,
active:u?.is_active!==false,noPlatformRole:!u?.platform_role,activationActive:u?.activation_status==='active',
enterpriseIdMatches:e?.id===spec.enterpriseId,membershipsArray:Array.isArray(u?.enterprise_memberships),memberPresent:!!member,
memberRoleAllowed:['super_admin','enterprise_admin'].includes(member?.role),memberActive:member?.status==='active'};
const a=t.diagnostics.auth;
t.sessionOK=status===200&&a.authenticated&&a.userIdMatches&&a.verified&&a.emailVerified&&a.active&&
a.noPlatformRole&&a.activationActive&&a.enterpriseIdMatches&&a.memberRoleAllowed&&a.memberActive;}
if(p==='/api/storefront/buyer-enterprises'){
const items=Array.isArray(data.items)?data.items:[],enterprise=items.find(e=>e.id===spec.enterpriseId);
t.diagnostics.enterprises={seen:t.diagnostics.enterprises.seen+1,status,authenticated:!!authenticated,itemsArray:Array.isArray(data.items),
expectedEnterprisePresent:!!enterprise,verified:enterprise?.verification_status==='verified',eligible:enterprise?.eligible===true};
t.enterprisesOK=status===200&&!!enterprise&&enterprise.verification_status==='verified';}
if(p==='/api/orders'){
const items=Array.isArray(data.items)?data.items:[];
t.diagnostics.orders={seen:t.diagnostics.orders.seen+1,status,authenticated:!!authenticated,itemsArray:Array.isArray(data.items),
allIdsOwned:items.every(o=>spec.ownedOrderIds.includes(o.id)),allBuyerMatches:items.every(o=>o.buyer_enterprise_id===spec.enterpriseId),
expectedIdsPresent:Object.values(spec.orders).every(w=>items.some(o=>o.id===w.id)),
expectedNumbersMatch:Object.values(spec.orders).every(w=>items.some(o=>o.id===w.id&&o.order_no===w.orderNo)),
expectedSnapshots:Object.values(spec.orders).every(w=>items.some(o=>o.id===w.id&&o.snapshot_version===1)),
expectedPaid:Object.values(spec.orders).every(w=>items.some(o=>o.id===w.id&&o.payment_status==='paid')),
expectedCompleted:Object.values(spec.orders).every(w=>items.some(o=>o.id===w.id&&o.main_status==='completed')),
expectedAccepted:Object.values(spec.orders).every(w=>items.some(o=>o.id===w.id&&o.delivery_status==='accepted'))};
const o=t.diagnostics.orders;
t.ordersOK=status===200&&o.itemsArray&&o.allIdsOwned&&o.allBuyerMatches&&o.expectedNumbersMatch&&o.expectedSnapshots&&o.expectedPaid&&o.expectedCompleted&&o.expectedAccepted;}
if(p==='/api/notifications/summary')t.diagnostics.notifications={seen:t.diagnostics.notifications.seen+1,status,authenticated:!!authenticated};
for(const [method,w]of Object.entries(spec.orders))if(p==='/api/orders/'+w.id+'/delivery-access'){
const encoded=JSON.stringify(data);if(hidden(data)||spec.sensitiveSentinels.some(s=>s&&encoded.includes(s)))t.unsafe=true;
if(Object.keys(data).sort().join('|')!==['order_id','method','ready','reason','files','api','saas'].sort().join('|'))t.unsafe=true;
t.access[method]={status,noStore,authenticated,data};}
};
const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send,header=XMLHttpRequest.prototype.setRequestHeader;
XMLHttpRequest.prototype.open=function(method,url,...rest){this.udaMethod=method;this.udaUrl=url;this.udaAuth=false;return open.call(this,method,url,...rest);};
XMLHttpRequest.prototype.setRequestHeader=function(name,value){if(String(name).toLowerCase()==='authorization')this.udaAuth=value==='Bearer '+localStorage.getItem('market_token');return header.call(this,name,value);};
XMLHttpRequest.prototype.send=function(body){if(suppressBackground(this.udaMethod,this.udaUrl))throw Error('Background GET intentionally suppressed');
if(!allowed(this.udaMethod,this.udaUrl)){t.blocked++;throw Error('Blocked locally');}
this.addEventListener('load',()=>{try{observe(this.udaUrl,this.status,JSON.parse(this.responseText),this.getResponseHeader('cache-control')==='no-store',this.udaAuth);}catch{t.unsafe=true;t.diagnostics.parseFailures++;}});return send.call(this,body);};
const fetchOriginal=window.fetch;window.fetch=function(input,init){const req=new Request(input,init);
if(suppressBackground(req.method,req.url))return Promise.reject(Error('Background GET intentionally suppressed'));
if(!allowed(req.method,req.url)){t.blocked++;return Promise.reject(Error('Blocked locally'));}
const authenticated=req.headers.get('authorization')==='Bearer '+localStorage.getItem('market_token');return fetchOriginal.call(this,input,init).then(r=>{
r.clone().json().then(data=>observe(req.url,r.status,data,r.headers.get('cache-control')==='no-store',authenticated)).catch(()=>{t.unsafe=true;t.diagnostics.parseFailures++;});return r;});};
navigator.sendBeacon=()=>{t.blocked++;return false;};
for(const name of ['open','alert','confirm','prompt'])window[name]=()=>{t.blocked++;throw Error('Native or external navigation blocked');};
for(const name of ['WebSocket','Worker','SharedWorker'])window[name]=function(){t.blocked++;throw Error('Background channel blocked');};
t.columnButtons=()=>Object.keys(spec.orders).every(method=>{const r=row(method),c=r?.querySelectorAll('td')[1],b=c?.querySelector('button');
return r?.closest('table').querySelectorAll('th')[1]?.textContent.trim()==='\u5e94\u7528\u8bbf\u95ee\u5730\u5740'&&b?.textContent.trim()===labels[method]&&!b.disabled&&!c.querySelector('a[href]');});
t.open=method=>{t.access[method]=null;const b=row(method)?.querySelectorAll('td')[1]?.querySelector('button');if(!b||b.disabled)throw Error('Owned entry absent');b.click();};
t.loaded=method=>!!modal()&&!!t.access[method]&&!modal().textContent.includes('\u6b63\u5728\u52a0\u8f7d\u4ea4\u4ed8\u4fe1\u606f');
t.matches=method=>{
const m=modal(),a=t.access[method],w=spec.orders[method],d=a?.data;
if(!m||!a||a.status!==200||!a.noStore||!a.authenticated||d.order_id!==w.id||d.method!==method||d.ready!==w.ready||
m.getAttribute('role')!=='dialog'||m.getAttribute('aria-modal')!=='true'||m.querySelector('.drawer-head p')?.textContent.trim()!==w.orderNo||m.querySelector('[role=alert]'))return false;
if(['api','model_api'].includes(method)){
const api=d.api,summary=m.querySelector('.delivery-api-summary'),text=summary?.textContent||'',manage=[...summary?.querySelectorAll('button')||[]].find(b=>b.textContent.trim()==='\u7ba1\u7406\u4f01\u4e1a\u51ed\u636e');
if(!api||!manage||manage.disabled||api.gateway_url!==w.gatewayUrl||api.version_code!==w.versionCode||api.expires_at!==w.expiresAt||
api.credentials_path!=='/api/orders/'+w.id+'/api-credentials'||api.shared_scope!=='enterprise'||api.gateway_base_path!==new URL(w.gatewayUrl).pathname||
Object.keys(api.limits).length!==Object.keys(w.limits).length||Object.entries(w.limits).some(([k,v])=>api.limits[k]!==v)||summary.querySelector('code')?.textContent!==w.gatewayUrl||!text.includes(w.versionCode))return false;
const value=label=>[...summary.querySelectorAll('dl>div')].find(n=>n.querySelector('dt')?.textContent.trim()===label)?.querySelector('dd')?.textContent.trim();
if(value('\u5230\u671f\u65f6\u95f4')!==new Date(w.expiresAt).toLocaleString('zh-CN',{hour12:false})||value('\u5171\u4eab\u8303\u56f4')!=='\u4f01\u4e1a\u5171\u4eab')return false;
if(value('\u6bcf\u5206\u949f\u9650\u6d41')!==String(w.limits.rate_limit_per_minute)||value('\u6bcf\u65e5\u989d\u5ea6')!==(w.limits.daily_quota===0?'\u4e0d\u9650':String(w.limits.daily_quota))||
value('\u6bcf\u6708\u989d\u5ea6')!==(w.limits.monthly_quota===0?'\u4e0d\u9650':String(w.limits.monthly_quota))||value('\u989d\u5ea6\u503c\uff08\u539f\u59cb\u5355\u4f4d\uff09')!==String(w.limits.quota_amount))return false;
if(method==='model_api'&&(!d.reason||!text.includes(d.reason)))return false;
return !m.querySelector('a[href]');}
if(method==='file')return d.files.length===1&&d.files[0].remaining_downloads===2&&m.textContent.includes(d.files[0].name)&&!!m.querySelector('tbody button');
return d.ready===false&&!!d.reason&&m.textContent.includes(d.reason)&&!m.querySelector('.delivery-saas-summary');
};
t.fits=()=>{const m=modal(),r=m?.getBoundingClientRect();if(!r||r.left< -1||r.top< -1||r.right>innerWidth+1||r.bottom>innerHeight+1||
m.scrollWidth>m.clientWidth+1||document.documentElement.scrollWidth>innerWidth+1)return false;
return [...m.querySelectorAll('.drawer-head button,.modal-actions button,.delivery-api-summary button,code,.subscription-summary dd')].every(n=>{
const b=n.getBoundingClientRect();return b.left>=r.left-1&&b.right<=r.right+1&&n.scrollWidth<=n.clientWidth+1;});};
t.guard=()=>{const jwt=localStorage.getItem('market_token'),html=document.documentElement.outerHTML;
return t.sessionOK&&t.ordersOK&&t.enterprisesOK&&!t.blocked&&!t.unsafe&&!document.querySelector('input[type=password]')&&
!(jwt&&html.includes(jwt))&&!/[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}/.test(html)&&
!/\bSecret\b|upstream|key_hash|synthetic-checksum/i.test(html)&&!spec.sensitiveSentinels.some(s=>s&&html.includes(s));};
t.sample=method=>{const a=t.access[method],m=modal(),r=m.getBoundingClientRect();return {http:a.status,noStore:a.noStore,ready:a.data.ready,
gatewayUrl:a.data.api?.gateway_url||null,version:a.data.api?.version_code||null,limits:a.data.api?.limits||null,
managementControl:!!m.querySelector('.delivery-api-summary button'),bounds:{left:r.left,right:r.right,top:r.top,bottom:r.bottom}};};
})();
'@
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$instrumentation.Replace('__SPEC__',$specJson)} | Out-Null
    $specJson = $null; $spec = $null
    $script:stage = 'private Runtime session injection'
    Cdp 'Page.navigate' @{url="$script:baseUrl/console?view=orders"} | Out-Null
    WaitJs '!!document.querySelector(".login-shell")'
    Js "(()=>{document.querySelectorAll('input[type=password]').forEach(n=>{n.value='';});localStorage.setItem('market_token',$tokenJson);return true;})()" | Out-Null
    $tokenJson = $null
    foreach ($viewport in @(@{name='desktop';width=1440;height=900},@{name='mobile';width=390;height=844})) {
        Check-Viewport $viewport.name $viewport.width $viewport.height
    }
    Guard
    $result.status = 'passed'
    }
} catch {
    $result.status = 'failed'
    $result.failures += @{stage=$script:stage;errorType=$_.Exception.GetType().Name;
        reason='Operation failed; inspect phase and startupDiagnostics; private details suppressed'}
    if ($socket -and -not $StartupOnly) {
        try { $result.failureProbe = Failure-Probe }
        catch { $result.failureProbe = @{available=$false} }
    }
    # No unguarded failure screenshot and no raw CDP/HTTP body or exception output.
} finally {
    Collect-SuppressedBackground
    $result.suppressedBackground = $script:suppressedBackground
    $fixture = $null; $secret = $null; $tokenJson = $null; $specJson = $null
    if ($socket) {
        if (-not $StartupOnly) { try { Js 'localStorage.removeItem("market_token");true' | Out-Null } catch { } }
        $socket.Dispose()
    }
    if ($chrome) {
        Get-CimInstance Win32_Process | Where-Object {$_.Name -eq 'chrome.exe' -and $_.CommandLine -like "*$profile*"} |
            ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}
        for ($i=0; $i -lt 20; $i++) {
            try { if (Test-Path -LiteralPath $profile) { Remove-Item -LiteralPath $profile -Recurse -Force }; break }
            catch { Start-Sleep -Milliseconds 250 }
        }
    } elseif (Test-Path -LiteralPath $profile) {
        Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue
    }
    $result.chromeProfileDeleted = -not (Test-Path -LiteralPath $profile)
    if (-not $result.chromeProfileDeleted) { $result.status='failed'; $result.failures += @{stage='profile cleanup';reason='Owned profile remains'} }
    $result.blockedRequests = $script:blockedRequests
    $result.managementRequestsAttempted = $script:managementAttempts
    $result.nativeDialogs = $script:nativeDialogs
    if ($script:outputReady) { $result | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $script:artifactDir 'browser-results.json') -Encoding UTF8 }
}
Write-Output ($result | ConvertTo-Json -Depth 20)
if ($result.status -ne 'passed') { exit 1 }
