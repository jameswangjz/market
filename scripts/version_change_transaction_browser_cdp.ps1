param(
    [Parameter(Mandatory=$true)][string]$FixtureFile,
    [string]$OutputDir = "$env:TEMP\market-version-change-transaction",
    [string]$BaseUrl,
    [string]$OrderNo,
    [string]$DeploymentTag
)
$ErrorActionPreference='Stop'
$socket=$null;$chrome=$null;$fixture=$null;$tokenJson=$null;$secret=$null
$profile=Join-Path $env:TEMP ('market-vct-'+[Guid]::NewGuid().ToString('N'))
$script:cdpId=0;$script:blocked=0;$script:unexpected=0;$script:posts=0;$script:stage='fixture validation';$script:deniedPaths=@()
$script:orderId=$null;$script:targetIds=@();$script:quoteToken=$null;$script:changeId=$null;$script:createdChangePending=$false
$result=[ordered]@{status='pending';passed=$false;mode='real console UI upgrade and downgrade';screenshots=@();checks=@();failures=@();limits=@(
    'Initial orders were paid and delivered by the fixture helper; browser initial purchase is not covered',
    'A directly issued QA buyer bearer is injected after /console loads; password login is not covered',
    'The browser uses visible quote, validation, submit, simulated payment and payment confirmation controls',
    'The supplied deployment tag is provenance only, not an asserted server image identity')}

