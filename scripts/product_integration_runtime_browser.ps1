param(
    [string]$BaseUrl = 'http://192.168.10.10:30080',
    [Parameter(Mandatory=$true)][string]$FixtureFile,
    [Parameter(Mandatory=$true)][string]$DeploymentTag,
    [Parameter(Mandatory=$true)][string]$ExpectedConsoleAsset,
    [Parameter(Mandatory=$true)][string]$OutputDir
)
$ErrorActionPreference = 'Stop'
$socket = $null
$chrome = $null
$profile = Join-Path $env:TEMP ('market-product-integration-' + [Guid]::NewGuid().ToString('N'))
$script:commandId = 0
$script:blockedMutations = 0
$script:networkMutations = 0
$script:allowedVerifies = @{}
$script:allowedPaths = @{}
$stage = 'fixture validation'
$result = [ordered]@{status='pending';deploymentTag=$DeploymentTag;checks=@();screenshots=@();failures=@();
    scope='Private metadata fixture; not a complete four-stage product review regression'}

function Send-Cdp([string]$method, $parameters) {
    $script:commandId++
    $payload = @{id=$script:commandId;method=$method;params=$parameters}|ConvertTo-Json -Depth 30 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $timeout = [Threading.CancellationTokenSource]::new(30000)
    try {$null=$socket.SendAsync([ArraySegment[byte]]::new($bytes),[Net.WebSockets.WebSocketMessageType]::Text,$true,$timeout.Token).GetAwaiter().GetResult()}
    finally {$timeout.Dispose()}
    return $script:commandId
}

function Cdp([string]$method, $parameters) {
    $id=Send-Cdp $method $parameters
    $timeout=[Threading.CancellationTokenSource]::new(30000)
    try {
        while($true) {
            $memory=[IO.MemoryStream]::new()
            try {
                do {$buffer=New-Object byte[] 65536;$part=$socket.ReceiveAsync([ArraySegment[byte]]::new($buffer),$timeout.Token).GetAwaiter().GetResult();
                    if($part.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close){throw 'CDP closed'};$memory.Write($buffer,0,$part.Count)
                } while(-not $part.EndOfMessage)
                $answer=[Text.Encoding]::UTF8.GetString($memory.ToArray())|ConvertFrom-Json
            } finally {$memory.Dispose()}
            if($answer.method -eq 'Fetch.requestPaused') {
                $request=$answer.params.request
                $path=([Uri]$request.url).AbsolutePath
                $allowed=$false
                if($request.method -in @('GET','HEAD','OPTIONS')) {$allowed=$true}
                elseif($request.method -eq 'POST' -and $script:allowedPaths.ContainsKey($path)) {
                    if(-not $script:allowedVerifies.ContainsKey($path)) {$script:allowedVerifies[$path]=0}
                    if($script:allowedVerifies[$path] -eq 0) {$script:allowedVerifies[$path]++;$allowed=$true}
                }
                if($allowed){Send-Cdp 'Fetch.continueRequest' @{requestId=$answer.params.requestId}|Out-Null}
                else {$script:blockedMutations++;Send-Cdp 'Fetch.failRequest' @{requestId=$answer.params.requestId;errorReason='BlockedByClient'}|Out-Null}
            } elseif($answer.method -eq 'Network.requestWillBeSent') {
                if($answer.params.request.method -notin @('GET','HEAD','OPTIONS')) {$script:networkMutations++}
            } elseif($answer.method -eq 'Page.javascriptDialogOpening') {
                Send-Cdp 'Page.handleJavaScriptDialog' @{accept=$false}|Out-Null
                throw 'Unexpected native dialog'
            }
            if($answer.id -eq $id){if($answer.error){throw 'CDP command failed'};return $answer.result}
        }
    } finally {$timeout.Dispose()}
}

