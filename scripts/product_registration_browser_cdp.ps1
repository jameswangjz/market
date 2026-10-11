param(
    [string]$BaseUrl = 'http://192.168.10.10:30080',
    [Parameter(Mandatory=$true)][string]$FixtureFile,
    [Parameter(Mandatory=$true)][string]$DeploymentTag,
    [string]$ExpectedConsoleAsset = 'ConsoleApp-qlJEGKDY.js',
    [string]$OutputDir = "$PSScriptRoot\..\docs\product-registration-browser-20261011"
)
$ErrorActionPreference = 'Stop'
$socket = $null
$chrome = $null
$tokenJson = $null
$profile = Join-Path $env:TEMP ('market-qa-reg-' + [Guid]::NewGuid().ToString('N'))
$script:commandId = 0
$script:cdpBlockedMutations = 0
$script:networkMutations = 0
$script:nativeDialogs = 0
$stage = 'fixture validation'
$result = [ordered]@{status='pending';mode='read-only';deploymentTag=$DeploymentTag;checks=@();screenshots=@();samples=@();failures=@();
    limits=@('Real isolated provider admin and two saved API versions; no real-user data',
    'Verify POST is rejected locally; successful verification is NOT asserted',
    'OAuth permission reads status/attachment metadata only; no credential body/artifact',
    'No uploads or registration save; FE004 compatibility/OIDC callback remains partial')}
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
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
    $result.screenshots += $name
}
function Guard([int]$expected) {
    Assert-Browser "window.__registrationTest.blocked.length===$expected && window.__registrationTest.nativeAttempts===0" 'Unexpected local mutation attempt or dialog'
    if ($script:cdpBlockedMutations -or $script:networkMutations -or $script:nativeDialogs) { throw 'Network mutation or native dialog observed' }
}
function Layout([string]$selector) {
    $q = $selector | ConvertTo-Json -Compress
    $bounds = Js @"
(() => {const n=document.querySelector($q),r=n.getBoundingClientRect();
const controls=[...n.querySelectorAll('input,select,textarea,button,a')].filter(x=>x.offsetWidth&&x.offsetHeight);
const boxes=controls.map(x=>x.getBoundingClientRect());
return {width:innerWidth,left:r.left,right:r.right,visible:!!n.offsetWidth,
overflow:n.scrollWidth>n.clientWidth+1,outside:boxes.some(b=>b.left<r.left-1||b.right>r.right+1),
overlap:boxes.some((a,i)=>boxes.slice(i+1).some(b=>Math.min(a.right,b.right)-Math.max(a.left,b.left)>2&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>2))};})()
"@
    if (-not $bounds.visible -or $bounds.left -lt -1 -or $bounds.right -gt $bounds.width+1 -or $bounds.overflow -or $bounds.outside -or $bounds.overlap) { throw 'Form/panel overflow or overlapping controls' }
    return $bounds
}
function Check-Viewport([int]$width,[int]$height) {
    $script:stage = "$width provider session"
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=$height;deviceScaleFactor=1;mobile=($width -eq 390)} | Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=products"} | Out-Null
    WaitJs '!!window.__registrationTest.session && !!document.querySelector(".product-link")'
    $assets = Js 'performance.getEntriesByType("resource").map(x=>new URL(x.name).pathname).filter(p=>p.startsWith("/assets/")&&p.endsWith(".js"))'
    if (-not ($assets | Where-Object {$_ -eq "/assets/$ExpectedConsoleAsset"})) { throw 'Served ConsoleApp asset differs from deployment' }
    $result.assets = $assets
    Assert-Browser '(()=>{const t=window.__registrationTest,s=t.session;return s.userId===t.expected.user_id&&!s.platformRole&&s.enterpriseId===t.expected.enterprise_id&&s.role==="super_admin"&&s.membershipStatus==="active"})()' 'Wrong authenticated provider enterprise/admin context'
    Guard 0
    $script:stage = "$width draft registration"
    Js '[...document.querySelectorAll("button")].find(b=>b.textContent.trim()==="\u65b0\u5efa\u4ea7\u54c1").click();true' | Out-Null
    WaitJs '!!document.querySelector(".product-modal .version-row")'
    Screenshot "registration-form-$width.png"
    Assert-Browser @'
