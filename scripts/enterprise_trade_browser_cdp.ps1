param(
    [Parameter(Mandatory=$true)][string]$FixtureFile,
    [ValidateSet('admin','super')][string]$BuyerRole='admin',
    [ValidateSet('password','jwt')][string]$LoginMode='password',
    [string]$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe',
    [string]$OutputDir="$env:TEMP\market-enterprise-trade",
    [switch]$GuardOnly
)
$ErrorActionPreference='Stop'
# Reuse only declarations, never execute the legacy driver's top-level live flow.
$parseErrors=$null;$parseTokens=$null
$legacy=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'password_first_purchase_browser_cdp.ps1'),[ref]$parseTokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'Legacy CDP helper parse failed'}
$names=@('Send-Cdp','Cdp','Js','WaitJs','Click-Action','Connect-OwnedChrome')
foreach($name in $names){
    $nodes=@($legacy.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name},$false))
    if($nodes.Count -ne 1){throw 'Legacy CDP declaration changed'}
    $declaration=$nodes[0].Extent.Text
    if($name -ceq 'Cdp') {
        $denialMarker='$script:blocked++'
        if(-not $declaration.Contains($denialMarker)){throw 'Legacy CDP denial handler changed'}
        $declaration=$declaration.Replace($denialMarker,($denialMarker+"`n                    Record-DeniedRequest `$request.method `$request.url"))
        $allowCall='Allowed-Request $request.method $request.url $request.postData'
        if(-not $declaration.Contains($allowCall)){throw 'Legacy CDP request guard call changed'}
        $declaration=$declaration.Replace($allowCall,($allowCall+' $request.headers'))
    }
    if($name -ceq 'Connect-OwnedChrome') {
        # The legacy observer is unfiltered; install our exact fixture observer instead.
        $legacyInstall="Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=`$observer}|Out-Null"
        if(-not $declaration.Contains($legacyInstall)){throw 'Legacy Chrome observer declaration changed'}
        $declaration=$declaration.Replace($legacyInstall,'')
    }
    . ([scriptblock]::Create($declaration))
}

function Record-DeniedRequest([string]$method,[string]$url) {
    if($script:deniedRequests.Count -ge 32){return}
    $safeMethod='OTHER';if($method -cmatch '^[A-Z]{1,16}$'){$safeMethod=$method}
    $path='invalid-url';$queryKeys=@()
    try {
        $uri=[Uri]$url
        if($uri.IsAbsoluteUri) {
            $path=$uri.AbsolutePath
            $path=[regex]::Replace($path,'(?i)(mk_[A-Za-z0-9_-]{24,}|eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+)','[redacted]')
            if($path.Length -gt 512){$path=$path.Substring(0,512)}
            foreach($part in $uri.Query.TrimStart('?').Split('&')) {
                if(-not $part -or $queryKeys.Count -ge 16){continue}
                $key=[Uri]::UnescapeDataString($part.Split([char[]]@('='),2)[0].Replace('+',' '))
                if($key -cnotmatch '^[A-Za-z_][A-Za-z0-9_]{0,47}$'){$key='[redacted-key]'}
                if($key -cnotin $queryKeys){$queryKeys+=,$key}
            }
        }
    } catch {}
    $script:deniedRequests+=,@{method=$safeMethod;path=$path;queryKeys=@($queryKeys)}
}