function Send-Cdp([string]$method,$parameters) {
    $script:cdpId++;$cmd=$script:cdpId
    $bytes=[Text.Encoding]::UTF8.GetBytes((@{id=$cmd;method=$method;params=$parameters}|ConvertTo-Json -Depth 20 -Compress))
    $timeout=[Threading.CancellationTokenSource]::new(30000)
    try {$null=$socket.SendAsync([ArraySegment[byte]]::new($bytes),[Net.WebSockets.WebSocketMessageType]::Text,$true,$timeout.Token).GetAwaiter().GetResult()}
    finally {$timeout.Dispose()};return $cmd
}
function Send-Raw([string]$method,$parameters) {
    $bytes=[Text.Encoding]::UTF8.GetBytes((@{id=(++$script:cdpId);method=$method;params=$parameters}|ConvertTo-Json -Depth 15 -Compress))
    $timeout=[Threading.CancellationTokenSource]::new(10000)
    try {$null=$socket.SendAsync([ArraySegment[byte]]::new($bytes),[Net.WebSockets.WebSocketMessageType]::Text,$true,$timeout.Token).GetAwaiter().GetResult()}
    finally {$timeout.Dispose()}
}
function Allowed([string]$method,[string]$url,[string]$body) {
    try {
        $u=[Uri]$url
        if($u.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $u.UserInfo -or $u.Fragment){return $false}
        if($method -eq 'GET') {
            if($u.AbsolutePath -match '^/api/(files(?:/|$)|.+/download/?$|auth/(?:caller|sso|oauth|callback)(?:/|$)|products/.*/credentials(?:/|$)|providers?/.*/credentials(?:/|$))'){return $false}
            return $true
        }
        if($method -ne 'POST' -or -not $script:orderId -or $u.Query){return $false}
        $root='/api/orders/'+[Uri]::EscapeDataString($script:orderId)
        $data=$body|ConvertFrom-Json
        if($u.AbsolutePath -eq $root+'/version-change-quote') {
            return $data.target_version_id -is [string] -and $data.target_version_id -cin $script:targetIds -and
                $data.compatibility_confirmed -is [bool]
        }
        if($u.AbsolutePath -eq $root+'/version-change-quote/validate') {
            return ($data.PSObject.Properties.Name -join ',') -ceq 'quote_token' -and
                $data.quote_token -is [string] -and $data.quote_token -ceq $script:quoteToken
        }
        if($u.AbsolutePath -eq $root+'/version-changes') {
            $bodyKeys=(@($data.PSObject.Properties.Name | Sort-Object) -join ',')
            if($bodyKeys -cne 'idempotency_key,quote_token' -or
                $data.quote_token -isnot [string] -or $data.quote_token -cne $script:quoteToken -or
                $data.idempotency_key -isnot [string] -or $data.idempotency_key.Length -lt 8 -or
                $data.idempotency_key.Length -gt 200 -or $script:createdChangePending){return $false}
            $script:createdChangePending=$true
            return $true
        }
        if($u.AbsolutePath -match '^/api/version-changes/([A-Za-z0-9_-]{1,128})/(start-payment|confirm-payment)$') {
            $cid=$Matches[1];$action=$Matches[2]
            if(-not $script:createdChangePending -or -not $script:changeId -or $cid -cne $script:changeId){return $false}
            if($action -eq 'start-payment'){return $body -match '^\s*\{\s*\}\s*$'}
            $confirmation=$body|ConvertFrom-Json
            return ($confirmation.PSObject.Properties.Name -join ',') -ceq 'reason' -and
                $confirmation.reason -ceq 'qa-vcs browser upgrade simulated payment'
        }
    } catch {}
    return $false
}
function Cdp([string]$method,$parameters) {
    $cmd=Send-Cdp $method $parameters;$timeout=[Threading.CancellationTokenSource]::new(30000)
    try {
        while($true) {
            $mem=[IO.MemoryStream]::new()
            try {
                do {$buffer=New-Object byte[] 65536;$part=$socket.ReceiveAsync([ArraySegment[byte]]::new($buffer),$timeout.Token).GetAwaiter().GetResult();
                    if($part.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close){throw 'CDP closed'};$mem.Write($buffer,0,$part.Count)
                } while(-not $part.EndOfMessage)
                $msg=[Text.Encoding]::UTF8.GetString($mem.ToArray())|ConvertFrom-Json
            } finally {$mem.Dispose()}
            if($msg.method -eq 'Fetch.requestPaused') {
                $req=$msg.params.request
                if(-not (Allowed $req.method $req.url $req.postData)) {
                    $script:blocked++
                    $path=([Uri]$req.url).AbsolutePath
                    if($path -match '^/api/[A-Za-z0-9/_-]+$'){$script:deniedPaths+=($req.method+' '+$path)}
                    Send-Raw 'Fetch.failRequest' @{requestId=$msg.params.requestId;errorReason='BlockedByClient'}
                }
                else {$script:posts++;Send-Raw 'Fetch.continueRequest' @{requestId=$msg.params.requestId}}
            } elseif($msg.method -eq 'Network.requestWillBeSent') {
                # Fetch interception is the enforcing network gate. This event is
                # informational only because the same request was already gated.
            } elseif($msg.method -eq 'Page.javascriptDialogOpening') {
                $script:blocked++;Send-Raw 'Page.handleJavaScriptDialog' @{accept=$false}
            }
            if($msg.id -eq $cmd) {if($msg.error){throw 'CDP command error'};return $msg.result}
        }
    } catch {throw 'CDP operation failed; details suppressed'} finally {$timeout.Dispose()}
}
function Js([string]$expression) {
    $r=Cdp 'Runtime.evaluate' @{expression=$expression;awaitPromise=$true;returnByValue=$true}
    if($r.exceptionDetails){throw 'Browser evaluation failed'};return $r.result.value
}
function WaitJs([string]$expression,[int]$seconds=35) {
    $until=[DateTime]::UtcNow.AddSeconds($seconds)
    while([DateTime]::UtcNow -lt $until){if(Js $expression){return};Start-Sleep -Milliseconds 300}
    throw 'Browser state timed out'
}
function Guard {
    if((Js 'window.__vct && window.__vct.guard()') -ne $true -or $script:blocked -or $script:unexpected) {throw 'Network or secret guard failed'}
}
function Shot([string]$name) {
    Guard;$capture=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($capture.data));$result.screenshots+=$name
}
function Resolve-PrivateInput([string]$path) {
    try {
        if($path.StartsWith('/')) {
            $native=$path
            $converted=& wsl.exe --exec wslpath -w $path 2>$null
            if($LASTEXITCODE -ne 0 -or -not $converted){throw 'path conversion'}
            $full=([string]$converted).Trim()
        } else {
            $full=[IO.Path]::GetFullPath($path)
            $wslPath=& wsl.exe --exec wslpath -u $full 2>$null
            if($LASTEXITCODE -ne 0 -or -not $wslPath){throw 'path conversion'}
            $native=([string]$wslPath).Trim()
        }
        $mode=& wsl.exe --exec stat -c %a $native 2>$null
        if($LASTEXITCODE -ne 0 -or ([string]$mode).Trim() -cne '600'){throw 'private mode'}
        $type=& wsl.exe --exec stat -c %F $native 2>$null
        if($LASTEXITCODE -ne 0 -or ([string]$type).Trim() -cne 'regular file'){throw 'file type'}
    } catch {throw 'Fixture and manifest must be regular 0600 WSL files'}
    if(-not [IO.File]::Exists($full)) {throw 'Fixture file is unavailable'}
    return $full
}
function Click-ExactText([string]$selector,[string]$label) {
    $labelJson=$label|ConvertTo-Json -Compress
    WaitJs "[...document.querySelectorAll('$selector')].some(x=>x.textContent.trim()===$labelJson&&!x.disabled)"
    $ok=Js "(()=>{const e=[...document.querySelectorAll('$selector')].find(x=>x.textContent.trim()===$labelJson);if(!e)return false;e.click();return true})()"
    if($ok -ne $true){throw "Required UI control missing: $label"}
}
function Run-Change([string]$kind,[string]$targetId,[string]$targetCode,[string]$screenName) {
    $script:stage="$kind open version-change quote"
    $script:targetIds=@($targetId)
    Js 'window.__vct.changeId=null;window.__vct.change=null;true'|Out-Null
    Click-ExactText '.subscription-heading button' '版本变更'
    WaitJs '!!document.querySelector("#version-change-title")&&!!document.querySelector(".version-change-modal select")'
    $targetJson=$targetId|ConvertTo-Json -Compress
    if((Js "(()=>{const s=document.querySelector('.version-change-modal select');s.value=$targetJson;s.dispatchEvent(new Event('change',{bubbles:true}));return s.value===$targetJson})()") -ne $true){throw 'Fixture target version is not available in the UI'}
    $script:targetIds=@((Js 'window.__vct.options.versions.map(v=>v.id)'))
    $script:stage="$kind obtain quote"
    Js 'document.querySelector(".version-change-modal form button[type=submit]").click();true'|Out-Null
    WaitJs '!!document.querySelector(".version-change-summary")'
    $script:quoteToken=[string](Js 'window.__vct.quote.quote_token')
    if(-not $script:quoteToken -or $script:quoteToken.Length -gt 8192){throw 'Quote token was not captured in memory'}
    $script:stage="$kind validate quote"
    Click-ExactText '.version-change-modal .modal-actions button' '校验报价'
    WaitJs 'document.querySelector(".version-change-summary .status-pill")?.textContent.includes("已校验")'
    $sample=Js '({action:window.__vct.quote?.action,target:window.__vct.quote?.target_version_code,executionReady:window.__vct.quote?.execution_ready})'
    if($sample.target -cne $targetCode -or $sample.executionReady -ne $false -or
        ($kind -eq 'upgrade' -and $sample.action -cne 'upgrade') -or ($kind -eq 'downgrade' -and $sample.action -cne 'downgrade')){throw 'Server quote action or target differs from fixture'}
    Shot "quote-$kind-$screenName.png"
    $script:stage="$kind submit validated quote"
    Click-ExactText '.version-change-modal .modal-actions button' '提交变更请求'
    WaitJs '!!document.querySelector("#vce-submit-title")'
    Click-ExactText '.vce-confirm-actions button' '确认提交'
    $currentCode=if($kind -eq 'upgrade'){'source'}else{'target'}
    $article="[...document.querySelectorAll('.vce-request')].find(e=>e.querySelector('.vce-request-head strong')?.textContent.includes('$currentCode → $targetCode'))"
    WaitJs "!!($article)&&!!($article).querySelector('.vce-status')"
    WaitJs '!!window.__vct.changeId'
    $observedId=[string](Js 'window.__vct.changeId')
    if(-not $observedId -or $observedId.Length -gt 128){throw 'UI submission did not return one valid change ID'}
    $script:changeId=$observedId
    if($kind -eq 'upgrade') {
        WaitJs "($article).querySelector('.vce-status')?.textContent.trim()==='待支付'"
        Click-ExactText '.vce-request .vce-actions button' '发起模拟支付'
        WaitJs "($article).querySelector('.vce-actions')?.innerText.includes('确认人工支付')"
        Click-ExactText '.vce-request .vce-actions button' '确认人工支付'
        WaitJs '!!document.querySelector("#vce-confirm-title")&&!!document.querySelector(".vce-confirm textarea")'
        $reason="qa-vcs browser $kind simulated payment"
        $reasonJson=$reason|ConvertTo-Json -Compress
        Js "(()=>{const e=document.querySelector('.vce-confirm textarea');e.value=$reasonJson;e.dispatchEvent(new Event('input',{bubbles:true}));return true})()"|Out-Null
        if((Js '!!document.querySelector(".vce-confirm-actions button[type=submit]")') -ne $true){throw 'Payment confirmation control missing'}
        Js 'document.querySelector(".vce-confirm-actions button[type=submit]").click();true'|Out-Null
        WaitJs '!document.querySelector("#vce-confirm-title")'
    } else {
        WaitJs "($article).querySelector('.vce-status')?.textContent.includes('退款')||($article).querySelector('.vce-status.done')"
    }
    $script:stage="$kind poll rendered completed state"
    WaitJs "($article).querySelector('.vce-status.done')?.textContent.trim()==='已完成'" 150
    WaitJs "window.__vct.change?.id===window.__vct.changeId&&window.__vct.change.status==='completed'" 20
    if($kind -eq 'downgrade') {
        if((Js "window.__vct.change.refund_status==='completed'") -ne $true){throw 'Downgrade refund is not completed in the returned change record'}
    }
    if($script:changeId){$script:changeId=$null;$script:createdChangePending=$false};$script:quoteToken=$null
    Guard;Shot "completed-$kind-$screenName.png"
    if($kind -eq 'upgrade'){$result.checks+='upgrade: UI quote/validation/submit; simulated payment and reason confirmation; UI and returned change record completed'}
    else {$result.checks+='downgrade: UI quote/validation/submit; refund worker completed; UI and returned change record show completed refund'}
}