(() => {const f=document.querySelector('.product-modal'),labels=[...f.querySelectorAll('label')];
const delivery=labels.find(l=>l.textContent.trim().startsWith('\u4ea4\u4ed8\u65b9\u5f0f')).querySelector('select');
return [...delivery.querySelectorAll('optgroup')].map(g=>[g.label,[...g.querySelectorAll('option')].map(o=>o.value)])
 .every((g,i)=>g[0]===['\u7ebf\u4e0a\u4ea4\u4ed8','\u7ebf\u4e0b\u4ea4\u4ed8'][i]&&JSON.stringify(g[1])===JSON.stringify([['file','api','model_api','tenant_access'],['training','consulting','custom']][i]))&&delivery.querySelectorAll('optgroup').length===2;})()
'@ 'Delivery online/offline groups differ'
    Js '[...document.querySelectorAll(".version-editor button")].find(b=>b.textContent.trim()==="\u65b0\u589e\u7248\u672c").click();true' | Out-Null
    Assert-Browser @'
(() => {const rows=[...document.querySelectorAll('.version-row')];return rows.length===2&&rows.every(r=>{
const labels=[...r.querySelectorAll('label')],required=s=>labels.find(l=>l.textContent.trim().startsWith(s))?.querySelector('input,textarea')?.required;
return ['\u7248\u672c\u53f7','\u7248\u672c\u4ef7\u683c','\u7248\u672c\u6210\u672c','\u7248\u672c\u7b80\u8981\u4ecb\u7ecd'].every(required)&&r.querySelector('input[type=file]')?.required;});})()
'@ 'Every file version must require code/price/cost/description/archive'
    $fileBounds = Layout '.product-modal'
    Js 'document.querySelector(".version-editor").scrollIntoView({block:"start"});true' | Out-Null
    Screenshot "registration-file-versions-$width.png"
    Assert-Browser '[...document.querySelectorAll(".version-compatibility select")].length===2 && [...document.querySelectorAll(".version-compatibility select")].every(n=>n.value==="compatible")' 'Version compatibility defaults unavailable'
    Js @'
(() => {const labels=[...document.querySelectorAll('.product-modal label')];
for(const [label,value] of [['\u4ea7\u54c1\u7c7b\u578b','saas'],['\u4ea4\u4ed8\u65b9\u5f0f','tenant_access']]){
const n=labels.find(l=>l.textContent.trim().startsWith(label)).querySelector('select');n.value=value;n.dispatchEvent(new Event('change',{bubbles:true}));}return true;})()
'@ | Out-Null
    WaitJs '!!document.querySelector(".oidc-redirect-field textarea")'
    Assert-Browser @'
(() => {const n=document.querySelector('.oidc-redirect-field textarea');
n.value='http://evil.example/callback';n.dispatchEvent(new Event('input',{bubbles:true}));
const rejected=!n.checkValidity();n.value='https://saas.example/callback\nhttp://market-mock-saas/callback';
n.dispatchEvent(new Event('input',{bubbles:true}));return rejected&&n.checkValidity();})()
'@ 'SaaS callback validation did not reject insecure host or accept whitelist'
    Js 'document.querySelector(".oidc-redirect-field").scrollIntoView({block:"center"});true' | Out-Null
    Layout '.product-modal' | Out-Null
    Screenshot "registration-saas-callbacks-$width.png"
    Js @'
(() => {const labels=[...document.querySelectorAll('.product-modal label')];
for(const [label,value] of [['\u4ea7\u54c1\u7c7b\u578b','api'],['\u4ea4\u4ed8\u65b9\u5f0f','api']]){
const n=labels.find(l=>l.textContent.trim().startsWith(label)).querySelector('select');n.value=value;n.dispatchEvent(new Event('change',{bubbles:true}));}return true;})()
'@ | Out-Null
    WaitJs 'document.querySelectorAll(".version-row input[type=url]").length===2'
    Assert-Browser @'