function Js([string]$expression) {
    $response=Cdp 'Runtime.evaluate' @{expression=$expression;awaitPromise=$true;returnByValue=$true}
    if($response.exceptionDetails){throw 'Browser evaluation failed'}
    return $response.result.value
}
function WaitJs([string]$expression) {
    for($i=0;$i -lt 120;$i++){if(Js $expression){return};Start-Sleep -Milliseconds 250}
    throw 'Browser condition timed out'
}
function Assert-Browser([string]$expression,[string]$message) {if(-not (Js $expression)){throw $message}}
function Screenshot([string]$name) {
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
    $result.screenshots+=$name
}
function Save-Result {$result|ConvertTo-Json -Depth 12|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')}

try {
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    if(@(Get-ChildItem -LiteralPath $OutputDir -Force).Count){throw 'Use a fresh output directory'}
    $site=[Uri]$BaseUrl
    if($site.Scheme -notin @('http','https') -or $site.UserInfo -or $site.Query -or $site.Fragment -or $site.AbsolutePath -ne '/') {throw 'BaseUrl must be an HTTP(S) origin'}
    $BaseUrl=$site.GetLeftPart([UriPartial]::Authority)
    $repoRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))+[IO.Path]::DirectorySeparatorChar
    if([IO.Path]::GetFullPath($FixtureFile).StartsWith($repoRoot,[StringComparison]::OrdinalIgnoreCase)){throw 'Private fixture token file must be outside the repository'}
    $fixture=Get-Content -LiteralPath $FixtureFile -Raw|ConvertFrom-Json
    if($fixture.prefix -notmatch '^qa-reg-[0-9a-f]{12}$' -or -not $fixture.token -or
       $fixture.product_id -ne ($fixture.prefix+'-p') -or $fixture.versions.Count -ne 2 -or
       $fixture.versions[0].health_path -ne '/health-missing' -or $fixture.versions[1].health_path -ne '/health') {throw 'Invalid private integration fixture'}
    foreach($version in $fixture.versions){$script:allowedPaths["/api/products/$($fixture.product_id)/versions/$($version.id)/integration/verify"]=$false}
    $expected=$fixture|Select-Object prefix,product_id,product_name,user_id,enterprise_id,versions|ConvertTo-Json -Depth 10 -Compress
    $tokenJson=[string]$fixture.token|ConvertTo-Json -Compress
    $fixture=$null

    $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0);$listener.Start();$port=$listener.LocalEndpoint.Port;$listener.Stop()
    $chrome=Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList @('--headless=new',"--remote-debugging-port=$port",("--user-data-dir=`"{0}`"" -f $profile),'--no-first-run','about:blank') -PassThru
    $target=$null
    for($i=0;$i -lt 60;$i++){try{$pages=Invoke-RestMethod "http://127.0.0.1:$port/json/list";$target=$pages|Where-Object {$_.type -eq 'page'}|Select-Object -First 1;if($target){break}}catch{};Start-Sleep -Milliseconds 500}
    if(-not $target){throw 'Chrome startup timed out'}
    $socket=[Net.WebSockets.ClientWebSocket]::new();$connectTimeout=[Threading.CancellationTokenSource]::new(30000)
    try{$null=$socket.ConnectAsync([Uri]$target.webSocketDebuggerUrl,$connectTimeout.Token).GetAwaiter().GetResult()}finally{$connectTimeout.Dispose()}
    Cdp 'Page.enable' @{}|Out-Null;Cdp 'Runtime.enable' @{}|Out-Null;Cdp 'Network.enable' @{}|Out-Null
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})}|Out-Null
    Cdp 'Browser.setDownloadBehavior' @{behavior='deny'}|Out-Null
    $source=@'
(() => {
const t=window.__productIntegrationQA={expected:__EXPECTED__,session:null,integrations:{},verifies:[],publicProduct:null,credentialDownloads:[],blocked:[],posts:{},native:0};
const path=u=>new URL(u,location.origin).pathname;
const credentialPath=p=>/^\/api\/orders\/[^/]+\/api-credentials\/[^/]+\/download$/.test(p);
const safeHeaders=h=>{const out={};for(const k of ['content-type','content-disposition','cache-control','pragma']){const v=h.get(k);if(v)out[k]=v}return out};
const allowed=(method,url)=>{method=String(method).toUpperCase();const p=path(url);if(['GET','HEAD','OPTIONS'].includes(method))return true;
const v=t.expected.versions.find(x=>p==='/api/products/'+t.expected.product_id+'/versions/'+x.id+'/integration/verify');
if(method==='POST'&&v&&!t.posts[v.id]){t.posts[v.id]=1;return true;}t.blocked.push({method,path:p});return false;};
const observe=(url,status,data)=>{const p=path(url);if(p==='/api/auth/me'&&status===200)t.session={userId:data.user?.id,platformRole:data.user?.platform_role,enterpriseId:data.enterprise?.id,role:data.role,membershipStatus:data.membership_status};if(p==='/api/storefront/products/'+t.expected.product_id)t.publicProduct={httpStatus:status,item:data};
for(const v of t.expected.versions){const base='/api/products/'+t.expected.product_id+'/versions/'+v.id+'/integration';if(p===base)t.integrations[v.id]={httpStatus:status,item:data.item,route:data.route};if(p===base+'/verify')t.verifies.push({versionId:v.id,httpStatus:status,status:data.status});}};
const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open=function(method,url,...rest){this.qaMethod=method;this.qaUrl=url;return open.call(this,method,url,...rest)};
XMLHttpRequest.prototype.send=function(body){if(!allowed(this.qaMethod,this.qaUrl))throw Error('Mutation blocked locally');this.addEventListener('load',()=>{try{if(credentialPath(path(this.qaUrl))){const h=new Headers();for(const line of this.getAllResponseHeaders().trim().split(/[\r\n]+/)){const i=line.indexOf(':');if(i>0)h.set(line.slice(0,i).trim(),line.slice(i+1).trim())}t.credentialDownloads.push({status:this.status,headers:safeHeaders(h),bodyRead:false});return}observe(this.qaUrl,this.status,JSON.parse(this.responseText))}catch{}});return send.call(this,body)};
const fetch=window.fetch;window.fetch=function(input,init){const req=new Request(input,init);if(!allowed(req.method,req.url))return Promise.reject(Error('Mutation blocked locally'));return fetch.call(this,input,init).then(r=>{if(credentialPath(path(req.url)))t.credentialDownloads.push({status:r.status,headers:safeHeaders(r.headers),bodyRead:false});else r.clone().json().then(d=>observe(req.url,r.status,d)).catch(()=>{});return r})};
navigator.sendBeacon=url=>{t.blocked.push({method:'POST',path:path(url)});return false};for(const name of ['alert','confirm','prompt'])window[name]=()=>{t.native++;throw Error('Native dialog blocked')};
})();
'@
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$source.Replace('__EXPECTED__',$expected)}|Out-Null
    $stage='login and asset validation'
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=products"}|Out-Null
    WaitJs '!!document.querySelector(".login-shell")'
    Js "localStorage.setItem('market_token',$tokenJson);true"|Out-Null;$tokenJson=$null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=products"}|Out-Null
    WaitJs '!!window.__productIntegrationQA.session && !!document.querySelector(".product-link")'
    $assets=Js 'performance.getEntriesByType("resource").map(x=>new URL(x.name).pathname).filter(p=>p.startsWith("/assets/")&&p.endsWith(".js"))'
    if(-not ($assets|Where-Object {$_ -eq "/assets/$ExpectedConsoleAsset"})){throw 'Served ConsoleApp asset differs from deployment'}
    Assert-Browser '(()=>{const s=window.__productIntegrationQA.session,e=window.__productIntegrationQA.expected;return s.userId===e.user_id&&!s.platformRole&&s.enterpriseId===e.enterprise_id&&s.role==="super_admin"&&s.membershipStatus==="active"})()' 'Authenticated fixture owner mismatch'
    $result.assets=$assets
    $stage='product integration panel'
    Js '[...document.querySelectorAll(".product-link")].find(n=>n.textContent.trim()===window.__productIntegrationQA.expected.product_name).click();true'|Out-Null
    WaitJs '!!document.querySelector(".product-integration-panel") && document.querySelectorAll(".integration-row").length===2'
    Assert-Browser '(()=>{const t=window.__productIntegrationQA;return t.expected.versions.every(v=>{const x=t.integrations[v.id];return x?.httpStatus===200&&x.item?.version_id===v.id&&x.item.status==="pending_integration"&&x.route?.product_id===t.expected.product_id&&x.route?.health_path===v.health_path})})()' 'Initial integration GET identity/state mismatch'
    $failureIdJs=([string]$expected|ConvertFrom-Json).versions[0].id|ConvertTo-Json -Compress
    Js "(()=>{const v=$failureIdJs,r=[...document.querySelectorAll('.integration-row')].find(x=>x.textContent.includes(window.__productIntegrationQA.expected.versions.find(y=>y.id===v).version_code));r.querySelector('button').click();return true})()"|Out-Null
    WaitJs "(()=>{const v=$failureIdJs,t=window.__productIntegrationQA,r=[...document.querySelectorAll('.integration-row')].find(x=>x.textContent.includes(t.expected.versions.find(y=>y.id===v).version_code));return r?.querySelector('.status-pill')?.textContent.includes('\u9a8c\u8bc1\u5931\u8d25')&&r.textContent.includes('integration_failed')&&t.integrations[v]?.item?.status==='failed'&&t.integrations[v]?.item?.health_status_code===404})()"
    $stage='healthy release verification'
    $successIdJs=([string]$expected|ConvertFrom-Json).versions[1].id|ConvertTo-Json -Compress
    Js "(()=>{const v=$successIdJs,t=window.__productIntegrationQA,r=[...document.querySelectorAll('.integration-row')].find(x=>x.textContent.includes(t.expected.versions.find(y=>y.id===v).version_code));r.querySelector('button').click();return true})()"|Out-Null
    WaitJs "(()=>{const v=$successIdJs,t=window.__productIntegrationQA,r=[...document.querySelectorAll('.integration-row')].find(x=>x.textContent.includes(t.expected.versions.find(y=>y.id===v).version_code));return r?.querySelector('.status-pill')?.textContent.includes('\u9a8c\u8bc1\u901a\u8fc7')&&r.textContent.includes('HTTP 200')&&t.integrations[v]?.item?.status==='verified'&&t.integrations[v]?.item?.healthy===true})()"
    Assert-Browser "(()=>{const t=window.__productIntegrationQA,a=t.expected.versions[0],b=t.expected.versions[1];return t.verifies.length===2&&t.verifies.filter(x=>x.httpStatus===200&&x.status==='failed').length===1&&t.verifies.filter(x=>x.httpStatus===200&&x.status==='verified').length===1&&t.integrations[a.id].item.health_status_code===404&&t.integrations[b.id].item.health_status_code===200&&t.blocked.length===0})()" 'Verify outcomes, real health status codes, or mutation allowlist mismatch'
    $publicProduct=Js "(async()=>{const t=window.__productIntegrationQA,r=await fetch('/api/storefront/products/'+encodeURIComponent(t.expected.product_id));const item=await r.json();t.publicProduct={httpStatus:r.status,item};return r.status===200&&item.id===t.expected.product_id&&item.versions?.length===1&&item.versions[0].id===t.expected.versions[1].id&&item.versions[0].version_code==='v2'})()"
    if(-not $publicProduct){throw 'Public storefront did not expose exactly the verified v2'}
    Assert-Browser "(()=>{const t=window.__productIntegrationQA,v=t.expected.versions[1],x=t.integrations[v.id];return x.route?.status==='active'&&x.route?.route_key===v.route_key&&x.route?.id===t.expected.versions[1].route_id})()" 'Verified release route is not active or does not match fixture route identity'
    Js 'document.querySelector(".product-integration-panel").scrollIntoView({block:"center"});true'|Out-Null
    Screenshot 'integration-desktop-1440.png'
    $stage='mobile integration panel'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=1;mobile=$true}|Out-Null
    Assert-Browser '(()=>{const p=document.querySelector(".product-integration-panel"),r=p.getBoundingClientRect();return r.width>0&&r.left>=-1&&r.right<=innerWidth+1&&p.scrollWidth<=p.clientWidth+1&&document.querySelectorAll(".integration-row").length===2})()' 'Mobile integration panel overflow or missing rows'
    Screenshot 'integration-mobile-390.png'
    Assert-Browser "(()=>{const t=window.__productIntegrationQA;return t.verifies.length===2&&t.blocked.length===0&&t.expected.versions.every(v=>!![...document.querySelectorAll('.integration-row')].find(r=>r.textContent.includes(v.version_code)))})()" 'Unexpected mobile mutation or missing version row'
    if($script:blockedMutations -ne 0 -or $script:networkMutations -ne 2){throw 'Unexpected mutation observed outside the two verification POSTs'}
    $result.checks=@('Exact fixture owner and deployed ConsoleApp asset','Real pending integration GET identity','404 health verification renders failed state','/health HTTP 200 verification publishes and renders verified state','Storefront exposes exactly v2 as purchasable','Verified v2 has active persisted gateway route evidence','390px mobile final-state layout')
    $result.coverageGaps=@('No buyer order/enterprise fixture: credential download endpoint was not invoked; observer records status and allowlisted headers only and never reads body','SaaS integration configuration download is outside this API-product fixture')
    $result.status='passed';Save-Result
} catch {
    $result.status='failed';$result.failedStage=$stage
    try{$result.browser=Js '(()=>{const t=window.__productIntegrationQA;return {panel:!!document.querySelector(".product-integration-panel"),rows:document.querySelectorAll(".integration-row").length,verifies:t?.verifies,blocked:t?.blocked,session:!!t?.session}})()'}catch{}
    Save-Result
    throw "Product integration browser acceptance failed at $stage; details recorded without credentials"
} finally {
    $tokenJson=$null;$fixture=$null
    if($socket){try{Js 'localStorage.removeItem("market_token");true'|Out-Null}catch{};$socket.Dispose()}
    if($chrome){Get-CimInstance Win32_Process|Where-Object {$_.Name -eq 'chrome.exe' -and $_.CommandLine -like "*$profile*"}|ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}}
    if(Test-Path -LiteralPath $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
}
