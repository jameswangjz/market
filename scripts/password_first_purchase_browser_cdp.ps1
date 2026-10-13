param(
    [Parameter(Mandatory=$true)][string]$FixtureFile,
    [string]$BaseUrl='',
    [string]$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe',
    [string]$OutputDir="$env:TEMP\market-password-first-purchase"
)
$ErrorActionPreference='Stop'
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0; $script:orderId=''; $script:stage='startup'
$profile=Join-Path $env:TEMP ('market-password-first-'+[Guid]::NewGuid().ToString('N'))
$script:artifactDir=''
$result=@{status='pending';checks=@();screenshots=@();scope='password login, public storefront quote and UI order, UI simulated payment, online worker delivery; no credential download'}

function Send-Cdp([string]$method,$parameters) {
    $script:commandId++
    $payload=@{id=$script:commandId;method=$method;params=$parameters}|ConvertTo-Json -Depth 25 -Compress
    $bytes=[Text.Encoding]::UTF8.GetBytes($payload)
    $timeout=[Threading.CancellationTokenSource]::new(30000)
    try{$null=$script:socket.SendAsync([ArraySegment[byte]]::new($bytes),[Net.WebSockets.WebSocketMessageType]::Text,$true,$timeout.Token).GetAwaiter().GetResult()}
    finally{$timeout.Dispose()}
    return $script:commandId
}
function Cdp([string]$method,$parameters) {
    $id=Send-Cdp $method $parameters
    $timeout=[Threading.CancellationTokenSource]::new(30000)
    try {
        while($true) {
            $memory=[IO.MemoryStream]::new()
            try {
                do {
                    $buffer=New-Object byte[] 65536
                    $part=$script:socket.ReceiveAsync([ArraySegment[byte]]::new($buffer),$timeout.Token).GetAwaiter().GetResult()
                    if($part.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close){throw 'closed'}
                    $memory.Write($buffer,0,$part.Count)
                } while(-not $part.EndOfMessage)
                $answer=[Text.Encoding]::UTF8.GetString($memory.ToArray())|ConvertFrom-Json
            } finally{$memory.Dispose()}
            if($answer.method -eq 'Fetch.requestPaused') {
                $request=$answer.params.request
                $hasAuthorization=$false
                if($request.headers){foreach($headerName in $request.headers.PSObject.Properties.Name){if([string]$headerName -ieq 'Authorization'){$hasAuthorization=$true}}}
                $requestPath=([Uri]$request.url).AbsolutePath
                $publicProductRequest=$requestPath -match '^/api/storefront/products(?:/|$)'
                if($publicProductRequest -and $hasAuthorization){$script:publicAuth++}
                if(-not ($publicProductRequest -and $hasAuthorization)) {
                  $allowed=Allowed-Request $request.method $request.url $request.postData
                } else {$allowed=$false}
                if($allowed) {
                    Send-Cdp 'Fetch.continueRequest' @{requestId=$answer.params.requestId}|Out-Null
                } else {
                    $script:blocked++
                    Send-Cdp 'Fetch.failRequest' @{requestId=$answer.params.requestId;errorReason='BlockedByClient'}|Out-Null
                }
            } elseif($answer.method -eq 'Page.javascriptDialogOpening') {
                $script:nativeDialogs++
                Send-Cdp 'Page.handleJavaScriptDialog' @{accept=$false}|Out-Null
            }
            if($answer.id -eq $id) {
                if($answer.error){throw 'rejected'}
                return $answer.result
            }
        }
    } catch {throw 'CDP operation failed; private details suppressed'}
    finally{$timeout.Dispose()}
}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $uri=[Uri]$url
        if($uri.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $uri.UserInfo){return $false}
        if($method -in @('GET','HEAD','OPTIONS')) {
            if($uri.AbsolutePath -match '(?i)(credential|api-credentials).*(download|secret)'){return $false}
            return $true
        }
        if($method -cne 'POST'){return $false}
        $path=$uri.AbsolutePath
        if($path -ceq '/api/auth/login') {
            $body=$postData|ConvertFrom-Json
            return ($body.identifier -ceq $script:fixture.buyer_email -or $body.email -ceq $script:fixture.buyer_email) -and
                [string]$body.password -ceq [string]$script:fixture.buyer_password
        }
        if($path -ceq '/api/orders/quote' -or $path -ceq '/api/orders') {
            $body=$postData|ConvertFrom-Json
            return $body.product_id -ceq $script:fixture.product_id -and
                $body.product_version_id -ceq $script:fixture.product_version_id -and
                $body.buyer_enterprise_id -ceq $script:fixture.enterprise_id -and
                $body.subscription_months -eq 1
        }
        if($script:orderId -and $path -ceq ("/api/orders/{0}/transition" -f $script:orderId)) {
            $body=$postData|ConvertFrom-Json
            return $body.action -cin @('start_payment','confirm_payment')
        }
    } catch {return $false}
    return $false
}
function Js([string]$expression) {
    $response=Cdp 'Runtime.evaluate' @{expression=$expression;awaitPromise=$true;returnByValue=$true}
    if($response.exceptionDetails){throw 'Browser interaction failed'}
    return $response.result.value
}
function WaitJs([string]$expression,[int]$seconds=60) {
    for($i=0;$i -lt ($seconds*4);$i++) {
        if(Js $expression){return}
        Start-Sleep -Milliseconds 250
    }
    throw 'Browser state timed out'
}
function Click-Action([string]$label) {
    $encoded=$label|ConvertTo-Json -Compress
    WaitJs "(()=>{const b=[...document.querySelectorAll('.order-drawer .action-list button')].find(x=>x.textContent.includes($encoded));return !!b&&!b.disabled})()"
    Js "(()=>{const b=[...document.querySelectorAll('.order-drawer .action-list button')].find(x=>x.textContent.includes($encoded));if(!b||b.disabled)throw Error();b.click();return true})()"|Out-Null
    WaitJs '!!document.querySelector(".action-dialog")'
    Js 'document.querySelector(".action-dialog button[type=submit]").click();true'|Out-Null
    WaitJs '!document.querySelector(".action-dialog")'
}
function Screenshot([string]$name) {
    if(-not (Js '(()=>{if(document.querySelector("input[type=password],.api-credential-reveal"))return false;return !/\bmk_[A-Za-z0-9_-]{24,}\b/.test(document.body.innerText)})()')) {
        throw 'Screenshot privacy check failed'
    }
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $script:artifactDir $name),[Convert]::FromBase64String($shot.data))
    $result.screenshots+=,$name
}
function Connect-OwnedChrome {
    if(-not(Test-Path -LiteralPath $ChromePath -PathType Leaf)){throw 'Chrome executable was not found'}
    New-Item -ItemType Directory -Path $profile|Out-Null
    $acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($true,$false)
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User;$acl.SetOwner($sid)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
    Set-Acl -LiteralPath $profile -AclObject $acl
    $script:chrome=Start-Process $ChromePath -ArgumentList @('--headless=new','--remote-debugging-port=0',"--user-data-dir=`"$profile`"",'--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-sync','--disable-extensions','about:blank') -PassThru
    $target=$null;$port=0
    for($i=0;$i -lt 60 -and -not $target;$i++) {
        try {
            $portFile=Join-Path $profile 'DevToolsActivePort'
            if(Test-Path $portFile){$lines=@(Get-Content -LiteralPath $portFile);if($lines.Count -eq 2 -and $lines[0] -match '^\d{1,5}$') {
                $port=[int]$lines[0];$pages=(Invoke-RestMethod "http://127.0.0.1:$port/json/list" -TimeoutSec 2)
                $target=@($pages|Where-Object{$_.type -ceq 'page' -and $_.url -ceq 'about:blank'})|Select-Object -First 1
            }}
        } catch {}
        if(-not $target){Start-Sleep -Milliseconds 500}
    }
    if(-not $target){throw 'Owned Chrome startup timed out'}
    $debugUri=[Uri]$target.webSocketDebuggerUrl
    if($debugUri.Scheme -cne 'ws' -or $debugUri.Host -cne '127.0.0.1' -or $debugUri.Port -ne $port -or $debugUri.UserInfo -or $debugUri.Query -or $debugUri.Fragment -or $debugUri.AbsolutePath -notmatch '^/devtools/page/[A-Za-z0-9-]+$'){throw 'Unexpected Chrome debug target'}
    $script:socket=[Net.WebSockets.ClientWebSocket]::new()
    $timeout=[Threading.CancellationTokenSource]::new(30000)
    try{$null=$script:socket.ConnectAsync($debugUri,$timeout.Token).GetAwaiter().GetResult()}finally{$timeout.Dispose()}
    Cdp 'Page.enable' @{}|Out-Null;Cdp 'Network.enable' @{}|Out-Null
    Cdp 'Network.setCacheDisabled' @{cacheDisabled=$true}|Out-Null
    Cdp 'Network.setBypassServiceWorker' @{bypass=$true}|Out-Null
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})}|Out-Null
    Cdp 'Browser.setDownloadBehavior' @{behavior='deny'}|Out-Null
    $observer=@'
(() => {
  try {
    const saved = JSON.parse(sessionStorage.getItem('market_pfirst_order') || '{}');
    if (/^[a-f0-9]{32}$/.test(saved.id) && typeof saved.orderNo === 'string') {
      window.__passwordFirstOrderId = saved.id;
      window.__passwordFirstOrderNo = saved.orderNo;
    }
  } catch {}
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this.__pfirstMethod = String(method).toUpperCase();
    this.__pfirstPath = new URL(url, location.href).pathname;
    return open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function(body) {
    if (this.__pfirstMethod === 'POST' && this.__pfirstPath === '/api/orders') {
      this.addEventListener('load', () => {
        try {
          const order = JSON.parse(this.responseText);
          if (this.status === 200 && /^[a-f0-9]{32}$/.test(order.id) && order.order_no) {
            window.__passwordFirstOrderId = order.id;
            window.__passwordFirstOrderNo = order.order_no;
            sessionStorage.setItem('market_pfirst_order', JSON.stringify({id: order.id, orderNo: order.order_no}));
          }
        } catch {}
      }, { once: true });
    }
    return send.call(this, body);
  };
})();
'@
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$observer}|Out-Null
}

try {
    $description=& wsl.exe --exec stat --printf '%a|%F' -- $FixtureFile 2>$null
    if($LASTEXITCODE -ne 0 -or [string]$description -cne '600|regular file'){throw 'Fixture must be a regular 0600 file'}
    $winFile=& wsl.exe --exec wslpath -w $FixtureFile
    $script:fixture=Get-Content -LiteralPath $winFile -Raw|ConvertFrom-Json
    if($script:fixture.run_id -notmatch '^qa-vcs-[0-9a-f]{12}$' -or
       $script:fixture.product_id -cne ($script:fixture.run_id+'-p-api') -or
       $script:fixture.enterprise_id -cne ($script:fixture.run_id+'-e-buyer') -or
       $script:fixture.product_version_id -cne ($script:fixture.run_id+'-v-api-source')){throw 'Unexpected private fixture ownership roots'}
    $script:artifactDir=Join-Path $OutputDir ($script:fixture.run_id+'-'+[Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $script:artifactDir|Out-Null
    if(-not $BaseUrl){$BaseUrl=[string]$script:fixture.base_url}
    $baseUri=[Uri]$BaseUrl
    if($baseUri.GetLeftPart([UriPartial]::Authority) -cne $script:fixture.base_url -or $baseUri.UserInfo -or $baseUri.Query -or $baseUri.Fragment){throw 'Browser origin differs from the private fixture'}
    $BaseUrl=$baseUri.GetLeftPart([UriPartial]::Authority)
    $script:stage='owned Chrome startup';Connect-OwnedChrome
    $script:stage='password login UI'
    Cdp 'Page.navigate' @{url="$BaseUrl/console"}|Out-Null
    WaitJs '!!document.querySelector(".login-shell input[autocomplete=username]") && !!document.querySelector(".login-shell input[autocomplete=current-password]")'
    $email=$script:fixture.buyer_email|ConvertTo-Json -Compress
    $password=$script:fixture.buyer_password|ConvertTo-Json -Compress
    Js "(()=>{const e=document.querySelector('.login-shell input[autocomplete=username]'),p=document.querySelector('.login-shell input[autocomplete=current-password]');e.value=$email;e.dispatchEvent(new Event('input',{bubbles:true}));p.value=$password;p.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('.login-shell button[type=submit]').click();return true})()"|Out-Null
    WaitJs '!!localStorage.getItem("market_token") && !document.querySelector(".login-shell")' 90
    $password=$null;$script:fixture.buyer_password=$null
    $script:stage='existing fixture order check'
    $productIdJson=$script:fixture.product_id|ConvertTo-Json -Compress
    $enterpriseIdJson=$script:fixture.enterprise_id|ConvertTo-Json -Compress
    $buyerIdJson=($script:fixture.run_id+'-u-buyer')|ConvertTo-Json -Compress
    $existingExpression=@'
fetch('/api/orders',{credentials:'same-origin',headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}}).then(async r=>{
  const d=await r.json();
  const found=(Array.isArray(d.items)?d.items:[]).filter(o=>o.product_name===__PRODUCT__&&o.buyer_enterprise_id===__ENTERPRISE__&&o.buyer_user_id===__BUYER__);
  if(found.length===1&&/^[a-f0-9]{32}$/.test(found[0].id))sessionStorage.setItem('market_pfirst_order',JSON.stringify({id:found[0].id,orderNo:found[0].order_no,paymentStatus:found[0].payment_status}));
  return {status:r.status,count:found.length};
})
'@
    $existingExpression=$existingExpression.Replace('__PRODUCT__',$productIdJson).Replace('__ENTERPRISE__',$enterpriseIdJson).Replace('__BUYER__',$buyerIdJson)
    $existing=Js $existingExpression
    if($existing.status -ne 200 -or $existing.count -gt 1){throw 'Fixture order lookup was ambiguous; refusing another order'}
    $reusedOrder=$existing.count -eq 1
    if(-not $reusedOrder) {
        $script:stage='public storefront product checkout'
        Cdp 'Page.navigate' @{url="$BaseUrl/products/$($script:fixture.product_id)"}|Out-Null
        WaitJs '(()=>{const s=document.querySelector(".store-checkout-field select");return !!s&&[...s.options].some(o=>o.value)})()' 90
        $enterprise=$script:fixture.enterprise_id|ConvertTo-Json -Compress
        Js "(()=>{const s=document.querySelector('.store-checkout-field select');s.value=$enterprise;s.dispatchEvent(new Event('change',{bubbles:true}));return true})()"|Out-Null
        WaitJs '!!document.querySelector(".store-quote-amount") && !document.querySelector(".store-version-tool button.primary-btn.full-btn").disabled' 90
        $script:stage='storefront order click'
        Js 'document.querySelector(".store-version-tool button.primary-btn.full-btn").click();true'|Out-Null
        WaitJs 'location.pathname==="/console" && !!window.localStorage.getItem("market_token")' 90
        $script:stage='owned order captured'
        WaitJs '(()=>{try{return /^[a-f0-9]{32}$/.test(JSON.parse(sessionStorage.getItem("market_pfirst_order")||"{}").id)}catch{return false}})()' 60
    } else {
        $script:stage='resume existing fixture order'
        Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"}|Out-Null
    }
    $script:orderId=Js 'JSON.parse(sessionStorage.getItem("market_pfirst_order")).id'
    WaitJs '(()=>{try{const no=JSON.parse(sessionStorage.getItem("market_pfirst_order")).orderNo;return [...document.querySelectorAll(".order-no")].some(n=>n.textContent.trim()===no)}catch{return false}})()' 90
    $script:stage='simulated payment UI'
    Js '(()=>{const no=JSON.parse(sessionStorage.getItem("market_pfirst_order")).orderNo;const cell=[...document.querySelectorAll(".order-no")].find(n=>n.textContent.trim()===no);const row=cell?.closest("tr");if(!row)throw Error();row.querySelector("button[title=\"查看订单\"]").click();return true})()'|Out-Null
    WaitJs '(()=>{const d=document.querySelector(".order-drawer");return !!d&&!!d.querySelector(".state-grid")})()' 60
    $paymentStatus=Js '(()=>{const d=document.querySelector(".order-drawer");const s=d?.querySelector(".state-grid");return s?.innerText||""})()'
    if($paymentStatus -match '未支付|unpaid') {
        $script:stage='simulated payment start action'
        Click-Action '发起模拟支付'
        WaitJs '(()=>{const d=document.querySelector(".order-drawer");const s=d?.querySelector(".state-grid")?.innerText||"";const b=[...document.querySelectorAll(".order-drawer .action-list button")].find(x=>x.textContent.includes("模拟确认支付"));return !!d&&/支付中|paying/i.test(s)&&!!b&&!b.disabled})()' 60
    }
    $paid=Js '(()=>{const d=document.querySelector(".order-drawer");return !!d&&/已支付|paid/i.test(d.querySelector(".state-grid")?.innerText||"")})()'
    if(-not $paid) {
        $script:stage='simulated payment confirmation action'
        WaitJs '(()=>{const d=document.querySelector(".order-drawer");const s=d?.querySelector(".state-grid")?.innerText||"";const b=[...document.querySelectorAll(".order-drawer .action-list button")].find(x=>x.textContent.includes("模拟确认支付"));return !!d&&/支付中|paying/i.test(s)&&!!b&&!b.disabled})()' 60
        Click-Action '模拟确认支付'
        WaitJs '(()=>{const d=document.querySelector(".order-drawer");return !!d&&/已支付|paid/i.test(d.querySelector(".state-grid")?.innerText||"")})()' 60
    }
    Js 'document.querySelector("button[aria-label=\"关闭订单详情\"]").click();true'|Out-Null
    $script:stage='online worker delivery'
    for($attempt=0;$attempt -lt 36;$attempt++) {
        $delivered=Js '(()=>{const no=JSON.parse(sessionStorage.getItem("market_pfirst_order")).orderNo;const r=[...document.querySelectorAll(".order-no")].find(n=>n.textContent.trim()===no);const text=r?.closest("tr")?.textContent||"";return text.includes("验收通过")||text.includes("已完成")})()'
        if($delivered){break}
        Start-Sleep -Seconds 5
        Js '(()=>{const b=[...document.querySelectorAll("button")].find(x=>x.textContent.includes("刷新订单"));b?.click();return !!b})()'|Out-Null
        Start-Sleep -Milliseconds 400
    }
    if(-not $delivered){throw 'Online delivery worker did not complete within the bounded wait'}
    $script:stage='buyer order credential summary'
    $summaryPath='/api/orders/'+$script:orderId+'/api-credentials'
    $pathJson=$summaryPath|ConvertTo-Json -Compress
    $orderIdJson=$script:orderId|ConvertTo-Json -Compress
    $productIdJson=$script:fixture.product_id|ConvertTo-Json -Compress
    $sourceRouteJson=$script:fixture.source_route|ConvertTo-Json -Compress
    $summaryExpression=@'
fetch(__PATH__,{credentials:'same-origin',headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}}).then(async r=>{
  const d=await r.json();
  const allowed=new Set(['id','name','key_prefix','status','secret_download_available','product_version_id','rate_limit_per_minute','daily_quota','monthly_quota','total_quota','expires_at','last_used_at','created_at']);
  const topAllowed=new Set(['order_id','product_id','product_name','route_key','gateway_base_path','shared_scope','items']);
  const items=Array.isArray(d.items)?d.items:[];
  const safe=Object.keys(d).every(k=>topAllowed.has(k))&&items.every(x=>Object.keys(x).every(k=>allowed.has(k))&&
    !Object.keys(x).some(k=>/secret|password|token|api_key|upstream/i.test(k)&&k!=='secret_download_available')&&
    !Object.values(x).some(v=>typeof v==='string'&&(/\bmk_[A-Za-z0-9_-]{24,}\b/.test(v)||/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/.test(v))));
  return {status:r.status,orderOK:d.order_id===__ORDER__,productOK:d.product_id===__PRODUCT__,
    routeOK:d.route_key===__ROUTE__,count:items.length,
    credentialOK:items.length===1&&items[0].status==='active'&&items[0].secret_download_available===true,
    secretFree:safe};
})
'@
    $summaryExpression=$summaryExpression.Replace('__PATH__',$pathJson).Replace('__ORDER__',$orderIdJson).Replace('__PRODUCT__',$productIdJson).Replace('__ROUTE__',$sourceRouteJson)
    $summary=Js $summaryExpression
    if($summary.status -ne 200 -or -not $summary.orderOK -or -not $summary.productOK -or -not $summary.routeOK -or
       $summary.count -ne 1 -or -not $summary.credentialOK -or -not $summary.secretFree){throw 'Buyer order credential summary check failed'}
    $script:stage='safe desktop and mobile order screenshots'
    Js '(()=>{const no=JSON.parse(sessionStorage.getItem("market_pfirst_order")).orderNo;const c=[...document.querySelectorAll(".order-no")].find(n=>n.textContent.trim()===no);const r=c?.closest("tr");if(!r)throw Error();r.querySelector("button[title=\"查看订单\"]").click();return true})()'|Out-Null
    WaitJs '!!document.querySelector(".order-drawer .api-order-panel .api-credential-item") && !document.querySelector("input[type=password]")'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Screenshot 'desktop-order.png'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=1;mobile=$true}|Out-Null
    Screenshot 'mobile-order.png'
    if($script:blocked -or $script:nativeDialogs -or $script:publicAuth){throw 'Unexpected network request, public authorization header or native dialog was blocked'}
    $result.status='passed';$result.checks=@('password form login via /api/auth/login','public storefront quote and exact product order through UI','simulated payment initiated and confirmed through UI','online delivery worker completion observed','buyer order credential summary contains no secrets','desktop and mobile order screenshots passed the privacy guard','no extra POST requests or secret downloads')
} catch {
    $result.status='failed';$result.stage=$script:stage
    $result.failure='Browser acceptance stopped; private exception details suppressed'
    $result.error_type=$_.Exception.GetType().Name
    $result.error_line=[int]$_.InvocationInfo.ScriptLineNumber
    throw 'Password first-purchase browser acceptance failed; consult the stage in browser-results.json'
} finally {
    $result.stage=$script:stage;$result.blockedRequests=$script:blocked;$result.nativeDialogs=$script:nativeDialogs;$result.publicStorefrontAuthorization=$script:publicAuth
    if($script:artifactDir -and (Test-Path $script:artifactDir)){$result|ConvertTo-Json -Depth 8|Set-Content -Encoding UTF8 (Join-Path $script:artifactDir 'browser-results.json')}
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue
}