(() => {const required=['\u7248\u672c\u4e0a\u6e38URL','\u8def\u7531\u6807\u8bc6','\u5165\u53e3\u8ba4\u8bc1','\u4e0a\u6e38\u8ba4\u8bc1','\u4e0a\u6e38Scope','\u8d85\u65f6','\u5065\u5eb7\u68c0\u67e5\u8def\u5f84','\u5065\u5eb7\u68c0\u67e5\u65b9\u6cd5','\u6bcf\u5206\u949f\u9650\u6d41','\u6bcf\u65e5\u914d\u989d','\u6bcf\u8ba2\u9605\u6708\u914d\u989d'];
return [...document.querySelectorAll('.version-row')].every(r=>required.every(s=>[...r.querySelectorAll('label')].find(l=>l.textContent.trim().startsWith(s))?.querySelector('input,select')?.required)&&!!r.querySelector('input[type=checkbox]'));})()
'@ 'Every API version must contain required config and quota fields'
    $apiBounds = Layout '.product-modal'
    Js @'
(() => {const t=window.__registrationTest,rows=[...document.querySelectorAll('.version-row')];
const set=(root,label,value)=>{const n=[...root.querySelectorAll('label')].find(l=>l.textContent.trim().startsWith(label)).querySelector('input,textarea');n.value=value;n.dispatchEvent(new Event('input',{bubbles:true}));};
set(document.querySelector('.product-modal'),'\u4ea7\u54c1\u6216\u670d\u52a1\u540d\u79f0',t.expected.prefix+'-draft');
rows.forEach((r,i)=>{const v=t.expected.versions[i];for(const [label,value] of [['\u7248\u672c\u7b80\u8981\u4ecb\u7ecd',t.expected.prefix+' draft '+(i+1)],['\u7248\u672c\u4ef7\u683c',12*(i+1)],['\u7248\u672c\u6210\u672c',7*(i+1)],['\u7248\u672c\u4e0a\u6e38URL',v.upstream_url],['\u8def\u7531\u6807\u8bc6',v.route_key+'-draft']])set(r,label,value);});return true;})()
'@ | Out-Null
    Screenshot "registration-api-versions-$width.png"
    Js 'document.querySelector(".product-modal .drawer-head button").click();true' | Out-Null
    WaitJs '!document.querySelector(".product-modal")'
    Guard 0
    $script:stage = "$width saved product integration"
    Js '[...document.querySelectorAll(".product-link")].find(n=>n.textContent.trim()===window.__registrationTest.expected.product_name).click();true' | Out-Null
    WaitJs '!!document.querySelector(".product-integration-panel") && document.querySelectorAll(".integration-row").length===2 && Object.keys(window.__registrationTest.integrations).length===2'
    Assert-Browser @'
(() => {const t=window.__registrationTest,rows=[...document.querySelectorAll('.integration-row')];return t.expected.versions.every(v=>{
const data=t.integrations[v.id],row=rows.find(r=>r.querySelector('strong')?.textContent===v.version_code);
return data?.status===200&&data.item?.version_id===v.id&&data.route?.product_id===t.expected.product_id&&data.item.status==='pending_integration'&&!data.item.healthy&&['upstream_url','route_key','version','auth_mode','upstream_auth_mode','upstream_scope','timeout_ms','strip_prefix','health_path','health_method','rate_limit_per_minute','daily_quota','monthly_quota'].every(k=>data.route[k]===v[k])&&row?.textContent.includes(v.route_key)&&row.textContent.includes(v.health_path)&&row.textContent.includes('\u5f85\u63a5\u5165\u9a8c\u8bc1');})&&[...document.querySelectorAll('.integration-actions button')].some(b=>b.textContent.trim()==='\u4e0b\u8f7d\u63a5\u5165\u51ed\u636e'&&!b.disabled);})()
'@ 'Saved version identity/config/status or provider download eligibility mismatch'
    Assert-Browser @'
