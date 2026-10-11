param(
    [Parameter(Mandatory=$true)][string]$FixtureFile,
    [string]$OutputDir = "$env:TEMP\market-online-delivery-browser",
    [string]$BaseUrl,
    [string]$ExpectedOrderNo,
    [string]$DeploymentTag,
    [switch]$VerifyBackendAccess
)
$ErrorActionPreference = 'Stop'
$socket = $null
$chrome = $null
$tokenJson = $null
$fixture = $null
$profile = Join-Path $env:TEMP ('market-online-delivery-' + [Guid]::NewGuid().ToString('N'))
$script:commandId = 0
$script:blockedMutations = 0
$script:downloadAttempts = 0
$script:networkMutations = 0
$script:nativeDialogs = 0
$stage = 'fixture validation'
$result = [ordered]@{status='pending';passed=$false;mode='read-only';checks=@();screenshots=@();samples=@();failures=@();
    limits=@('Completed file order and private buyer administrator fixture are supplied by the main agent',
        'No download requests or order mutations; deploymentTag records supplied provenance, not a server version assertion')}

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

function Send-Cdp([string]$method, $parameters) {
    $script:commandId++
    $payload = @{id=$script:commandId;method=$method;params=$parameters} | ConvertTo-Json -Depth 30 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $timeout = [Threading.CancellationTokenSource]::new(30000)
    try { $null = $socket.SendAsync([ArraySegment[byte]]::new($bytes),[Net.WebSockets.WebSocketMessageType]::Text,$true,$timeout.Token).GetAwaiter().GetResult() }
    finally { $timeout.Dispose() }
    return $script:commandId
}

function Is-FileDownload([string]$url) {
    try { return ([Uri]$url).AbsolutePath -match '^/api/files/[^/]+/download/?$' }
    catch { return $false }
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
                    $download = Is-FileDownload $answer.params.request.url
                    $mutation = $answer.params.request.method -notin @('GET','HEAD','OPTIONS')
                    if ($download -or $mutation) {
                        if ($download) { $script:downloadAttempts++ }
                        if ($mutation) { $script:blockedMutations++ }
                        Send-Cdp 'Fetch.failRequest' @{requestId=$answer.params.requestId;errorReason='BlockedByClient'} | Out-Null
                    } else {
                        Send-Cdp 'Fetch.continueRequest' @{requestId=$answer.params.requestId} | Out-Null
                    }
                } elseif ($answer.method -eq 'Network.requestWillBeSent') {
                    if ($answer.params.request.method -notin @('GET','HEAD','OPTIONS')) { $script:networkMutations++ }
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
    $result.blockedMutationRequests = $script:blockedMutations
    $result.networkMutationEvents = $script:networkMutations
    $result.blockedDownloadRequests = $script:downloadAttempts
    $result.nativeDialogs = $script:nativeDialogs
    $result | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')
}

function Screenshot([string]$name) {
    $shot = Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
    $result.screenshots += $name
}

function Guard {
    Assert-Browser 'window.__onlineDeliveryTest.blockedMutations===0 && window.__onlineDeliveryTest.downloadAttempts===0 && window.__onlineDeliveryTest.nativeAttempts===0' 'Unexpected browser mutation, download or native dialog attempt'
    if ($script:blockedMutations -or $script:networkMutations -or $script:downloadAttempts -or $script:nativeDialogs) {
        throw 'Network mutation, download or native dialog observed'
    }
}

function Check-Viewport([string]$name,[int]$width,[int]$height) {
    $script:stage = "$name buyer session and order table"
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=$height;deviceScaleFactor=1;mobile=($name -eq 'mobile')} | Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"} | Out-Null
    WaitJs '!!document.querySelector(".sidebar") && !!window.__onlineDeliveryTest.order && !!window.__onlineDeliveryTest.session && !!window.__onlineDeliveryTest.enterprises'
    Assert-Browser @'
