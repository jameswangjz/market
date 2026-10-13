param(
    [Parameter(Mandatory=$true)][string]$FixtureFile,
    [string]$OutputDir = "$env:TEMP\market-oidc-browser",
    [string]$ChromePath = 'C:\Program Files\Google\Chrome\Application\chrome.exe',
    [string]$ProxyVm = '192.168.10.10',
    [int]$ProxyVmPort = 30083,
    [switch]$NoProxy,
    [switch]$RestoreOwnedGrant
)
$ErrorActionPreference = 'Stop'
$socket = $null; $chrome = $null; $fixture = $null
$relay = $null; $script:outputReady = $false
$profile = Join-Path $env:TEMP ('market-oidc-' + [Guid]::NewGuid().ToString('N'))
$script:commandId = 0; $script:blocked = 0; $script:stage = 'fixture'
$script:consentIds = [Collections.Generic.HashSet[string]]::new()
$script:checks = @(); $script:screenshots = @(); $script:responses = @{}
$script:screenshotHashes = @{}
$script:blockedDetails = @()
$result = [ordered]@{status='failed';database='real PostgreSQL';browser_sso='pending';mock_session='pending';application_access='pending';negative_cases='pending';
    limits=@('Paid and delivered terms are synthetic scoped fixtures', 'Mobile uses Chromium device emulation', 'No payment, settlement or external SaaS delivery verification')}
$script:startup = [ordered]@{chrome_path_exists=$false;process_started=$false;cdp_http_seen=$false;
    target_count=0;page_target_seen=$false;websocket_connected=$false;cdp_initialized=$false}
$script:flow = [ordered]@{rp_entry_status=0;op_authorize_status=0;platform_login_status=0;
    consent_details_status=0;consent_decision_status=0;rp_callback_status=0;rp_app_status=0;
    login_form_seen=$false;login_form_submitted=$false;connection_refused=0;connection_reset=0}
$script:relayEvidence = [ordered]@{owned=(-not [bool]$NoProxy);vm_health_status=0;
    loopback_listener_seen=$false;loopback_tcp_connected=$false;loopback_health_status=0;service_verified=$false}

function Resolve-PathValue([string]$value) {
    if ($value.StartsWith('/')) {
        $p = & wsl.exe --exec wslpath -w $value 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $p) { throw 'Path conversion failed' }
        return [string]($p | Select-Object -Last 1)
    }
    return [IO.Path]::GetFullPath($value)
}

function Allowed($request) {
    try {
        $u = [Uri]$request.url; $method = $request.method
        $authority = $u.GetLeftPart([UriPartial]::Authority)
        if ($u.UserInfo -or $u.Fragment -or $authority -cnotin @($fixture.baseUrl,$fixture.rpPublic)) { return $false }
        if ($method -ceq 'GET') {
            if ($authority -ceq $fixture.rpPublic) {
                return $u.AbsolutePath -ceq '/app' -or $u.AbsolutePath -ceq '/sso/callback' -or
                    $u.AbsolutePath -ceq ('/sso/apps/' + $fixture.clientId) -or $u.AbsolutePath -ceq '/favicon.ico'
            }
            if ($u.AbsolutePath -match '^/api/oidc/browser/([a-f0-9]{64})$') {
                return $script:consentIds.Contains($Matches[1])
            }
            return $u.AbsolutePath -match '^/(assets/|console(?:/|$)|oidc/authorize$|favicon.ico$)' -or
                $u.AbsolutePath -cin @('/api/auth/me','/api/enterprises','/api/applications','/api/storefront/buyer-enterprises',
                    '/api/notifications/summary','/api/notifications/stream','/api/notifications/sender-context',
                    '/api/dashboard','/api/products','/api/orders','/api/development/tasks') -or
                $u.AbsolutePath -ceq ('/api/applications/' + $fixture.subscriptionId + '/members') -or
                $u.AbsolutePath -ceq '/api/settings/application-address-policy'
        }
        if ($method -cne 'POST' -or $authority -cne $fixture.baseUrl -or $u.Query) { return $false }
        if ($u.AbsolutePath -ceq '/api/auth/login') {
            $body = $request.postData | ConvertFrom-Json
            return @($body.PSObject.Properties.Name).Count -eq 2 -and
                $body.email -cin @($fixture.memberLogin,$fixture.adminLogin) -and $body.password -ceq $fixture.password
        }
        if ($u.AbsolutePath -match '^/api/oidc/browser/([a-f0-9]{64})/(approve|reject)$') {
            return $script:consentIds.Contains($Matches[1]) -and -not $request.postData
        }
        $prefix = '/api/applications/' + $fixture.subscriptionId
        if ($u.AbsolutePath -ceq ($prefix + '/launch')) { return -not $request.postData }
        if ($u.AbsolutePath -ceq ($prefix + '/grants')) {
            $body = $request.postData | ConvertFrom-Json
            return @($body.PSObject.Properties.Name).Count -eq 2 -and $body.user_id -ceq $fixture.memberUserId -and
                $body.granted -is [bool]
        }
    } catch { }
    return $false
}