(() => {const t=window.__registrationTest,rows=[...document.querySelectorAll('.product-modal .version-row')];
const value=(r,label)=>[...r.querySelectorAll('label')].find(l=>l.textContent.trim().startsWith(label))?.querySelector('input,select,textarea')?.value;
return rows.length===2&&t.expected.versions.every((v,i)=>{const r=rows[i];return value(r,'\u7248\u672c\u53f7')===v.version_code&&Number(value(r,'\u7248\u672c\u4ef7\u683c'))===12*(i+1)&&Number(value(r,'\u7248\u672c\u6210\u672c'))===7*(i+1)&&value(r,'\u7248\u672c\u7b80\u8981\u4ecb\u7ecd')===t.expected.prefix+' isolated version '+(i+1)&&Number(value(r,'\u6bcf\u5206\u949f\u9650\u6d41'))===v.rate_limit_per_minute&&Number(value(r,'\u6bcf\u65e5\u914d\u989d'))===v.daily_quota&&Number(value(r,'\u6bcf\u8ba2\u9605\u6708\u914d\u989d'))===v.monthly_quota/1000&&value(r,'\u7248\u672c\u4e0a\u6e38URL')===v.upstream_url&&value(r,'\u8def\u7531\u6807\u8bc6')===v.route_key;});})()
'@ 'Saved registration version fields or monthly quota units differ from fixture'
    Js 'document.querySelector(".product-integration-panel").scrollIntoView({block:"center"});true' | Out-Null
    $panelBounds = Layout '.product-integration-panel'
    Screenshot "productdetail-integration-$width.png"
    $script:stage = "$width credential permission metadata"
    $permission = Js @'
(async()=>{const p='/api/products/'+window.__registrationTest.expected.product_id+'/oauth-credentials-download';
const r=await fetch(p,{headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}});
const result={status:r.status,attachment:(r.headers.get('content-disposition')||'').startsWith('attachment;')};
if(r.body)await r.body.cancel();return result;})()
'@
    if ($permission.status -ne 200 -or -not $permission.attachment) { throw 'Pending integration credential download permission denied' }
    $script:stage = "$width locally blocked verify"
    Js 'document.querySelector(".integration-row button").click();true' | Out-Null
    WaitJs 'window.__registrationTest.blocked.length===1 && !!document.querySelector(".integration-row [role=alert]")'
    Assert-Browser '(()=>{const t=window.__registrationTest,b=t.blocked[0];return b.method==="POST"&&b.path==="/api/products/"+t.expected.product_id+"/versions/"+t.expected.versions[0].id+"/integration/verify"&&Object.values(t.integrations).every(x=>x.item.status==="pending_integration"&&!x.item.healthy)})()' 'Blocked verification endpoint or retained status mismatch'
    Guard 1
    Screenshot "productdetail-verify-blocked-$width.png"
    $result.samples += @{viewport=$width;fileForm=$fileBounds;apiForm=$apiBounds;panel=$panelBounds;permission=$permission;blockedVerify=1;status='passed'}
    $result.checks += "$width registration fields/groups and layout; real saved integration GETs; credential permission metadata; locally rejected verify POST"
}