(() => {const t=window.__onlineDeliveryTest,o=t.order,u=t.session,e=t.enterprises.find(x=>x.id===o.buyer_enterprise_id);
const member=u.memberships.find(x=>x.enterprise_id===o.buyer_enterprise_id&&(x.status||x.membership_status)==='active');
return o.order_no===t.expectedOrderNo&&o.snapshot_version===1&&o.delivery_method==='file'&&
o.payment_status==='paid'&&o.main_status==='completed'&&o.delivery_status==='accepted'&&
!u.platformRole&&u.verifiedStatus==='verified'&&u.active!==false&&(!u.activationStatus||u.activationStatus==='active')&&
e?.verification_status==='verified'&&['super_admin','enterprise_admin','admin'].includes(member?.role);})()
'@ 'Fixture is not a completed paid file order for a verified buyer administrator'
    WaitJs '(()=>{const t=window.__onlineDeliveryTest,n=[...document.querySelectorAll(".order-no")].find(n=>n.textContent.trim()===t.expectedOrderNo);return !!n?.closest("tr").querySelectorAll("td")[1]?.querySelector("button")})()'
    Assert-Browser @'
(() => {const t=window.__onlineDeliveryTest,n=[...document.querySelectorAll('.order-no')].filter(n=>n.textContent.trim()===t.expectedOrderNo);
if(n.length!==1)return false;const row=n[0].closest('tr'),table=row.closest('table'),cell=row.querySelectorAll('td')[1];
return table.querySelectorAll('thead th')[1]?.textContent.trim()==='\u5e94\u7528\u8bbf\u95ee\u5730\u5740'&&
cell.querySelector('button')?.textContent.trim()==='\u4e0b\u8f7d\u6587\u4ef6'&&!cell.querySelector('button').disabled&&!cell.querySelector('a[href]');})()
'@ 'Second column or exact order download entry differs'
    Js @'
(() => {const n=[...document.querySelectorAll('.order-no')].find(n=>n.textContent.trim()===window.__onlineDeliveryTest.expectedOrderNo);
const row=n.closest('tr');row.scrollIntoView({block:'center'});row.querySelectorAll('td')[1].scrollIntoView({block:'nearest',inline:'center'});return true;})()
'@ | Out-Null
    Screenshot "online-delivery-orders-$name.png"
    $script:stage = "$name real delivery access modal"
    Js @'
(() => {const n=[...document.querySelectorAll('.order-no')].find(n=>n.textContent.trim()===window.__onlineDeliveryTest.expectedOrderNo);
n.closest('tr').querySelectorAll('td')[1].querySelector('button').click();return true;})()
'@ | Out-Null
    WaitJs '!!document.querySelector("[aria-labelledby=delivery-access-title]") && !!window.__onlineDeliveryTest.access && !document.querySelector("[aria-labelledby=delivery-access-title] [role=status]")'
    Assert-Browser @'
(() => {const t=window.__onlineDeliveryTest,a=t.access,m=document.querySelector('[aria-labelledby=delivery-access-title]');
const rows=[...m.querySelectorAll('tbody tr')];
return a.status===200&&a.authenticated&&a.order_id===t.order.id&&a.method==='file'&&a.ready===true&&a.files.length>0&&
t.accessReads===1&&m.getAttribute('role')==='dialog'&&m.getAttribute('aria-modal')==='true'&&
m.querySelector('.drawer-head p')?.textContent.trim()===t.expectedOrderNo&&!m.querySelector('a[href]')&&!m.querySelector('[role=alert]')&&
rows.length===a.files.length&&a.files.every((f,i)=>{const cells=rows[i].querySelectorAll('td'),b=cells[4]?.querySelector('button');
return cells[0]?.textContent.trim()===f.name&&cells[1]?.textContent.trim()===(f.download_limit===0?'\u4e0d\u9650\u5236':String(f.download_limit))&&
cells[2]?.textContent.trim()===String(f.used_downloads)&&cells[3]?.textContent.trim()===(f.download_limit===0?'\u4e0d\u9650\u5236':String(f.remaining_downloads))&&
b?.textContent.trim()==='\u4e0b\u8f7d'&&b.disabled===(!f.validUrl||(f.download_limit!==0&&f.remaining_downloads===0));});})()
'@ 'Real delivery-access response, file names, download limits or counts differ from modal'
    $bounds = Js @'
