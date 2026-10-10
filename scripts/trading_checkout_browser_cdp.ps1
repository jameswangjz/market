[CmdletBinding(DefaultParameterSetName='TokenFile')]
param(
    [string]$BaseUrl = 'http://192.168.10.10:30080',
    [Parameter(Mandatory=$true, ParameterSetName='Token')][string]$Token,
    [Parameter(ParameterSetName='TokenFile')][string]$TokenFile = "$env:TEMP\trd-phase2-fixture.json",
    [Parameter(Mandatory=$true)][string]$ProductId,
    [string]$OutputDir = "$env:TEMP\market-trading-checkout-browser",
    [switch]$CreateOrder
)
$ErrorActionPreference = 'Stop'
if ($PSCmdlet.ParameterSetName -eq 'TokenFile') {
    try { $Token = [string](Get-Content -LiteralPath $TokenFile -Raw | ConvertFrom-Json).token }
    catch { throw 'Cannot read token from TokenFile JSON (details suppressed)' }
}
# Use an isolated disposable Chrome profile; never put the JWT in process arguments.
$site = [Uri]$BaseUrl
if ($site.Scheme -notin @('http', 'https') -or $site.UserInfo -or $site.Query -or $site.Fragment -or $site.AbsolutePath -ne '/') {
    throw 'BaseUrl must be an HTTP(S) origin without credentials, query or path'
}
if ([string]::IsNullOrWhiteSpace($Token) -or [string]::IsNullOrWhiteSpace($ProductId)) { throw 'Token and ProductId are required' }
$BaseUrl = $site.GetLeftPart([UriPartial]::Authority)
$detailPath = '/products/' + [Uri]::EscapeDataString($ProductId)
$detailPathJson = $detailPath | ConvertTo-Json -Compress
$profile = Join-Path $env:TEMP ('market-trading-checkout-' + [Guid]::NewGuid().ToString('N'))
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
$listener.Stop()
$socket = $null
$chrome = $null
$stage = 'initialization'
$checks = @()
$result = [ordered]@{ status='pending'; productId=$ProductId; createOrder=[bool]$CreateOrder; orderId=$null; checks=@(); limits=@('No payment requests', 'Fixture creation and cleanup are owned by the main agent', 'Monthly browser checks run only for a supplied monthly product; file/offline fixtures do not cover monthly UI') }

function Save-Result {
    $result.checks = @($checks)
    $result | ConvertTo-Json -Depth 12 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')
}

function Assert-Browser([string]$expression, [string]$message) {
    if (-not (Js $expression)) { throw $message }
}

function Set-Months([int]$months) {
    Js "(()=>{const input=document.querySelector('.store-checkout-field input[type=number]');if(!input)throw Error('Missing months input');input.value='$months';input.dispatchEvent(new Event('input',{bubbles:true}));return true})()" | Out-Null
}

function Wait-Quote([int]$months) {
    WaitJs "(()=>{const t=window.__checkoutTest,q=t.quotes.at(-1),s=document.querySelector('.store-checkout-field select'),v=document.querySelector('.store-version-tool > label select');return !!q&&q.status===200&&q.subscription_months===$months&&q.buyer_enterprise_id===s?.value&&q.product_version_id===v?.value&&document.querySelector('.store-quote')?.getAttribute('aria-busy')==='false'&&!!document.querySelector('.store-quote-amount')&&!document.querySelector('.store-checkout-error')&&!document.querySelector('.store-version-tool button.primary-btn.full-btn')?.disabled})()"
    Assert-Browser '(()=>{const q=window.__checkoutTest.quotes.at(-1),fmt=n=>"\u00a5"+Number(n).toLocaleString("zh-CN",{minimumFractionDigits:2,maximumFractionDigits:2}),lines=[...document.querySelectorAll(".store-quote-line strong")];return q.currency==="CNY"&&q.hasQuoteId&&document.querySelector(".store-quote-amount").textContent.trim()===fmt(q.amount)&&lines[0]?.textContent.trim()===fmt(q.unit_price)&&(q.billing_unit!=="month"||lines[1]?.textContent.trim()===q.subscription_months+" \u4e2a\u6708")})()' 'Displayed quote differs from the actual server response'
}