function Allowed-Request([string]$method,[string]$url,[string]$postData,$headers=$null) {
    try {
        $uri=[Uri]$url
        if($uri.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $uri.UserInfo -or $uri.Fragment){return $false}
        $path=$uri.AbsolutePath
        if($path -match '(?i)(download|secret|credential-reveal)' -or $path.Contains('%')){return $false}
        if($method -ceq 'GET') {
            if($path -cin @('/console',('/products/'+$script:fixture.product_id),'/favicon.ico')){return $true}
            if($path -cmatch '^/assets/[A-Za-z0-9_.-]+\.(js|css|woff2|png|svg)$' -and -not $uri.Query){return $true}
            if($path -cin @('/api/notifications/summary','/api/notifications/stream','/api/notifications/sender-context')) {
                if(-not $uri.Query) {
                    if($path -cnotin @('/api/notifications/stream','/api/notifications/sender-context') -or
                       $script:actorRole -cnotin @('admin','super')){return $false}
                    if(-not $headers){return $false}
                    foreach($header in $headers.PSObject.Properties) {
                        if($header.Name -ieq 'Authorization') {
                            # Password login issues a fresh JWT; the real backend verifies its signature.
                            if([string]$header.Value -cnotmatch '^Bearer ([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)$'){return $false}
                            $payload=$Matches[2].Replace('-','+').Replace('_','/')
                            $payload=$payload.PadRight($payload.Length+((4-$payload.Length%4)%4),'=')
                            $claims=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload))|ConvertFrom-Json
                            return $claims.sub -cin @($script:fixture.actors.super.user_id,$script:fixture.actors.admin.user_id)
                        }
                    }
                    return $false
                }
                $seen=@{}
                foreach($part in $uri.Query.TrimStart('?').Split('&')) {
                    $pair=$part.Split([char[]]@('='),2)
                    if($pair.Count -ne 2){return $false}
                    $key=[Uri]::UnescapeDataString($pair[0].Replace('+',' '))
                    $value=[Uri]::UnescapeDataString($pair[1].Replace('+',' '))
                    if($seen.ContainsKey($key) -or $key -cnotin @('enterprise_id','business_view')){return $false}
                    if($key -ceq 'enterprise_id' -and $value -cne $script:fixture.enterprise_id){return $false}
                    if($key -ceq 'business_view' -and $value -cne 'buyer' -and
                       ($value -cne 'provider' -or -not $script:startupNotifications -or
                        [DateTime]::UtcNow -ge $script:startupNotificationDeadline)){return $false}
                    $seen[$key]=$true
                }
                return $seen.ContainsKey('enterprise_id')
            }
            $reads=@('/api/auth/me','/api/settings/application-address-policy',
                '/api/storefront/buyer-enterprises','/api/orders',
                ('/api/storefront/products/'+$script:fixture.product_id),
                ('/api/storefront/products/'+$script:fixture.product_id+'/logo'))
            if($path -cin $reads){
                foreach($part in $uri.Query.TrimStart('?').Split('&')){
                    if(-not $part){continue}
                    $pair=$part.Split('=',2);$key=[Uri]::UnescapeDataString($pair[0]);$value=[Uri]::UnescapeDataString($pair[1])
                    if($key -cnotin @('enterprise_id','business_view','view_as','page','page_size')){return $false}
                    if($key -ceq 'enterprise_id' -and $value -cne $script:fixture.enterprise_id){return $false}
                    if($key -cin @('business_view','view_as') -and $value -cne 'buyer'){return $false}
                    if($key -cin @('page','page_size') -and $value -notmatch '^\d{1,3}$'){return $false}
                }
                return $true
            }
            if($script:orderId -and -not $uri.Query -and $path -cin @(
                ('/api/orders/'+$script:orderId),('/api/orders/'+$script:orderId+'/subscription'),
                ('/api/orders/'+$script:orderId+'/version-changes'),
                ('/api/orders/'+$script:orderId+'/product-files'),('/api/orders/'+$script:orderId+'/api-credentials'),
                ('/api/orders/'+$script:orderId+'/delivery-access'))){return $true}
            if($script:subscriptionId -and -not $uri.Query -and $path -cin @(
                ('/api/saas-subscriptions/'+$script:subscriptionId+'/operations'),
                ('/api/saas-subscriptions/'+$script:subscriptionId+'/sync-tree'))){return $true}
            return $false
        }
        if($method -cne 'POST' -or $uri.Query){return $false}
        $body=$postData|ConvertFrom-Json
        $keys=@($body.PSObject.Properties.Name)
        if($path -ceq '/api/auth/login') {
            if(@($keys|Where-Object{$_ -cnotin @('identifier','email','password')}).Count){return $false}
            $actor=$script:fixture.actors.($script:actorRole)
            return ($body.identifier -ceq $actor.email -or $body.email -ceq $actor.email) -and $body.password -ceq $actor.password
        }
        if($path -cin @('/api/orders/quote','/api/orders')) {
            if($script:orderId -or ($path -ceq '/api/orders' -and ($script:orderPosts -ge 1 -or $script:actorRole -cne 'super'))){return $false}
            $allowedKeys=@('product_id','product_version_id','buyer_enterprise_id','subscription_months')
            if($path -ceq '/api/orders') {
                $allowedKeys+='quote_id'
                if($script:quoteId -cnotmatch '^[a-f0-9]{64}$' -or $body.quote_id -cne $script:quoteId){return $false}
            }
            if(@($keys|Where-Object{$_ -cnotin $allowedKeys}).Count){return $false}
            $ok=$body.product_id -ceq $script:fixture.product_id -and
                $body.product_version_id -ceq $script:fixture.product_version_id -and
                $body.buyer_enterprise_id -ceq $script:fixture.enterprise_id -and $body.subscription_months -eq 1
            if($ok -and $path -ceq '/api/orders'){$script:orderPosts++}
            return $ok
        }
        if($script:orderId -and $path -ceq ('/api/orders/'+$script:orderId+'/transition')) {
            if(@($keys|Where-Object{$_ -cnotin @('action','reason')}).Count){return $false}
            return $script:actorRole -ceq 'super' -and $body.action -cin @('start_payment','confirm_payment')
        }
    } catch {return $false}
    return $false
}
function Install-OwnedOrderObserver {
    $owned=@{origin=$BaseUrl;product=$script:fixture.product_id;version=$script:fixture.product_version_id;
        enterprise=$script:fixture.enterprise_id;buyer=$script:fixture.actors.super.user_id}
    $source=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'enterprise_trade_browser_order_observer.js'))+
        '('+($owned|ConvertTo-Json -Compress)+');'
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$source}|Out-Null
    Js $source|Out-Null
    $script:orderObserverInstalled=$true
}
function Start-BuyerNavigation {
    $script:startupNotifications=$true
    $script:startupNotificationDeadline=[DateTime]::UtcNow.AddSeconds(60)
}
function Finish-BuyerNavigation {
    $enterprise=$script:fixture.enterprise_id|ConvertTo-Json -Compress
    WaitJs "(()=>{const q=new URLSearchParams(location.search),s=document.querySelector('.console-buyer-context #buyer-enterprise');return location.pathname==='/console'&&q.get('view')==='orders'&&q.get('enterprise_id')===$enterprise&&!!s&&!s.disabled&&[...s.options].some(o=>o.value===$enterprise)})()" 60
    $script:startupNotifications=$false
}
function Navigate-BuyerConsole {
    Start-BuyerNavigation
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders&enterprise_id=$($script:fixture.enterprise_id)"}|Out-Null
}
if($GuardOnly){return}