(() => {const m=document.querySelector('[aria-labelledby=delivery-access-title]'),r=m.getBoundingClientRect(),style=getComputedStyle(m);
const controls=[...m.querySelectorAll('.drawer-head button,.modal-actions button')].map(n=>n.getBoundingClientRect());
const table=m.querySelector('.table-wrap'),downloads=[...m.querySelectorAll('tbody tr td:last-child button')];
return {viewportWidth:innerWidth,viewportHeight:innerHeight,visible:!!m.offsetWidth&&!!m.offsetHeight,
left:r.left,top:r.top,right:r.right,bottom:r.bottom,overflow:m.scrollWidth>m.clientWidth+1,
tableScrollWidth:table?.scrollWidth,tableClientWidth:table?.clientWidth,downloadButtonCount:downloads.length,
downloadButtonsOutside:downloads.some(n=>{const b=n.getBoundingClientRect(),s=getComputedStyle(n),t=table.getBoundingClientRect();
return !n.offsetWidth||!n.offsetHeight||s.visibility!=='visible'||s.display==='none'||
b.left<r.left-1||b.right>r.right+1||b.top<r.top-1||b.bottom>r.bottom+1||
b.left<t.left-1||b.right>t.right+1||b.top<t.top-1||b.bottom>t.bottom+1;}),
controlsOutside:controls.some(b=>b.left<r.left-1||b.right>r.right+1||b.top<r.top-1||b.bottom>r.bottom+1),
styled:style.backgroundColor!=='rgba(0, 0, 0, 0)'&&parseFloat(style.paddingLeft)>0};})()
'@
    if (-not $bounds.visible -or -not $bounds.styled -or $bounds.left -lt -1 -or $bounds.top -lt -1 -or
        $bounds.right -gt $width+1 -or $bounds.bottom -gt $height+1 -or $bounds.overflow -or $bounds.controlsOutside) {
        throw 'File modal or main controls exceed viewport bounds'
    }
    if ($name -eq 'desktop') {
        $script:stage = 'desktop full table and download button visibility'
        if ($null -eq $bounds.tableScrollWidth -or $null -eq $bounds.tableClientWidth -or
            $bounds.tableScrollWidth -gt $bounds.tableClientWidth+1 -or
            $bounds.downloadButtonCount -lt 1 -or $bounds.downloadButtonsOutside) {
            throw 'Desktop delivery table must fit without horizontal scrolling and every download button must be visible inside the modal'
        }
        $result.checks += 'Desktop table scrollWidth <= clientWidth + 1; every download button fully inside modal and table viewport'
    }
    Screenshot "online-delivery-files-$name.png"
    if ($name -eq 'mobile') {
        Js @'
(() => {const m=document.querySelector('[aria-labelledby=delivery-access-title]'),n=m.querySelector('.table-wrap');
const cell=m.querySelector('tbody tr').querySelectorAll('td')[2];
n.scrollLeft+=cell.getBoundingClientRect().left-n.getBoundingClientRect().left-12;return true;})()
'@ | Out-Null
        Screenshot 'online-delivery-counts-mobile.png'
        Js '(()=>{const n=document.querySelector("[aria-labelledby=delivery-access-title] .table-wrap");n.scrollLeft=n.scrollWidth;return true})()' | Out-Null
        Screenshot 'online-delivery-download-mobile.png'
    }
    if ($VerifyBackendAccess) {
        $script:stage = "$name additional authenticated GET delivery-access"
        Assert-Browser @'
(async()=>{const t=window.__onlineDeliveryTest,expected=t.access;
const r=await fetch('/api/orders/'+encodeURIComponent(t.order.id)+'/delivery-access',{headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}});
if(r.status!==200)return false;const a=await r.json();return a.order_id===expected.order_id&&a.method==='file'&&a.ready===true&&
JSON.stringify(a.files.map(({id,name,download_limit,used_downloads,remaining_downloads})=>({id,name,download_limit,used_downloads,remaining_downloads})))===
JSON.stringify(expected.files.map(({id,name,download_limit,used_downloads,remaining_downloads})=>({id,name,download_limit,used_downloads,remaining_downloads})));})()
'@ 'Additional real backend GET failed or download counts changed'
    }
    $sample = Js @'