try {
    New-Item -ItemType Directory -Force $OutputDir | Out-Null
    $chrome = Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' -ArgumentList @('--headless=new', "--remote-debugging-port=$port", "--user-data-dir=`"$profile`"", '--no-first-run', 'about:blank') -PassThru
    $stage = 'Chrome CDP connection'
    $connected = $false
    for ($i=0; $i -lt 60; $i++) {
        try {
            $targets = Invoke-RestMethod "http://127.0.0.1:$port/json/list"
            if (@($targets | Where-Object { $_.type -eq 'page' }).Count) { $connected = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $connected) { throw 'Chrome CDP startup timed out' }
    # Reuse the same CDP transport as the existing storefront/dialog acceptance scripts.
    $helper = Get-Content -Raw (Join-Path $PSScriptRoot 'message_center_browser_cdp.ps1')
    $start = $helper.IndexOf("`$ErrorActionPreference = 'Stop'")
    $end = $helper.IndexOf('New-Item -ItemType Directory -Force $OutputDir')
    if ($start -lt 0 -or $end -le $start) { throw 'CDP helper boundaries changed' }
    Invoke-Expression ($helper.Substring($start, $end-$start).Replace('127.0.0.1:9222', "127.0.0.1:$port"))
    # Runtime exceptions can include evaluated source. Suppress them so JWT injection
    # can never be echoed by the shared helper, a timeout, or an artifact.
    function Js([string]$expression) {
        try { $response = Cdp 'Runtime.evaluate' @{expression=$expression;awaitPromise=$true;returnByValue=$true} }
        catch { throw 'Browser evaluation failed (details suppressed)' }
        if ($response.exceptionDetails) { throw 'Browser evaluation failed (details suppressed)' }
        return $response.result.value
    }
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false} | Out-Null
    $allowOrderJson = ([bool]$CreateOrder).ToString().ToLowerInvariant()
    $instrumentation = @'
(() => {
  const allowOrder = __ALLOW_ORDER__;
  const t = window.__checkoutTest = {quotes:[], orders:[], orderAttempts:0, blockedMutations:0, publicAuth:0};
  const path = url => new URL(url, location.origin).pathname;
  const isPublic = url => /^\/api\/storefront\/products(?:\/|$)/.test(path(url));
  const allowed = (method, url) => {
    if (!path(url).startsWith('/api/') || ['GET','HEAD','OPTIONS'].includes(method)) return true;
    if (method === 'POST' && path(url) === '/api/orders/quote') return true;
    if (method === 'POST' && path(url) === '/api/orders') {
      t.orderAttempts++;
      if (allowOrder && t.orderAttempts === 1) return true;
    }
    t.blockedMutations++;
    return false;
  };
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send, header = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function(method, url, ...args) {
    this.checkoutMethod = method.toUpperCase(); this.checkoutUrl = url; this.checkoutAuth = false;
    return open.call(this, method, url, ...args);
  };
  XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
    if (name.toLowerCase() === 'authorization') this.checkoutAuth = true;
    return header.call(this, name, value);
  };
  XMLHttpRequest.prototype.send = function(body) {
    if (isPublic(this.checkoutUrl) && this.checkoutAuth) t.publicAuth++;
    if (!allowed(this.checkoutMethod, this.checkoutUrl)) throw Error('Mutation blocked by checkout acceptance script');
    const endpoint = path(this.checkoutUrl);
    if (this.checkoutMethod === 'POST' && ['/api/orders/quote','/api/orders'].includes(endpoint)) {
      const request = JSON.parse(body);
      this.addEventListener('load', () => {
        let data; try { data = JSON.parse(this.responseText); } catch { data = {}; }
        if (endpoint === '/api/orders/quote') t.quotes.push({
          status:this.status, product_id:data.product_id, product_version_id:data.product_version_id,
          buyer_enterprise_id:request.buyer_enterprise_id, subscription_months:data.subscription_months,
          unit_price:data.unit_price, amount:data.amount, currency:data.currency, billing_unit:data.billing_unit,
          hasQuoteId:typeof data.quote_id==='string'&&!!data.quote_id, authenticated:this.checkoutAuth
        });
        else t.orders.push({status:this.status, id:data.id, order_no:data.order_no,
          hasQuoteId:typeof request.quote_id==='string'&&!!request.quote_id});
      });
    }
    return send.call(this, body);
  };
  const originalFetch = window.fetch;
  window.fetch = function(input, init) {
    const request = new Request(input, init);
    if (isPublic(request.url) && request.headers.has('authorization')) t.publicAuth++;
    if (!allowed(request.method, request.url)) return Promise.reject(Error('Mutation blocked by checkout acceptance script'));
    return originalFetch.call(this, input, init);
  };
})();
'@
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$instrumentation.Replace('__ALLOW_ORDER__', $allowOrderJson)} | Out-Null

    $stage = 'anonymous detail and real product logo'
    Cdp 'Page.navigate' @{url="$BaseUrl$detailPath"} | Out-Null
    WaitJs '!!document.querySelector(".store-product-heading") && !!document.querySelector(".store-version-tool a.primary-btn.full-btn")'
    Assert-Browser '!localStorage.getItem("market_token") && !document.querySelector(".store-checkout-field select") && !document.querySelector(".store-quote-amount")' 'Anonymous detail unexpectedly has purchase context'
    WaitJs '(()=>{const i=document.querySelector(".store-product-heading .store-logo img");return !!i&&!i.hidden&&i.complete&&i.naturalWidth>0&&i.naturalHeight>0})()'
    $logo = Js '(()=>{const i=document.querySelector(".store-product-heading .store-logo img");return {loaded:!i.hidden&&i.complete&&i.naturalWidth>0,width:i.naturalWidth,height:i.naturalHeight}})()'
    Assert-Browser 'window.__checkoutTest.publicAuth===0 && window.__checkoutTest.orderAttempts===0 && window.__checkoutTest.quotes.length===0' 'Anonymous browsing sent an authenticated public request or purchase request'
    $checks += 'anonymous detail without enterprise or quote; actual product logo loaded'
    Screenshot 'checkout-anonymous-desktop.png'
    $stage = 'anonymous subscription login return target'
    Assert-Browser "(()=>{const a=document.querySelector('.store-version-tool a.primary-btn.full-btn'),u=new URL(a.href);return u.pathname==='/console'&&u.searchParams.get('returnTo')===$detailPathJson})()" 'Subscription login link has an incorrect return target'
    Js 'document.querySelector(".store-version-tool a.primary-btn.full-btn").click();true' | Out-Null
    WaitJs 'location.pathname==="/console" && !!document.querySelector("input[autocomplete=username]")'
    Assert-Browser "new URLSearchParams(location.search).get('returnTo')===$detailPathJson" 'Login navigation lost the detail return target'
    Screenshot 'checkout-login-guide.png'
    $checks += 'anonymous subscribe opens console login with detail return target'

    $stage = 'isolated JWT injection'
    $tokenJson = $Token | ConvertTo-Json -Compress
    Js "(()=>{localStorage.setItem('market_token',$tokenJson);return true})()" | Out-Null
    $tokenJson = $null
    $Token = $null
    Cdp 'Page.navigate' @{url="$BaseUrl$detailPath"} | Out-Null
    $stage = 'authenticated explicit enterprise selection'
    WaitJs '!!document.querySelector(".store-checkout-field select") && !document.querySelector(".store-version-tool [role=status]")'
    Assert-Browser 'document.querySelector(".store-checkout-field select").value==="" && document.querySelector(".store-version-tool button.primary-btn.full-btn").disabled && !document.querySelector(".store-quote-amount") && window.__checkoutTest.quotes.length===0' 'Enterprise must remain unselected and ordering disabled until explicit selection'
    Js "(async()=>{const detail=await fetch('/api/storefront/products/'+encodeURIComponent($((ConvertTo-Json -InputObject $ProductId -Compress))));if(!detail.ok)throw Error('Product unavailable');const buyers=await fetch('/api/storefront/buyer-enterprises',{headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}});if(!buyers.ok)throw Error('Buyer fixture unavailable');window.__checkoutFixture={product:await detail.json(),buyers:(await buyers.json()).items};return true})()" | Out-Null
    $monthly = Js 'window.__checkoutFixture.product.delivery_category!=="offline" && ["api","model_api","tenant_access"].includes(window.__checkoutFixture.product.delivery_method)'
    $buyerId = Js '(()=>{const e=window.__checkoutFixture.buyers.find(e=>e.eligible===true&&e.verification_status==="verified"&&["super_admin","enterprise_admin"].includes(e.role));if(!e)throw Error("Fixture needs an eligible buyer enterprise");return e.id})()'
    $buyerJson = $buyerId | ConvertTo-Json -Compress
    Assert-Browser '(()=>{const versions=window.__checkoutFixture.product.versions,s=document.querySelector(".store-version-tool > label select");return [...s.options].every(o=>versions.some(v=>v.id===o.value))&&window.__checkoutFixture.buyers.every(e=>[...document.querySelector(".store-checkout-field select").options].some(o=>o.value===e.id))})()' 'Version IDs or enterprise options differ from the public/authenticated APIs'
    Js "(()=>{const s=document.querySelector('.store-checkout-field select');s.value=$buyerJson;s.dispatchEvent(new Event('change',{bubbles:true}));return true})()" | Out-Null
    Wait-Quote 1
    Assert-Browser '!!document.querySelector(".store-buyer-status") && window.__checkoutTest.quotes.at(-1).authenticated' 'Buyer qualification or authenticated quote missing'
    $checks += 'token detail starts without a default enterprise; explicit eligible selection produces server quote'

    $stage = 'subscription months and quote display'
    if ($monthly) {
        Assert-Browser '(()=>{const i=document.querySelector(".store-checkout-field input[type=number]");return !!i&&i.min==="1"&&i.max==="36"&&i.step==="1"&&i.value==="1"})()' 'Monthly delivery requires a 1..36 integer input'
        foreach ($months in @(36, 1)) { Set-Months $months; Wait-Quote $months }
        foreach ($months in @(0, 37)) {
            $before = Js 'window.__checkoutTest.quotes.length'
            Set-Months $months
            WaitJs '!!document.querySelector(".store-checkout-error") && document.querySelector(".store-version-tool button.primary-btn.full-btn").disabled && !document.querySelector(".store-quote-amount")'
            Assert-Browser "window.__checkoutTest.quotes.length===$before" 'Invalid months should not request a quote'
        }
        Set-Months 1
        Wait-Quote 1
        $checks += 'monthly input bounds 1/36 quoted, 0/37 rejected, server amount/unit price/term displayed'
    } else {
        Assert-Browser '!document.querySelector(".store-checkout-field input[type=number]") && window.__checkoutTest.quotes.at(-1).subscription_months===1 && window.__checkoutTest.quotes.at(-1).billing_unit==="order"' 'One-time delivery must use one month without a monthly input'
        $checks += 'one-time delivery has no months input and quotes one order'
    }
    Screenshot 'checkout-quoted-desktop.png'
    $stage = '390px mobile checkout and logo'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=2;mobile=$true} | Out-Null
    Js 'window.scrollTo(0,0);true' | Out-Null
    WaitJs 'innerWidth===390 && !!document.querySelector(".store-quote-amount")'
    $mobile = Js '(()=>{const p=document.querySelector(".store-version-tool"),r=p.getBoundingClientRect(),i=document.querySelector(".store-product-heading .store-logo img"),outside=[...p.querySelectorAll("select,input,button,.store-quote-line")].some(n=>{const b=n.getBoundingClientRect();return b.left<r.left-1||b.right>r.right+1});return {width:innerWidth,bodyWidth:document.documentElement.scrollWidth,panelLeft:r.left,panelRight:r.right,controlsOutsidePanel:outside,logoLoaded:!!i&&!i.hidden&&i.complete&&i.naturalWidth>0}})()'
    if ($mobile.bodyWidth -gt 391 -or $mobile.panelLeft -lt 0 -or $mobile.panelRight -gt 391 -or $mobile.controlsOutsidePanel -or -not $mobile.logoLoaded) { throw 'Mobile checkout overflows or the actual logo is not loaded' }
    Screenshot 'checkout-mobile-logo.png'
    Js 'document.querySelector(".store-version-tool").scrollIntoView({block:"start"});true' | Out-Null
    Screenshot 'checkout-mobile-quote.png'
    $result.mobile = $mobile
    $result.logo = $logo
    $result.monthly = [bool]$monthly
    $checks += '390px mobile document and checkout controls stay in bounds; actual logo loaded'

    $stage = 'request isolation and optional actual order'
    Assert-Browser 'window.__checkoutTest.publicAuth===0 && window.__checkoutTest.orderAttempts===0 && window.__checkoutTest.blockedMutations===0' 'Unexpected authenticated public request or mutation'
    $result.quote = Js '(()=>{const q=window.__checkoutTest.quotes.at(-1);return {product_id:q.product_id,product_version_id:q.product_version_id,buyer_enterprise_id:q.buyer_enterprise_id,subscription_months:q.subscription_months,unit_price:q.unit_price,amount:q.amount,currency:q.currency,billing_unit:q.billing_unit,hasQuoteId:q.hasQuoteId}})()'
    if ($CreateOrder) {
        # Exactly one UI click. Never automatically retry an order, including a 409.
        Js 'document.querySelector(".store-version-tool button.primary-btn.full-btn").click();true' | Out-Null
        WaitJs 'window.__checkoutTest.orders.length>0'
        $order = Js '(()=>{const o=window.__checkoutTest.orders[0];return {id:o.id,order_no:o.order_no,status:o.status,hasQuoteId:o.hasQuoteId}})()'
        # Persist any returned ID before checking navigation, for fixture cleanup on failure.
        $result.orderId = $order.id
        $result.order = $order
        Save-Result
        if ($order.id) { Write-Output "CreatedOrderId=$($order.id)" }
        if ($order.status -lt 200 -or $order.status -ge 300 -or -not $order.id -or -not $order.hasQuoteId) { throw 'Order not confirmed; inspect the result status and clean up any returned ID' }
        WaitJs 'location.pathname==="/console" && new URLSearchParams(location.search).get("view")==="orders"'
        Assert-Browser 'window.__checkoutTest.orderAttempts===1 && window.__checkoutTest.blockedMutations===0' 'Expected exactly one order and no other mutation'
        $checks += 'one real UI order includes quote_id; response ID captured; navigates to console orders'
    } else {
        $checks += 'CreateOrder disabled; no order was created'
    }
    $result.status = 'passed'
    Save-Result
    Write-Output 'Checkout browser checks passed'
} catch {
    $result.status = 'failed'
    $result.failedStage = $stage
    # Do not serialize exception details, CDP payloads, headers, localStorage or HTML.
    if (Test-Path $OutputDir) { Save-Result }
    throw "Checkout browser checks failed at: $stage (details suppressed; see browser-results.json)"
} finally {
    $tokenJson = $null
    if ($socket) {
        try { Js 'localStorage.removeItem("market_token");true' | Out-Null } catch { }
        $socket.Dispose()
    }
    if ($chrome) {
        Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like "*$profile*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    if (Test-Path $profile) { Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue }
}