function Send-Cdp([string]$method,$parameters) {
    $script:commandId++
    $bytes = [Text.Encoding]::UTF8.GetBytes((@{id=$script:commandId;method=$method;params=$parameters} | ConvertTo-Json -Depth 25 -Compress))
    $timeout = [Threading.CancellationTokenSource]::new(30000)
    try { $null = $socket.SendAsync([ArraySegment[byte]]::new($bytes),[Net.WebSockets.WebSocketMessageType]::Text,$true,$timeout.Token).GetAwaiter().GetResult() }
    finally { $timeout.Dispose() }
    return $script:commandId
}

function Cdp([string]$method,$parameters) {
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
                # Learn only requests arriving through the OP's console redirect.
                $u = [Uri]$request.url
                if ($u.GetLeftPart([UriPartial]::Authority) -ceq $fixture.baseUrl -and
                    $u.AbsolutePath -ceq '/console/sso' -and $u.Query -cmatch '^\?request=([a-f0-9]{64})$') {
                    $null = $script:consentIds.Add($Matches[1])
                }
                if (Allowed $request) { Send-Cdp 'Fetch.continueRequest' @{requestId=$answer.params.requestId} | Out-Null }
                else {
                    $script:blocked++
                    $safePath = '<redacted-path>'
                    if ($u.AbsolutePath -cmatch '^/(?:[A-Za-z_-]{1,32}/)*[A-Za-z_.-]{1,48}$') { $safePath = $u.AbsolutePath }
                    $originKind = 'other'
                    if ($u.GetLeftPart([UriPartial]::Authority) -ceq $fixture.baseUrl) { $originKind = 'OP' }
                    elseif ($u.GetLeftPart([UriPartial]::Authority) -ceq $fixture.rpPublic) { $originKind = 'RP' }
                    $script:blockedDetails += @{origin=$originKind;path=$safePath;method=($request.method -replace '[^A-Z]','')}
                    Send-Cdp 'Fetch.failRequest' @{requestId=$answer.params.requestId;errorReason='BlockedByClient'} | Out-Null
                }
            } elseif ($answer.method -eq 'Network.responseReceived') {
                $u = [Uri]$answer.params.response.url
                $status = [int]$answer.params.response.status
                if ($u.AbsolutePath -ceq ('/sso/apps/' + $fixture.clientId)) { $script:flow.rp_entry_status = $status }
                elseif ($u.AbsolutePath -ceq '/oidc/authorize') { $script:flow.op_authorize_status = $status }
                elseif ($u.AbsolutePath -ceq '/api/auth/login') { $script:flow.platform_login_status = $status }
                elseif ($u.AbsolutePath -match '^/api/oidc/browser/[a-f0-9]{64}$') { $script:flow.consent_details_status = $status }
                elseif ($u.AbsolutePath -match '^/api/oidc/browser/[a-f0-9]{64}/(approve|reject)$') { $script:flow.consent_decision_status = $status }
                elseif ($u.AbsolutePath -ceq '/sso/callback') { $script:flow.rp_callback_status = $status }
                elseif ($u.AbsolutePath -ceq '/app') { $script:flow.rp_app_status = $status }
                # Persist only path/status for expected RP/consent endpoints, never query or headers.
                if ($u.AbsolutePath -match '^/(sso/callback|app|api/oidc/browser/[a-f0-9]{64}/(approve|reject))$') {
                    $script:responses[$u.AbsolutePath] = [int]$answer.params.response.status
                }
            } elseif ($answer.method -eq 'Network.loadingFailed') {
                if ($answer.params.errorText -ceq 'net::ERR_CONNECTION_REFUSED') { $script:flow.connection_refused++ }
                elseif ($answer.params.errorText -ceq 'net::ERR_CONNECTION_RESET') { $script:flow.connection_reset++ }
            } elseif ($answer.method -eq 'Page.javascriptDialogOpening') {
                $script:blocked++; Send-Cdp 'Page.handleJavaScriptDialog' @{accept=$false} | Out-Null
            }
            if ($answer.id -eq $id) {
                if ($answer.error) { throw 'CDP command failed' }
                return $answer.result
            }
        }
    } finally { $timeout.Dispose() }
}