(() => {const t=window.__onlineDeliveryTest;return {orderId:t.order.id,orderNo:t.expectedOrderNo,accessStatus:t.access.status,
authenticated:t.access.authenticated,files:t.access.files.map(({id,name,download_limit,used_downloads,remaining_downloads})=>({id,name,download_limit,used_downloads,remaining_downloads})),
assets:performance.getEntriesByType('resource').map(r=>new URL(r.name).pathname).filter(p=>p.startsWith('/assets/')&&p.endsWith('.js'))};})()
'@
    $counts = $sample.files | ConvertTo-Json -Depth 8 -Compress
    if ($script:baselineCounts -and $script:baselineCounts -cne $counts) { throw 'File counts changed between desktop and mobile' }
    $script:baselineCounts = $counts
    Guard
    $result.samples += @{viewport=$name;width=$width;height=$height;status='passed';modal=$bounds;backend=$sample;additionalBackendGet=[bool]$VerifyBackendAccess}
    $result.checks += "$name exact second column/order entry; real authenticated access GET; file names and per-file quotas; modal bounds; no download or mutation"
    Js 'document.querySelector("[aria-labelledby=delivery-access-title] .drawer-head button").click();true' | Out-Null
    WaitJs '!document.querySelector("[aria-labelledby=delivery-access-title]")'
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
    if ([string]::IsNullOrWhiteSpace($secret)) { $secret = [string]$fixture.token }
    if ([string]::IsNullOrWhiteSpace($secret) -or [string]::IsNullOrWhiteSpace($ExpectedOrderNo) -or
        [string]::IsNullOrWhiteSpace($DeploymentTag) -or [string]::IsNullOrWhiteSpace($BaseUrl)) {
        throw 'Fixture requires buyerAdminToken (or token), expectedOrderNo, deploymentTag and baseUrl'
    }
    $site = [Uri]$BaseUrl
    if ($site.Scheme -notin @('http','https') -or $site.UserInfo -or $site.Query -or $site.Fragment -or $site.AbsolutePath -ne '/') {
        throw 'BaseUrl must be an HTTP(S) origin'
    }
    $BaseUrl = $site.GetLeftPart([UriPartial]::Authority)
    $tokenJson = $secret | ConvertTo-Json -Compress
    $secret = $null
    $fixture = $null
    $result.baseUrl = $BaseUrl
    $result.expectedOrderNo = $ExpectedOrderNo
    $result.deploymentTag = $DeploymentTag
    $orderNoJson = $ExpectedOrderNo | ConvertTo-Json -Compress
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $listener.Start(); $port = $listener.LocalEndpoint.Port; $listener.Stop()
    $stage = 'Chrome startup'
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
    try { $null = $socket.ConnectAsync([Uri]$target.webSocketDebuggerUrl,$timeout.Token).GetAwaiter().GetResult() }
    finally { $timeout.Dispose() }
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Network.enable' @{} | Out-Null
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})} | Out-Null
    Cdp 'Browser.setDownloadBehavior' @{behavior='deny'} | Out-Null
    $instrumentation = @'