try {
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $script:stage='private fixture path'
    $fixturePath=Resolve-PrivateInput $FixtureFile
    $fixture=[IO.File]::ReadAllText($fixturePath,[Text.Encoding]::UTF8)|ConvertFrom-Json
    if(-not $BaseUrl){$BaseUrl=[string]$fixture.baseUrl};if(-not $DeploymentTag){$DeploymentTag=[string]$fixture.deploymentTag}
    if(-not $OrderNo){$OrderNo=[string]$fixture.orders.api.orderNo}
    $secret=[string]$fixture.buyerAdminToken;$run=[string]$fixture.runId;$order=[string]$fixture.orders.api.id
    if($run -notmatch '^qa-vcs-[0-9a-f]{12}$' -or
        [string]::IsNullOrWhiteSpace($secret) -or [string]::IsNullOrWhiteSpace($OrderNo) -or [string]::IsNullOrWhiteSpace($DeploymentTag)) {throw 'Fixture ownership fields missing or malformed'}
    $script:stage='private manifest path'
    $manifestPath=Resolve-PrivateInput ([string]$fixture.manifest)
    $manifest=[IO.File]::ReadAllText($manifestPath,[Text.Encoding]::UTF8)|ConvertFrom-Json
    if($manifest.run_id -cne $run -or $manifest.products.api -cne ($run+'-p-api') -or
        $manifest.versions.api.source -cne $fixture.orders.api.sourceVersionId -or
        $manifest.versions.api.target -cne $fixture.orders.api.targetVersionId -or
        $manifest.routes.api.source -cne $fixture.orders.api.sourceRoute -or
        $manifest.routes.api.target -cne $fixture.orders.api.targetRoute -or
        $manifest.database_ids.orders.api -cne $order -or $manifest.database_ids.orders.api -ceq 'ORD-261012005144E1B3' -or
        $manifest.users.buyer -cne ($run+'-u-buyer') -or $manifest.enterprises.buyer -cne ($run+'-e-buyer')) {throw 'Fixture IDs do not match the exact private manifest'}
    $script:stage='origin validation'
    $site=[Uri]$BaseUrl;if($site.Scheme -notin @('http','https') -or $site.AbsolutePath -ne '/' -or $site.Query -or $site.Fragment -or $site.UserInfo){throw 'Base URL must be an HTTP(S) origin'}
    $BaseUrl=$site.GetLeftPart([UriPartial]::Authority);$script:orderId=$order
    $script:versionSource=[string]$fixture.orders.api.sourceVersionId;$script:versionTarget=[string]$fixture.orders.api.targetVersionId
    if($script:versionSource -notmatch ('^'+[regex]::Escape($run)+'-') -or $script:versionTarget -notmatch ('^'+[regex]::Escape($run)+'-')){throw 'Version IDs outside fixture roots'}
    $orderNoJson=$OrderNo|ConvertTo-Json -Compress;$tokenJson=$secret|ConvertTo-Json -Compress;$secret=$null
    $script:stage='Chrome startup'
    $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0);$listener.Start();$port=$listener.LocalEndpoint.Port;$listener.Stop()
    $chrome=Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList @('--headless=new',"--remote-debugging-port=$port","--user-data-dir=`"$profile`"",'--no-first-run','about:blank') -PassThru
    $page=$null;for($i=0;$i -lt 60;$i++){try{$page=(Invoke-RestMethod "http://127.0.0.1:$port/json/list")|Where-Object {$_.type -eq 'page'}|Select-Object -First 1;if($page){break}}catch{};Start-Sleep -Milliseconds 500}
    if(-not $page){throw 'Chrome startup timed out'}
    $script:stage='Chrome CDP connection'
    $socket=[Net.WebSockets.ClientWebSocket]::new();$connect=[Threading.CancellationTokenSource]::new(30000);try{$socket.ConnectAsync([Uri]$page.webSocketDebuggerUrl,$connect.Token).GetAwaiter().GetResult()}finally{$connect.Dispose()}
    Cdp 'Page.enable' @{}|Out-Null;Cdp 'Network.enable' @{}|Out-Null;Cdp 'Network.setCacheDisabled' @{cacheDisabled=$true}|Out-Null;Cdp 'Network.setBypassServiceWorker' @{bypass=$true}|Out-Null
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})}|Out-Null;Cdp 'Browser.setDownloadBehavior' @{behavior='deny'}|Out-Null
    $observer=@'
(()=>{const t=window.__vct={orderNo:__ORDER__,order:null,options:null,quote:null,changeId:null,change:null,blocked:0,private:new Set()};
const consume=(method,url,status,text)=>{try{const p=new URL(url,location.origin).pathname,d=JSON.parse(text);if(p==='/api/auth/me')t.user=d.user;if(p==='/api/orders'&&status===200){const a=(d.items||[]).filter(x=>x.order_no===t.orderNo);if(a.length===1)t.order=a[0]}if(t.order&&p==='/api/orders/'+encodeURIComponent(t.order.id)+'/version-change-options')t.options=d;if(t.order&&p==='/api/orders/'+encodeURIComponent(t.order.id)+'/version-change-quote'){t.quote=d;if(d.quote_token)t.private.add(d.quote_token)}if(method==='POST'&&t.order&&p==='/api/orders/'+encodeURIComponent(t.order.id)+'/version-changes'){const item=d.item||d;if(typeof item.id==='string')t.changeId=item.id}if(t.order&&p==='/api/orders/'+encodeURIComponent(t.order.id)+'/version-changes'&&Array.isArray(d.items)){const item=d.items.find(x=>x.id===t.changeId);if(item)t.change=item}}catch{}};
const xo=XMLHttpRequest.prototype.open,xs=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.open=function(m,u,...a){this.__vctUrl=u;this.__vctMethod=m;return xo.call(this,m,u,...a)};XMLHttpRequest.prototype.send=function(b){this.addEventListener('load',()=>consume(this.__vctMethod,this.__vctUrl,this.status,this.responseText));return xs.call(this,b)};
const f=window.fetch;window.fetch=async(...a)=>{const r=await f.apply(this,a);r.clone().text().then(x=>consume(a[1]?.method||a[0]?.method||'GET',r.url,r.status,x));return r};
for(const k of ['alert','confirm','prompt'])window[k]=()=>{t.blocked++;throw Error('dialog blocked')};
t.guard=()=>!t.blocked&&[...t.private].every(x=>!document.documentElement.outerHTML.includes(x)&&![...Object.keys(localStorage)].some(k=>(localStorage.getItem(k)||'').includes(x)));})();
'@.Replace('__ORDER__',$orderNoJson)
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$observer}|Out-Null
    $script:stage='console token bootstrap'
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"}|Out-Null;WaitJs '!!document.querySelector(".login-shell")'
    Js "localStorage.setItem('market_token',$tokenJson);true"|Out-Null;$tokenJson=$null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=900;deviceScaleFactor=1;mobile=$false}|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"}|Out-Null;WaitJs '!!document.querySelector(".sidebar")&&!!window.__vct.order&&!!window.__vct.user'
    if((Js "window.__vct.order.id==='$order'&&window.__vct.order.order_no===$orderNoJson&&window.__vct.order.payment_status==='paid'&&['accepted','completed'].includes(window.__vct.order.delivery_status)&&window.__vct.user.is_active===true&&window.__vct.user.verified_status==='verified'&&!window.__vct.user.platform_role") -ne $true){throw 'Buyer order is not the exact paid/delivered fixture order'}
    Js '(()=>{const n=[...document.querySelectorAll(".order-no")].find(e=>e.textContent.trim()===window.__vct.orderNo);const b=n?.closest("tr")?.querySelector("button[title*=订单]")||n?.closest("tr")?.querySelector("button");if(!b)return false;b.click();return true})()'|Out-Null
    WaitJs '!!document.querySelector(".order-drawer")&&!!document.querySelector(".subscription-heading button")'
    $script:targetIds=@($script:versionTarget)
    Run-Change 'upgrade' $script:versionTarget 'target' 'desktop'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=1;mobile=$true}|Out-Null
    Run-Change 'downgrade' $script:versionSource 'source' 'mobile'
    Guard;$result.status='passed';$result.passed=$true;$result.deploymentTag=$DeploymentTag;$result.allowedPOSTs=$script:posts
} catch {
    $result.status='failed';$result.failedStage=$script:stage;$result.reason='Failure details suppressed'
    $result.errorType=$_.Exception.GetType().Name
    $result.errorLine=$_.InvocationInfo.ScriptLineNumber
    if($socket){try{Shot 'failure.png'}catch{}}
    throw 'Browser transaction acceptance failed; inspect browser-results.json'
} finally {
    $result.blockedRequests=$script:blocked;$result.unexpectedRequests=$script:unexpected
    $result.deniedPaths=$script:deniedPaths
    if(Test-Path -LiteralPath $OutputDir){$result|ConvertTo-Json -Depth 16|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')}
    $tokenJson=$null;$secret=$null;$fixture=$null;$script:quoteToken=$null
    if($socket){try{$socket.Dispose()}catch{}}
    if($chrome){Get-CimInstance Win32_Process|Where-Object {$_.Name -eq 'chrome.exe' -and $_.CommandLine -like "*$profile*"}|ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}}
    if(Test-Path -LiteralPath $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
}