function Js([string]$expression) {
    $r = Cdp 'Runtime.evaluate' @{expression=$expression;awaitPromise=$true;returnByValue=$true}
    if ($r.exceptionDetails) { throw 'Browser evaluation failed' }
    return $r.result.value
}
function WaitJs([string]$expression) {
    for ($i=0;$i -lt 100;$i++) { if (Js $expression) { return }; Start-Sleep -Milliseconds 250 }
    throw 'Browser condition timed out'
}
function AssertJs([string]$expression,[string]$message) { if (-not (Js $expression)) { throw $message } }
function Q($value) { return ConvertTo-Json -InputObject $value -Compress -Depth 10 }
function Navigate([string]$url) { Cdp 'Page.navigate' @{url=$url} | Out-Null }
function Click([string]$selector) {
    $s = Q $selector
    WaitJs "!!document.querySelector($s)&&!document.querySelector($s).disabled"
    Js "document.querySelector($s).click();true" | Out-Null
}
function ClickText([string]$text) {
    $t = Q $text
    WaitJs "[...document.querySelectorAll('button')].some(b=>b.textContent.trim()===$t&&!b.disabled)"
    Js "[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===$t).click();true" | Out-Null
}
function Login([string]$login) {
    $previousStage = $script:stage
    $script:stage = $previousStage + ' / waiting for login form'
    WaitJs '!!document.querySelector(".login-shell input[autocomplete=username]")'
    $script:flow.login_form_seen = $true
    $script:stage = $previousStage + ' / submitting login form'
    $name = Q $login; $password = Q $fixture.password
    Js "(()=>{for(const [s,v] of [['input[autocomplete=username]',$name],['input[autocomplete=current-password]',$password]]){const e=document.querySelector(s);e.value=v;e.dispatchEvent(new Event('input',{bubbles:true}));}document.querySelector('.login-shell button[type=submit]').click();return true})()" | Out-Null
    $script:flow.login_form_submitted = $true
    $script:stage = $previousStage + ' / waiting after login'
}
function ClearSession {
    Cdp 'Network.clearBrowserCookies' @{} | Out-Null
    Cdp 'Storage.clearDataForOrigin' @{origin=$fixture.baseUrl;storageTypes='local_storage'} | Out-Null
}
function Shot([string]$name) {
    if ($script:blocked) { throw 'Browser network guard failed' }
    $secret = Q $fixture.password
    AssertJs "(()=>{const h=document.documentElement.outerHTML,t=localStorage.getItem('market_token');return !h.includes($secret)&&(!t||!h.includes(t))&&!document.querySelector('input[type=password]')&&document.documentElement.scrollWidth<=innerWidth+1})()" 'Screenshot privacy or viewport guard failed'
    $shot = Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
    $script:screenshots += $name
    $script:screenshotHashes[$name] = (Get-FileHash -LiteralPath (Join-Path $OutputDir $name) -Algorithm SHA256).Hash.ToLowerInvariant()
}
function CookieCheck([string]$name,[string]$url) {
    $cookies = (Cdp 'Network.getCookies' @{urls=@($url)}).cookies
    $c = @($cookies | Where-Object {$_.name -ceq $name})
    if ($c.Count -ne 1 -or -not $c[0].httpOnly -or $c[0].sameSite -cne 'Lax') { throw 'Cookie attributes incorrect' }
}