try {
    New-Item -ItemType Directory -Force $OutputDir | Out-Null
    $repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')) + [IO.Path]::DirectorySeparatorChar
    if ([IO.Path]::GetFullPath($FixtureFile).StartsWith($repoRoot,[StringComparison]::OrdinalIgnoreCase)) { throw 'Private fixture token file must be outside repository' }
    $site = [Uri]$BaseUrl
    if ($site.Scheme -notin @('http','https') -or $site.UserInfo -or $site.Query -or $site.Fragment -or $site.AbsolutePath -ne '/') { throw 'BaseUrl must be an HTTP(S) origin' }
    $BaseUrl = $site.GetLeftPart([UriPartial]::Authority)
    $fixture = Get-Content -LiteralPath $FixtureFile -Raw | ConvertFrom-Json
    if ($fixture.prefix -notmatch '^qa-reg-[0-9a-f]{12}$' -or $fixture.product_id -ne ($fixture.prefix+'-p') -or $fixture.user_id -ne ($fixture.prefix+'-u') -or $fixture.enterprise_id -ne ($fixture.prefix+'-e') -or $fixture.versions.Count -ne 2 -or [string]::IsNullOrWhiteSpace($fixture.token)) { throw 'Invalid isolated fixture' }
    $tokenJson = [string]$fixture.token | ConvertTo-Json -Compress
    $expected = $fixture | Select-Object prefix,product_id,product_name,user_id,enterprise_id,versions | ConvertTo-Json -Depth 10 -Compress
    $fixture = $null
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $listener.Start(); $port = $listener.LocalEndpoint.Port; $listener.Stop()
    $stage = 'Chrome startup'
    $chrome = Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList @('--headless=new',"--remote-debugging-port=$port","--user-data-dir=`"$profile`"",'--no-first-run','about:blank') -PassThru
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
    $stage = 'CDP page enable'
    Cdp 'Page.enable' @{} | Out-Null
    $stage = 'CDP network enable'
    Cdp 'Network.enable' @{} | Out-Null
    $stage = 'CDP mutation guard'
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})} | Out-Null
    $stage = 'CDP download guard'
    Cdp 'Browser.setDownloadBehavior' @{behavior='deny'} | Out-Null
    $instrumentation = @'
(() => {
const t=window.__registrationTest={expected:__EXPECTED__,session:null,integrations:{},blocked:[],nativeAttempts:0};
const path=url=>new URL(url,location.origin).pathname;
const allowed=(method,url)=>{method=String(method).toUpperCase();if(['GET','HEAD','OPTIONS'].includes(method))return true;t.blocked.push({method,path:path(url)});return false;};
const observe=(url,status,data)=>{const p=path(url);if(p==='/api/auth/me'&&status===200)t.session={userId:data.user?.id,platformRole:data.user?.platform_role,enterpriseId:data.enterprise?.id,role:data.role,membershipStatus:data.membership_status};
for(const v of t.expected.versions)if(p==='/api/products/'+t.expected.product_id+'/versions/'+v.id+'/integration')t.integrations[v.id]={status,item:data.item,route:data.route};};
const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open=function(method,url,...rest){this.qaMethod=method;this.qaUrl=url;return open.call(this,method,url,...rest)};
XMLHttpRequest.prototype.send=function(body){if(!allowed(this.qaMethod,this.qaUrl))throw Error('Mutation blocked locally');this.addEventListener('load',()=>{if(path(this.qaUrl).endsWith('/oauth-credentials-download'))return;try{observe(this.qaUrl,this.status,JSON.parse(this.responseText))}catch{}});return send.call(this,body)};
const fetch=window.fetch;window.fetch=function(input,init){const req=new Request(input,init);if(!allowed(req.method,req.url))return Promise.reject(Error('Mutation blocked locally'));return fetch.call(this,input,init).then(r=>{const p=path(req.url);if(p==='/api/auth/me'||p.startsWith('/api/products/'+t.expected.product_id+'/versions/'))r.clone().json().then(d=>observe(req.url,r.status,d)).catch(()=>{});return r})};
navigator.sendBeacon=url=>{t.blocked.push({method:'POST',path:path(url)});return false};
for(const name of ['alert','confirm','prompt'])window[name]=()=>{t.nativeAttempts++;throw Error('Native dialog blocked')};
})();
'@
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$instrumentation.Replace('__EXPECTED__',$expected)} | Out-Null
    $stage = 'private session injection'
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=products"} | Out-Null
    WaitJs '!!document.querySelector(".login-shell")'
    Js "localStorage.setItem('market_token',$tokenJson);true" | Out-Null
    $tokenJson = $null
    foreach ($viewport in @(@{width=1440;height=1000},@{width=390;height=844})) {
        try { Check-Viewport $viewport.width $viewport.height }
        catch { $result.failures += @{viewport=$viewport.width;stage=$stage;reason=$_.Exception.Message}; Save-Result }
    }
    if ($result.failures.Count) { throw 'Viewport acceptance failed' }
    $result.status = 'passed'
    Save-Result
    Write-Output 'Read-only acceptance passed; verification POST was blocked locally'
} catch {
    $result.status = 'failed'; $result.failedStage = $stage
    if ($stage -eq 'Chrome startup' -or $stage -eq 'CDP connection') { $result.startupError = $_.Exception.Message }
    Save-Result
    throw "Registration acceptance failed at $stage; exception details suppressed"
} finally {
    $fixture = $null; $tokenJson = $null
    if ($socket) { try { Js 'localStorage.removeItem("market_token");true' | Out-Null } catch { }; $socket.Dispose() }
    if ($chrome) { Get-CimInstance Win32_Process | Where-Object {$_.Name -eq 'chrome.exe' -and $_.CommandLine -like "*$profile*"} | ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue} }
    if (Test-Path -LiteralPath $profile) { Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue }
}