function Login-Actor([string]$role) {
    $previousStage=$result.stage
    $result.stage="login ${role}: prepare navigation"
    $script:actorRole=$role
    Navigate-BuyerConsole
    $result.stage="login ${role}: clear previous token"
    Js 'localStorage.removeItem("market_token");true'|Out-Null
    $result.stage="login ${role}: fresh console navigation"
    Navigate-BuyerConsole
    $actor=$script:fixture.actors.$role
    if($LoginMode -ceq 'jwt') {
        $result.stage="login ${role}: private JWT session"
        $jwt=$actor.token|ConvertTo-Json -Compress
        Js "localStorage.setItem('market_token',$jwt);true"|Out-Null
        Navigate-BuyerConsole
    } else {
        $result.stage="login ${role}: password form wait"
        WaitJs '!!document.querySelector(".login-shell input[autocomplete=username]")'
        $result.stage="login ${role}: password form submit"
        $email=$actor.email|ConvertTo-Json -Compress;$password=$actor.password|ConvertTo-Json -Compress
        Js "(()=>{const e=document.querySelector('input[autocomplete=username]'),p=document.querySelector('input[autocomplete=current-password]');e.value=$email;e.dispatchEvent(new Event('input',{bubbles:true}));p.value=$password;p.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('.login-shell button[type=submit]').click();return true})()"|Out-Null
    }
    $result.stage="login ${role}: authenticated UI wait"
    WaitJs '!!localStorage.getItem("market_token") && !document.querySelector(".login-shell")' 60
    $uid=$actor.user_id|ConvertTo-Json -Compress
    $enterprise=$script:fixture.enterprise_id|ConvertTo-Json -Compress
    $expectedRole='enterprise_admin';if($role -ceq 'super'){$expectedRole='super_admin'}
    $memberRole=$expectedRole|ConvertTo-Json -Compress
    $result.stage="login ${role}: exact membership proof"
    $identity=Js "fetch('/api/auth/me',{headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}}).then(async r=>{const d=await r.json();return r.status===200&&d.user?.id===$uid&&!d.user?.platform_role&&(d.user.enterprise_memberships||[]).some(m=>m.enterprise_id===$enterprise&&m.role===$memberRole&&m.status==='active')})"
    if(-not $identity){throw 'Current real buyer identity or enterprise role differs'}
    $result.stage="login ${role}: buyer order context wait"
    Finish-BuyerNavigation
    $result.stage=$previousStage
}
function Open-OwnedOrder {
    $id=$script:orderId|ConvertTo-Json -Compress
    $product=$script:fixture.product_id|ConvertTo-Json -Compress
    $version=$script:fixture.product_version_id|ConvertTo-Json -Compress
    $enterprise=$script:fixture.enterprise_id|ConvertTo-Json -Compress
    $buyer=$script:fixture.actors.super.user_id|ConvertTo-Json -Compress
    $proof=Js "fetch('/api/orders/'+$id,{headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}}).then(async r=>{const d=await r.json(),o=d.order;return {ok:r.status===200&&o?.id===$id&&o.product_id===$product&&o.product_version_id===$version&&o.buyer_enterprise_id===$enterprise&&o.buyer_user_id===$buyer&&o.order_no!=='ORD-261012005144E1B3',subscription:o?.subscription_id||'',orderNo:o?.order_no||'',paid:o?.payment_status==='paid',delivered:['accepted','completed'].includes(o?.delivery_status)}})"
    if(-not $proof.ok){throw 'Exact owned order proof failed'}
    $script:subscriptionId=[string]$proof.subscription
    if($script:subscriptionId -and $script:subscriptionId -notmatch '^[a-f0-9]{32}$'){throw 'Invalid owned subscription ID'}
    $no=$proof.orderNo|ConvertTo-Json -Compress
    WaitJs "[...document.querySelectorAll('.order-no')].some(n=>n.textContent.trim()===$no)" 60
    Js "(()=>{const c=[...document.querySelectorAll('.order-no')].find(n=>n.textContent.trim()===$no);c.closest('tr').querySelector('button[title=\u67e5\u770b\u8ba2\u5355]').click();return true})()"|Out-Null
    WaitJs '!!document.querySelector(".order-drawer .state-grid")'
    return $proof
}
$script:socket=$null;$script:chrome=$null;$script:commandId=0
$script:blocked=0;$script:nativeDialogs=0;$script:publicAuth=0;$script:orderId='';$script:subscriptionId='';$script:orderPosts=0
$script:startupNotifications=$false;$script:orderObserverInstalled=$false
$script:startupNotificationDeadline=[DateTime]::MinValue
$script:deniedRequests=@()
$script:quoteId=''
$profile=Join-Path $env:TEMP ('market-enterprise-trade-'+[Guid]::NewGuid().ToString('N'))
$result=@{status='pending';stage='private fixture';limitation='Synthetic pending_integration approval precondition; not four-review publication acceptance'}
$artifactDir=''
try {
    $description=& wsl.exe --exec stat --printf '%a|%F' -- $FixtureFile 2>$null
    if($LASTEXITCODE -ne 0 -or [string]$description -cne '600|regular file'){throw 'Private regular 0600 fixture required'}
    $winFile=& wsl.exe --exec wslpath -w $FixtureFile
    $script:fixture=Get-Content -LiteralPath $winFile -Raw|ConvertFrom-Json
    $run=$script:fixture.run_id;$kind=$script:fixture.kind
    if($kind -ceq 'saas'){
        $valid=$run -cmatch '^qa-vcs-saas-[0-9a-f]{12}$' -and $script:fixture.product_id -ceq ($run+'-p-saas') -and $script:fixture.product_version_id -ceq ($run+'-v-source') -and $script:fixture.enterprise_id -ceq ($run+'-e-buyer')
    } else {
        $index=0;if($kind -ceq 'model-serving'){$index=1}
        $valid=$kind -cin @('api','model-serving') -and $run -cmatch '^qa-api-auto-[0-9a-f]{12}$' -and $script:fixture.product_id -ceq ($run+'-p'+$index) -and $script:fixture.product_version_id -ceq ($run+'-v'+$index) -and $script:fixture.enterprise_id -ceq ($run+'-buy')
    }
    if(-not $valid){throw 'Unexpected owned fixture roots'}
    $superId=$run+'-buyer';$adminId=$run+'-ops'
    if($kind -ceq 'saas'){$superId=$run+'-u-buyer';$adminId=$run+'-u-delivery'}
    if($script:fixture.actors.super.user_id -cne $superId -or $script:fixture.actors.admin.user_id -cne $adminId){throw 'Unexpected fixture buyer actors'}
    $BaseUrl=[string]$script:fixture.base_url;$uri=[Uri]$BaseUrl
    if($uri.Scheme -cnotin @('http','https') -or $uri.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $uri.UserInfo){throw 'Exact fixture HTTP origin required'}
    $artifactDir=Join-Path $OutputDir ($run+'-'+[Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $artifactDir -Force|Out-Null
    $result.stage='owned Chrome';Connect-OwnedChrome
    Install-OwnedOrderObserver
    $result.stage='buyer login';Login-Actor $BuyerRole
    $product=$script:fixture.product_id|ConvertTo-Json -Compress
    $enterprise=$script:fixture.enterprise_id|ConvertTo-Json -Compress
    $existing=Js "fetch('/api/orders',{headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}}).then(async r=>{const d=await r.json();return {ok:r.status===200,count:(d.items||[]).filter(o=>o.product_id===$product||o.product_name===$product).length}})"
    if(-not $existing.ok -or $existing.count){throw 'Fresh orderless fixture required; journal and clean before retry'}
    $result.stage='storefront UI purchase'
    Cdp 'Page.navigate' @{url="$BaseUrl/products/$($script:fixture.product_id)"}|Out-Null
    WaitJs '!!document.querySelector(".store-checkout-field select")' 60
    Js "(()=>{const s=document.querySelector('.store-checkout-field select');s.value=$enterprise;s.dispatchEvent(new Event('change',{bubbles:true}));return true})()"|Out-Null
    WaitJs '!!document.querySelector(".store-quote-amount") && !document.querySelector(".store-version-tool button.primary-btn.full-btn").disabled' 60
    if($BuyerRole -ceq 'admin') {
        $result.adminQuoteAndSubscribeEligible=$true
        Login-Actor 'super'
        $result.stage='super storefront navigation'
        Cdp 'Page.navigate' @{url="$BaseUrl/products/$($script:fixture.product_id)"}|Out-Null
        WaitJs '!!document.querySelector(".store-checkout-field select")' 60
        $result.stage='super storefront enterprise selection'
        Js "(()=>{const s=document.querySelector('.store-checkout-field select');s.value=$enterprise;s.dispatchEvent(new Event('change',{bubbles:true}));return true})()"|Out-Null
        $result.stage='super storefront quote wait'
        WaitJs '!!document.querySelector(".store-quote-amount") && !document.querySelector(".store-version-tool button.primary-btn.full-btn").disabled' 60
    }
    if(-not $script:orderObserverInstalled){throw 'Owned order observer was not installed before purchase'}
    $result.stage='super exact quote response capture'
    $script:quoteId=Js 'window.__enterpriseTradeQuoteId||""'
    if($script:quoteId -cnotmatch '^[a-f0-9]{64}$'){throw 'Exact fixture quote response was not captured'}
    $result.stage='super storefront purchase click'
    Start-BuyerNavigation
    Js 'document.querySelector(".store-version-tool button.primary-btn.full-btn").click();true'|Out-Null
    $result.stage='exact owned order capture wait'
    WaitJs '(()=>{try{return /^[a-f0-9]{32}$/.test(JSON.parse(sessionStorage.getItem("market_enterprise_trade_order")||"{}").id)}catch{return false}})()' 60
    $script:orderId=Js 'JSON.parse(sessionStorage.getItem("market_enterprise_trade_order")).id'
    Finish-BuyerNavigation
    $result.stage='super-admin UI payment';Login-Actor 'super'
    $proof=Open-OwnedOrder
    if($proof.paid){throw 'Fresh purchase was already paid'}
    Click-Action ([regex]::Unescape('\u53d1\u8d77\u6a21\u62df\u652f\u4ed8'))
    Click-Action ([regex]::Unescape('\u6a21\u62df\u786e\u8ba4\u652f\u4ed8'))
    $result.stage='bounded real worker observation'
    for($attempt=0;$attempt -lt 36;$attempt++) {
        Navigate-BuyerConsole
        Finish-BuyerNavigation
        $proof=Open-OwnedOrder
        if($proof.paid -and $proof.delivered){break}
        Start-Sleep -Seconds 5
    }
    if(-not ($proof.paid -and $proof.delivered)){throw 'Worker delivery timeout'}
    if($script:blocked -or $script:nativeDialogs -or $script:publicAuth){throw 'Unexpected request or dialog blocked'}
    $result.status='passed';$result.buyerRole=$BuyerRole;$result.loginMode=$LoginMode
    $result.orderId=$script:orderId;$result.kind=$kind
    $result.checks=@('real storefront quote and super-admin purchase UI','exact owned order identity','super-admin start and confirm payment UI','real worker paid and delivered state','no response mock or secret download')
} catch {
    $result.status='failed';$result.error_type=$_.Exception.GetType().Name
    $result.error_line=[int]$_.InvocationInfo.ScriptLineNumber
} finally {
    $result.blockedRequests=$script:blocked;$result.nativeDialogs=$script:nativeDialogs
    $result.deniedRequests=@($script:deniedRequests)
    if($artifactDir){$result|ConvertTo-Json -Depth 6|Set-Content -Encoding UTF8 (Join-Path $artifactDir 'browser-results.json')}
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue
    $script:fixture=$null
}
$result|ConvertTo-Json -Depth 6 -Compress
if($result.status -cne 'passed'){exit 1}