try {
    if ($FixtureFile.StartsWith('/')) {
        $mode = & wsl.exe --exec stat -c '%a' -- $FixtureFile 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]($mode | Select-Object -Last 1) -cne '600') { throw 'WSL credentials must be 0600' }
    } else {
        throw 'Use the original WSL absolute path to a 0600 fixture credential file'
    }
    $FixtureFile = Resolve-PathValue $FixtureFile; $OutputDir = Resolve-PathValue $OutputDir
    if (Test-Path $OutputDir) { throw 'Use a fresh output directory' }
    New-Item -ItemType Directory $OutputDir | Out-Null
    $script:outputReady = $true
    $fixture = Get-Content -LiteralPath $FixtureFile -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($field in @('baseUrl','rpPublic','deploymentTag','runId','productId','enterpriseId','otherEnterpriseId','subscriptionId','clientId','password','memberLogin','adminLogin','memberUserId','launchUrl')) {
        if (-not $fixture.$field) { throw 'Fixture field missing' }
    }
    if ($fixture.runId -cnotmatch '^qa-vcq-[a-f0-9]{12}$' -or $fixture.clientId -cnotmatch '^oidc_[a-f0-9]{32}$') { throw 'Fixture identity invalid' }
    foreach ($base in @($fixture.baseUrl,$fixture.rpPublic)) {
        $u = [Uri]$base
        if ($u.Scheme -notin @('http','https') -or $u.UserInfo -or $u.Query -or $u.Fragment -or $u.AbsolutePath -ne '/') { throw 'Invalid fixture origin' }
    }
    $expected = $fixture.rpPublic + '/sso/apps/' + $fixture.clientId + '?enterprise_id=' + $fixture.enterpriseId
    if ($fixture.launchUrl -cne $expected) { throw 'Launch URL does not match owned application' }
    $result.runtime_tag = $fixture.deploymentTag; $result.run_id = $fixture.runId
    $script:stage = 'Windows VM RP health preflight'
    $vmHealth = Invoke-WebRequest -Uri "http://${ProxyVm}:${ProxyVmPort}/health" -UseBasicParsing -TimeoutSec 5
    $script:relayEvidence.vm_health_status = [int]$vmHealth.StatusCode
    if ($vmHealth.StatusCode -ne 200 -or ($vmHealth.Content | ConvertFrom-Json).service -cne 'mock-saas') {
        throw 'VM RP health preflight failed'
    }
    if (-not $NoProxy) {
        if ($fixture.rpPublic -cne 'http://127.0.0.1:30082' -or $ProxyVm -cne '192.168.10.10' -or $ProxyVmPort -ne 30083) {
            throw 'Default relay requires the scoped loopback/VM origins'
        }
        $script:stage = 'owned loopback relay'
        if (-not ('MarketOidcRelay' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;
public sealed class MarketOidcRelay : IDisposable {
    readonly TcpListener listener;
    readonly ConcurrentBag<TcpClient> clients = new ConcurrentBag<TcpClient>();
    volatile bool stopped;
    public MarketOidcRelay(string host, int port) {
        listener = new TcpListener(IPAddress.Loopback, 30082);
        listener.Start();
        Task.Factory.StartNew(() => {
            while (!stopped) {
                TcpClient incoming;
                try { incoming = listener.AcceptTcpClient(); } catch { break; }
                clients.Add(incoming);
                Task.Factory.StartNew(() => {
                    using (incoming) using (var outgoing = new TcpClient()) {
                        clients.Add(outgoing);
                        try {
                            var connect = outgoing.ConnectAsync(host, port);
                            if (!connect.Wait(10000) || stopped) return;
                            incoming.NoDelay = outgoing.NoDelay = true;
                            var a = incoming.GetStream(); var b = outgoing.GetStream();
                            Task.WhenAny(a.CopyToAsync(b), b.CopyToAsync(a)).Wait();
                        } catch { }
                    }
                });
            }
        }, CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
    }
    public void Dispose() {
        stopped = true; listener.Stop();
        foreach (var client in clients) { try { client.Close(); } catch { } }
    }
}
'@
        }
        $relay = [MarketOidcRelay]::new($ProxyVm,$ProxyVmPort)
    }
    $script:stage = 'Windows RP loopback listener proof'
    $rpAddress = [Uri]$fixture.rpPublic
    $script:relayEvidence.loopback_listener_seen = @(
        [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
        Where-Object {$_.Port -eq $rpAddress.Port -and [Net.IPAddress]::IsLoopback($_.Address)}
    ).Count -gt 0
    if (-not $script:relayEvidence.loopback_listener_seen) { throw 'Windows RP loopback listener absent' }
    $probe = [Net.Sockets.TcpClient]::new()
    try {
        $connectProbe = $probe.ConnectAsync($rpAddress.Host,$rpAddress.Port)
        if (-not $connectProbe.Wait(3000)) { throw 'Windows RP loopback TCP probe timed out' }
        $script:relayEvidence.loopback_tcp_connected = $probe.Connected
    } finally { $probe.Dispose() }
    $script:stage = 'Windows RP loopback HTTP health proof'
    $localHealth = Invoke-WebRequest -Uri ($fixture.rpPublic + '/health') -UseBasicParsing -TimeoutSec 5
    $script:relayEvidence.loopback_health_status = [int]$localHealth.StatusCode
    $script:relayEvidence.service_verified = $localHealth.StatusCode -eq 200 -and
        ($localHealth.Content | ConvertFrom-Json).service -ceq 'mock-saas'
    if (-not $script:relayEvidence.service_verified) { throw 'Windows RP loopback health preflight failed' }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $listener.Start(); $port = $listener.LocalEndpoint.Port; $listener.Stop()
    $script:stage = 'Chrome executable validation'
    $script:startup.chrome_path_exists = Test-Path -LiteralPath $ChromePath -PathType Leaf
    if (-not $script:startup.chrome_path_exists) { throw 'Chrome executable missing' }
    $script:stage = 'Chrome process launch'
    $chrome = Start-Process $ChromePath -ArgumentList @('--headless=new','--no-proxy-server',"--remote-debugging-port=$port","--user-data-dir=`"$profile`"",'--no-first-run','about:blank') -PassThru
    $script:startup.process_started = $null -ne $chrome
    $script:stage = 'CDP page target discovery'
    $target = $null
    for ($i=0;$i -lt 60;$i++) {
        try {
            # Invoke-RestMethod can emit the entire JSON array as one pipeline item.
            $pages = Invoke-RestMethod "http://127.0.0.1:$port/json/list" -TimeoutSec 2
            $script:startup.cdp_http_seen = $true
            $script:startup.target_count = @($pages).Count
            $target = @($pages) | Where-Object {$_.type -ceq 'page' -and $_.webSocketDebuggerUrl -is [string]} | Select-Object -First 1
        } catch { $script:startup.discovery_exception_type = $_.Exception.GetType().Name }
        if ($target) { break }; Start-Sleep -Milliseconds 250
    }
    if (-not $target) { throw 'Chrome unavailable' }
    $script:startup.page_target_seen = $true
    $script:stage = 'CDP WebSocket endpoint validation'
    $endpoint = [Uri]([string]$target.webSocketDebuggerUrl)
    if ($endpoint.Scheme -cne 'ws' -or $endpoint.Host -cnotin @('127.0.0.1','localhost') -or
        $endpoint.Port -ne $port -or $endpoint.UserInfo -or $endpoint.Query -or $endpoint.Fragment -or
        -not $endpoint.AbsolutePath.StartsWith('/devtools/page/')) { throw 'Unexpected CDP endpoint' }
    $script:stage = 'CDP WebSocket connection'
    $socket = [Net.WebSockets.ClientWebSocket]::new()
    $timeout = [Threading.CancellationTokenSource]::new(30000)
    try { $null = $socket.ConnectAsync($endpoint,$timeout.Token).GetAwaiter().GetResult() } finally { $timeout.Dispose() }
    $script:startup.websocket_connected = $true
    foreach ($method in @('Page.enable','Network.enable')) { $script:stage = 'CDP initialization ' + $method; Cdp $method @{} | Out-Null }
    $script:stage = 'CDP initialization network settings'
    Cdp 'Network.setCacheDisabled' @{cacheDisabled=$true} | Out-Null
    Cdp 'Network.setBypassServiceWorker' @{bypass=$true} | Out-Null
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})} | Out-Null
    Cdp 'Browser.setDownloadBehavior' @{behavior='deny'} | Out-Null
    $script:startup.cdp_initialized = $true
    if ($RestoreOwnedGrant) {
        $script:stage = 'restore owned grant for repeat run'
        ClearSession
        Navigate ($fixture.baseUrl + '/console?view=applications')
        Login $fixture.adminLogin
        WaitJs '!!document.querySelector(".application-row")'
        Click '.application-row .application-actions button.secondary-btn'
        $member = Q $fixture.memberUserId
        WaitJs "[...document.querySelectorAll('.application-member')].some(r=>r.textContent.includes($member)&&r.querySelector('input')&&!r.querySelector('input').disabled)"
        Js "(()=>{const e=[...document.querySelectorAll('.application-member')].find(r=>r.textContent.includes($member)).querySelector('input');if(!e.checked)e.click();return true})()" | Out-Null
        WaitJs "[...document.querySelectorAll('.application-member')].some(r=>r.textContent.includes($member)&&r.querySelector('input')?.checked&&!r.querySelector('input').disabled)"
        $script:checks += 'owned_grant_restored_for_repeat'
    }
    foreach ($viewport in @(@{name='desktop';width=1440;height=1000;mobile=$false},@{name='mobile';width=390;height=844;mobile=$true})) {
        $script:stage = $viewport.name + ' login consent session'
        ClearSession
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$viewport.width;height=$viewport.height;deviceScaleFactor=1;mobile=$viewport.mobile} | Out-Null
        $script:stage = $viewport.name + ' RP entry navigation'
        Navigate $fixture.launchUrl
        Login $fixture.memberLogin
        $script:stage = $viewport.name + ' consent rendering'
        WaitJs '!!document.querySelector(".oidc-consent footer button.primary-btn")&&!!document.querySelector(".oidc-consent dl")'
        $script:stage = $viewport.name + ' OP flow cookie attributes'
        CookieCheck 'market_oidc_flow' ($fixture.baseUrl + '/api/oidc/browser')
        $script:stage = $viewport.name + ' RP flow cookie attributes'
        CookieCheck 'mock_saas_oidc_flow' ($fixture.rpPublic + '/sso/callback')
        $script:stage = $viewport.name + ' consent identity assertion'
        $productJs = Q $fixture.productId; $eid = Q $fixture.enterpriseId
        AssertJs "document.querySelector('.oidc-consent').textContent.includes($productJs)&&document.querySelector('.oidc-consent').textContent.includes($eid)" 'Consent identity mismatch'
        $script:stage = $viewport.name + ' consent screenshot privacy and capture'
        Shot ($viewport.name + '-consent.png')
        $script:stage = $viewport.name + ' consent approve'
        Click '.oidc-consent footer button.primary-btn'
        $script:stage = $viewport.name + ' RP session rendering'
        WaitJs "location.origin===$(Q $fixture.rpPublic)&&location.pathname==='/app'&&!!document.querySelector('main h1')"
        $script:stage = $viewport.name + ' RP session cookie attributes'
        CookieCheck 'mock_saas_session' ($fixture.rpPublic + '/app')
        $script:stage = $viewport.name + ' RP session identity assertion'
        $member = Q $fixture.memberUserId
        AssertJs "document.body.textContent.includes($eid)&&document.body.textContent.includes($member)&&document.body.textContent.includes($productJs)" 'RP session identity mismatch'
        if ($script:responses['/app'] -ne 200) { throw 'RP app response not successful' }
        $script:stage = $viewport.name + ' RP screenshot privacy and capture'
        Shot ($viewport.name + '-app.png')
        $script:checks += $viewport.name + '_login_consent_rp_session'
    }
    $script:stage = 'reject'
    ClearSession; Navigate $fixture.launchUrl; Login $fixture.memberLogin
    WaitJs '!!document.querySelector(".oidc-consent dl")'
    Click '.oidc-consent footer button.secondary-btn'
    WaitJs "location.origin===$(Q $fixture.rpPublic)&&location.pathname==='/sso/callback'"
    WaitJs 'document.readyState==="complete"&&document.body.textContent.includes("SSO")'
    if ($script:responses['/sso/callback'] -ne 400) { throw 'Rejection callback not denied' }
    Navigate ($fixture.rpPublic + '/app')
    WaitJs 'document.readyState==="complete"&&document.body.textContent.includes("expired")'
    if ($script:responses['/app'] -ne 401) { throw 'Rejected flow created session' }
    $script:checks += 'reject_no_rp_session'
    $script:stage = 'cross enterprise'
    ClearSession
    Navigate ($fixture.rpPublic + '/sso/apps/' + $fixture.clientId + '?enterprise_id=' + $fixture.otherEnterpriseId)
    Login $fixture.memberLogin
    WaitJs '!!document.querySelector(".oidc-consent [role=alert]")'
    AssertJs '!document.querySelector(".oidc-consent dl")' 'Cross-enterprise consent available'
    $script:checks += 'cross_enterprise_denied'
    $script:stage = 'applications UI'
    ClearSession; Navigate ($fixture.baseUrl + '/console?view=applications'); Login $fixture.adminLogin
    WaitJs '!!document.querySelector(".application-row")'
    Shot 'applications.png'
    Click '.application-row .application-actions button.secondary-btn'
    WaitJs '!!document.querySelector("[role=dialog] input[type=checkbox]")'
    $member = Q $fixture.memberUserId
    AssertJs "[...document.querySelectorAll('[role=dialog] .application-member')].some(r=>r.textContent.includes($member)&&r.querySelector('input')?.checked)" 'Owned granted member absent'
    $result.mobile_member_modal_layout = Js @'
(() => {const m=document.querySelector('.application-members-modal'),l=m.querySelector('.application-member'),
b=m.querySelector('input[type=checkbox]'),r=m.getBoundingClientRect(),q=b.getBoundingClientRect(),s=getComputedStyle(b);
return {viewport_width:innerWidth,viewport_height:innerHeight,label_direction:getComputedStyle(l).flexDirection,
modal_left:r.left,modal_top:r.top,modal_right:r.right,modal_bottom:r.bottom,
checkbox_width:q.width,checkbox_height:q.height,checkbox_left:q.left,checkbox_right:q.right,
checkbox_top:q.top,checkbox_bottom:q.bottom,padding_left:s.paddingLeft,padding_right:s.paddingRight,
padding_top:s.paddingTop,padding_bottom:s.paddingBottom,
checkbox_inside_modal:q.left>=r.left&&q.right<=r.right&&q.top>=r.top&&q.bottom<=r.bottom,
horizontal_overflow:m.scrollWidth>m.clientWidth+1};})()
'@
    Shot 'mobile-members.png'
    $layout = $result.mobile_member_modal_layout
    if ($layout.label_direction -cne 'row' -or $layout.checkbox_width -ne 18 -or $layout.checkbox_height -ne 18 -or
        $layout.padding_left -cne '0px' -or $layout.padding_right -cne '0px' -or
        $layout.padding_top -cne '0px' -or $layout.padding_bottom -cne '0px' -or
        -not $layout.checkbox_inside_modal -or $layout.horizontal_overflow) {
        throw 'Mobile member row or checkbox layout assertion failed'
    }
    $script:checks += 'mobile_member_row_layout'
    $script:checks += 'applications_listing_members_ui'
    $script:stage = 'member launch UI'
    ClearSession; Navigate ($fixture.baseUrl + '/console?view=applications'); Login $fixture.memberLogin
    WaitJs '!!document.querySelector(".application-row")'
    Click '.application-row .application-actions button.primary-btn'
    WaitJs '!!document.querySelector(".oidc-consent dl")'
    Click '.oidc-consent footer button.primary-btn'
    WaitJs "location.origin===$(Q $fixture.rpPublic)&&location.pathname==='/app'&&!!document.querySelector('main h1')"
    $script:checks += 'applications_launch_ui'
    $script:stage = 'revoke existing RP session'
    # Retain RP cookies while switching the OP login to the fixture administrator.
    Cdp 'Storage.clearDataForOrigin' @{origin=$fixture.baseUrl;storageTypes='local_storage'} | Out-Null
    Navigate ($fixture.baseUrl + '/console?view=applications')
    Login $fixture.adminLogin
    WaitJs '!!document.querySelector(".application-row")'
    Click '.application-row .application-actions button.secondary-btn'
    WaitJs "[...document.querySelectorAll('.application-member')].some(r=>r.textContent.includes($member)&&r.querySelector('input')?.checked)"
    Js "[...document.querySelectorAll('.application-member')].find(r=>r.textContent.includes($member)).querySelector('input').click();true" | Out-Null
    WaitJs "[...document.querySelectorAll('.application-member')].some(r=>r.textContent.includes($member)&&r.querySelector('input')&&!r.querySelector('input').checked&&!r.querySelector('input').disabled)"
    Navigate ($fixture.rpPublic + '/app')
    WaitJs 'document.readyState==="complete"&&document.body.textContent.includes("no longer")'
    if ($script:responses['/app'] -ne 401) { throw 'Revoked RP session accepted' }
    Cdp 'Storage.clearDataForOrigin' @{origin=$fixture.baseUrl;storageTypes='local_storage'} | Out-Null
    Navigate ($fixture.baseUrl + '/console?view=applications')
    Login $fixture.memberLogin
    WaitJs '!!document.querySelector(".applications-center")'
    WaitJs '!document.querySelector(".applications-center [role=status]")&&!document.querySelector(".application-row")'
    $script:checks += 'grant_revoke_rp_session_and_listing'
    if ($script:blocked) { throw 'Network guard blocked unexpected requests' }
    $result.status='passed'; $result.browser_sso='passed'; $result.mock_session='passed'
    $result.application_access='passed'; $result.negative_cases='passed'
} catch {
    $failure = $_
    $result.failed_stage = $script:stage
    $result.error = 'Browser acceptance failed; protocol details suppressed'
    $result.exception_type = $failure.Exception.GetType().Name
    if ($failure.Exception.Message -cin @('Browser condition timed out','Browser evaluation failed',
        'CDP command failed','Cookie attributes incorrect','Consent identity mismatch','RP session identity mismatch',
        'RP app response not successful','Screenshot privacy or viewport guard failed')) {
        $result.failure_reason = $failure.Exception.Message
    }
    if ($socket -and $script:startup.cdp_initialized) {
        try {
            $result.browser_state = Js @'
(() => ({op_origin:location.origin===__OP__,rp_origin:location.origin===__RP__,
console_path:location.pathname==='/console',consent_path:location.pathname==='/console/sso',
app_path:location.pathname==='/app',browser_error:location.protocol==='chrome-error:',
login_form:!!document.querySelector('.login-shell'),login_error:!!document.querySelector('.login-shell .error-text'),
consent_visible:!!document.querySelector('.oidc-consent dl'),consent_error:!!document.querySelector('.oidc-consent [role=alert]'),
login_input_count:document.querySelectorAll('.login-shell input[autocomplete=username]').length,
consent_details_count:document.querySelectorAll('.oidc-consent dl').length,
consent_approve_count:document.querySelectorAll('.oidc-consent footer button.primary-btn').length}))()
'@.Replace('__OP__',(Q $fixture.baseUrl)).Replace('__RP__',(Q $fixture.rpPublic))
        } catch { $result.browser_state_unavailable = $true }
    }
} finally {
    $result.checks = $script:checks; $result.screenshots = $script:screenshots; $result.blocked_requests = $script:blocked
    $result.screenshot_hashes = $script:screenshotHashes
    $result.startup = $script:startup
    $result.flow = $script:flow
    $result.relay = $script:relayEvidence
    $result.blocked_request_details = $script:blockedDetails
    if ($script:outputReady) { $result | ConvertTo-Json -Depth 15 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json') }
    if ($socket) { $socket.Dispose() }
    if ($chrome) { Stop-Process -Id $chrome.Id -Force -ErrorAction SilentlyContinue }
    if ($relay) { $relay.Dispose() }
    for ($i=0;$i -lt 10 -and (Test-Path $profile);$i++) { Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 200 }
    $fixture=$null
}
Write-Output ($result | ConvertTo-Json -Depth 15)
if ($result.status -ne 'passed') { exit 1 }