(() => {
const t=window.__onlineDeliveryTest={expectedOrderNo:__ORDER_NO__,order:null,session:null,enterprises:null,access:null,accessReads:0,blockedMutations:0,downloadAttempts:0,nativeAttempts:0};
const path=url=>new URL(url,location.origin).pathname;
const allowed=(method,url)=>{if(/^\/api\/files\/[^/]+\/download\/?$/.test(path(url))){t.downloadAttempts++;return false;}
if(!['GET','HEAD','OPTIONS'].includes(String(method).toUpperCase())){t.blockedMutations++;return false;}return true;};
const observe=(url,status,data,authenticated)=>{const p=path(url);
if(p==='/api/auth/me'&&status===200)t.session={platformRole:data.user?.platform_role,verifiedStatus:data.user?.verified_status,
active:data.user?.is_active,activationStatus:data.user?.activation_status,memberships:data.user?.enterprise_memberships||[]};
if(p==='/api/storefront/buyer-enterprises'&&status===200)t.enterprises=data.items;
if(p==='/api/orders'&&status===200){const matches=data.items?.filter(o=>o.order_no===t.expectedOrderNo)||[];
if(matches.length===1){const o=matches[0];t.order={id:o.id,order_no:o.order_no,buyer_enterprise_id:o.buyer_enterprise_id,snapshot_version:o.snapshot_version,
delivery_method:o.delivery_method,payment_status:o.payment_status,main_status:o.main_status,delivery_status:o.delivery_status};}}
if(t.order&&p==='/api/orders/'+encodeURIComponent(t.order.id)+'/delivery-access'){
t.accessReads++;t.access={status,authenticated,order_id:data.order_id,method:data.method,ready:data.ready,
files:(data.files||[]).map(f=>({id:f.id,name:f.name,download_limit:f.download_limit,used_downloads:f.used_downloads,remaining_downloads:f.remaining_downloads,
validUrl:typeof f.url==='string'&&f.url.startsWith('/api/files/'+encodeURIComponent(f.id)+'/download?')&&
new URL(f.url,location.origin).searchParams.size===1&&new URL(f.url,location.origin).searchParams.get('order_id')===t.order.id&&!f.url.includes('#')}))};}};
const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send,header=XMLHttpRequest.prototype.setRequestHeader;
XMLHttpRequest.prototype.open=function(method,url,...rest){this.qaMethod=method;this.qaUrl=url;this.qaAuthenticated=false;return open.call(this,method,url,...rest)};
XMLHttpRequest.prototype.setRequestHeader=function(name,value){if(String(name).toLowerCase()==='authorization')this.qaAuthenticated=value==='Bearer '+localStorage.getItem('market_token');return header.call(this,name,value)};
XMLHttpRequest.prototype.send=function(body){if(!allowed(this.qaMethod,this.qaUrl))throw Error('Request blocked locally');
this.addEventListener('load',()=>{try{observe(this.qaUrl,this.status,JSON.parse(this.responseText),this.qaAuthenticated)}catch{}});return send.call(this,body)};
const originalFetch=window.fetch;window.fetch=function(input,init){const req=new Request(input,init);
if(!allowed(req.method,req.url))return Promise.reject(Error('Request blocked locally'));
const authenticated=req.headers.get('authorization')==='Bearer '+localStorage.getItem('market_token');
return originalFetch.call(this,input,init).then(r=>{if(['/api/auth/me','/api/storefront/buyer-enterprises','/api/orders'].includes(path(req.url))||path(req.url).endsWith('/delivery-access'))
r.clone().json().then(d=>observe(req.url,r.status,d,authenticated)).catch(()=>{});return r})};
navigator.sendBeacon=()=>{t.blockedMutations++;return false};
for(const name of ['alert','confirm','prompt'])window[name]=()=>{t.nativeAttempts++;throw Error('Native dialog blocked')};
})();
'@
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$instrumentation.Replace('__ORDER_NO__',$orderNoJson)} | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=900;deviceScaleFactor=1;mobile=$false} | Out-Null
    $stage = 'private buyer session injection'
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"} | Out-Null
    WaitJs '!!document.querySelector(".login-shell")'
    Js "localStorage.setItem('market_token',$tokenJson);true" | Out-Null
    $tokenJson = $null
    foreach ($viewport in @(@{name='desktop';width=1440;height=900},@{name='mobile';width=390;height=844})) {
        try { Check-Viewport $viewport.name $viewport.width $viewport.height }
        catch {
            $result.failures += @{viewport=$viewport.name;stage=$stage;reason=$_.Exception.Message}
            try { Screenshot "online-delivery-failed-$($viewport.name).png" } catch { }
            Save-Result
        }
    }
    if ($result.failures.Count) { throw 'Viewport acceptance failed' }
    $result.status = 'passed'
    $result.passed = $true
    Save-Result
    Write-Output ($result | ConvertTo-Json -Depth 20)
} catch {
    $result.status = 'failed'
    $result.failedStage = $stage
    Save-Result
    throw "Online delivery acceptance failed at $stage; details suppressed; inspect browser-results.json"
} finally {
    $fixture = $null; $secret = $null; $tokenJson = $null
    if ($socket) { try { Js 'localStorage.removeItem("market_token");true' | Out-Null } catch { }; $socket.Dispose() }
    if ($chrome) {
        Get-CimInstance Win32_Process | Where-Object {$_.Name -eq 'chrome.exe' -and $_.CommandLine -like "*$profile*"} |
            ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}
    }
    if (Test-Path -LiteralPath $profile) { Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue }
}
