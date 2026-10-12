param(
    [Parameter(Mandatory=$true)][string]$FixtureFile,
    [string]$BaseUrl='http://192.168.10.10:30080',
    [string]$OutputDir="$env:TEMP\market-api-initial-browser"
)
$ErrorActionPreference='Stop'
$socket=$null; $chrome=$null
$profile=Join-Path $env:TEMP ('market-api-initial-'+[Guid]::NewGuid().ToString('N'))
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$script:commandId=0; $script:blockedRequests=0; $script:nativeDialogs=0
$result=@{status='pending';checks=@();startupDiagnostics=@{};scope='Real browser checkout, simulated payment, deployed worker and credential download; approved fixture products, not product review'}
$stage='startup'
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'unified_delivery_browser_cdp.ps1'),[ref]$null,[ref]$null)
foreach($name in @('Send-Cdp','Cdp','Js','WaitJs','Assert-Browser','Connect-OwnedChrome')) {
    $fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true)
    if(-not $fn){throw 'Missing owned browser utility'}
    . ([ScriptBlock]::Create($fn.Extent.Text))
}
function Allowed-Request([string]$method,[string]$url) {
    try {
        $uri=[Uri]$url
        if($uri.GetLeftPart([UriPartial]::Authority) -ne $BaseUrl -or $uri.UserInfo){return $false}
        if($method -in @('GET','HEAD','OPTIONS')) {return $true}
        return $method -eq 'POST' -and ($uri.AbsolutePath -in @('/api/orders','/api/orders/quote') -or $uri.AbsolutePath -match '^/api/orders/[a-f0-9]{32}/transition$')
    } catch {return $false}
}
function Screenshot([string]$name) {
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
}
function Click-Action([string]$text) {
    $encoded=$text|ConvertTo-Json -Compress
    WaitJs "(()=>{const b=[...document.querySelectorAll('.order-drawer .action-list button')].find(n=>n.textContent.trim().startsWith($encoded));return !!b&&!b.disabled})()"
    Js "(()=>{const b=[...document.querySelectorAll('.order-drawer .action-list button')].find(n=>n.textContent.trim().startsWith($encoded));if(!b||b.disabled)throw Error('Action unavailable');b.click();return true})()"|Out-Null
    WaitJs '!!document.querySelector(".action-dialog")'
    Js 'document.querySelector(".action-dialog button.primary-btn").click();true'|Out-Null
    WaitJs '!document.querySelector(".action-dialog")'
}
try {
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $description=& wsl.exe --exec stat --printf '%a|%F' -- $FixtureFile 2>$null
    if($LASTEXITCODE -ne 0 -or [string]$description -cne '600|regular file'){throw 'Private fixture must be a regular 0600 file'}
    $winFile=& wsl.exe --exec wslpath -w $FixtureFile
    $fixture=Get-Content -LiteralPath $winFile -Raw|ConvertFrom-Json
    if($fixture.run_id -notmatch '^qa-api-auto-[a-f0-9]{12}$' -or $fixture.orders.Count -ne 2 -or -not $fixture.token){throw 'Unexpected private fixture'}
    Connect-OwnedChrome
    Cdp 'Browser.setDownloadBehavior' @{behavior='allow';downloadPath=(Join-Path $profile 'downloads')}|Out-Null
    $tokenJson=$fixture.token|ConvertTo-Json -Compress
    $productsJson=@($fixture.orders|ForEach-Object {$_.product_id})|ConvertTo-Json -Compress
    $enterpriseJson=$fixture.enterprise_id|ConvertTo-Json -Compress
    $source=@'
(() => {
localStorage.setItem('market_token',__TOKEN__);
const products=__PRODUCTS__,enterprise=__ENTERPRISE__;
const t=window.__initialProbe={created:null,owned:[],payments:[],downloads:0,leaks:0,publicAuth:0,blocked:0,orderStatus:null};
const hidden=o=>o&&typeof o==='object'&&Object.entries(o).some(([k,v])=>['secret_encrypted','unit_cost','unit_cost_snapshot','total_cost','total_cost_snapshot','cost','profit','upstream_client_secret','upstream_url'].includes(k)||hidden(v));
const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send,header=XMLHttpRequest.prototype.setRequestHeader;
XMLHttpRequest.prototype.open=function(m,u,...rest){this.probeMethod=m.toUpperCase();this.probePath=new URL(u,location.origin).pathname;return open.call(this,m,u,...rest)};
XMLHttpRequest.prototype.setRequestHeader=function(n,v){if(n.toLowerCase()==='authorization')this.probeAuth=true;return header.call(this,n,v)};
XMLHttpRequest.prototype.send=function(body){
const p=this.probePath,m=this.probeMethod;
if(p.startsWith('/api/storefront/')&&p!=='/api/storefront/buyer-enterprises'&&this.probeAuth)t.publicAuth++;
if(m==='POST'){
 const data=JSON.parse(body||'{}');
 const ok=['/api/orders','/api/orders/quote'].includes(p)?products.includes(data.product_id)&&data.buyer_enterprise_id===enterprise:
 p.startsWith('/api/orders/')&&p.endsWith('/transition')&&t.owned.includes(p.split('/')[3])&&['start_payment','confirm_payment'].includes(data.action);
 if(!ok){t.blocked++;throw Error('Unowned mutation blocked');}
}
this.addEventListener('load',()=>{
 if(p==='/api/orders'&&m==='POST')t.orderStatus=this.status;
 if(this.responseType==='blob'){if(p.endsWith('/download')&&this.status===200)t.downloads++;return;}
 let data;try{data=JSON.parse(this.responseText)}catch{return;}
 if(this.status!==200)return;
 if(p==='/api/orders'&&m==='POST'){if(data.buyer_enterprise_id!==enterprise||!data.id||!data.order_no){t.blocked++;return;}t.created={id:data.id,order_no:data.order_no};t.owned.push(data.id);}
 if(p.endsWith('/transition'))t.payments.push(data.payment_status);
 if((p.startsWith('/api/storefront/')||p==='/api/orders'||(p.startsWith('/api/orders/')&&p.split('/').length===4))&&hidden(data))t.leaks++;
});
return send.call(this,body);
};
})()
'@
    $source=$source.Replace('__TOKEN__',$tokenJson).Replace('__PRODUCTS__',$productsJson).Replace('__ENTERPRISE__',$enterpriseJson)
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$source}|Out-Null
    $tokenJson=$null; $source=$null; $fixture.token=$null
    $views=@(@{name='desktop';width=1440;height=1000;index=0},@{name='mobile';width=390;height=844;index=1})
    foreach($view in $views) {
        $stage=$view.name+'-quote'
        $product=$fixture.orders[$view.index].product_id
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$view.width;height=$view.height;deviceScaleFactor=1;mobile=($view.name -eq 'mobile')}|Out-Null
        Cdp 'Page.navigate' @{url="$BaseUrl/products/$product"}|Out-Null
        WaitJs '!!document.querySelector(".store-checkout-field select option[value]:not([value=\"\"])")'
        $enterprise=$fixture.enterprise_id|ConvertTo-Json -Compress
        Js "(()=>{const s=document.querySelector('.store-checkout-field select');s.value=$enterprise;s.dispatchEvent(new Event('change',{bubbles:true}));return true})()"|Out-Null
        WaitJs '!!document.querySelector(".store-quote-amount") && !document.querySelector(".store-version-tool button.primary-btn.full-btn").disabled'
        Screenshot ($view.name+'-quote.png')
        $stage=$view.name+'-subscribe'
        Js 'document.querySelector(".store-version-tool button.primary-btn.full-btn").click();true'|Out-Null
        WaitJs '!!window.__initialProbe.created && location.pathname==="/console" && !!document.querySelector(".order-no")'
        $stage=$view.name+'-start-payment'
        Js '(()=>{const row=[...document.querySelectorAll(".order-no")].find(n=>n.textContent.trim()===window.__initialProbe.created.order_no)?.closest("tr");if(!row)throw Error("Owned order missing");row.querySelector("button[title=\u67e5\u770b\u8ba2\u5355]").click();return true})()'|Out-Null
        WaitJs '!!document.querySelector(".order-drawer") && [...document.querySelectorAll(".order-drawer button")].some(b=>b.textContent.includes("\u53d1\u8d77\u6a21\u62df\u652f\u4ed8"))'
        Click-Action ([regex]::Unescape('\u53d1\u8d77\u6a21\u62df\u652f\u4ed8'))
        $stage=$view.name+'-confirm-payment'
        WaitJs '[...document.querySelectorAll(".order-drawer button")].some(b=>b.textContent.includes("\u6a21\u62df\u786e\u8ba4\u652f\u4ed8"))'
        Click-Action ([regex]::Unescape('\u6a21\u62df\u786e\u8ba4\u652f\u4ed8'))
        WaitJs 'window.__initialProbe.payments.includes("paid")'
        $stage=$view.name+'-close-paid-order'
        WaitJs '!!document.querySelector("button[aria-label=\u5173\u95ed\u8ba2\u5355\u8be6\u60c5]")'
        Js 'document.querySelector("button[aria-label=\u5173\u95ed\u8ba2\u5355\u8be6\u60c5]").click();true'|Out-Null
        $stage=$view.name+'-wait-delivery'
        # Existing order refresh polling reflects actual deployed worker completion.
        WaitJs '(()=>{const row=[...document.querySelectorAll(".order-no")].find(n=>n.textContent.trim()===window.__initialProbe.created.order_no)?.closest("tr");return row?.querySelectorAll("td")[5]?.textContent.includes("\u5c65\u7ea6\u5b8c\u6210")||row?.querySelectorAll("td")[5]?.textContent.includes("\u9a8c\u6536\u901a\u8fc7")})()'
        Js '(()=>{const row=[...document.querySelectorAll(".order-no")].find(n=>n.textContent.trim()===window.__initialProbe.created.order_no).closest("tr");row.querySelector("button[title=\u67e5\u770b\u8ba2\u5355]").click();return true})()'|Out-Null
        WaitJs '!!document.querySelector("button[title=\u4e0b\u8f7d\u8c03\u7528\u51ed\u636e]")'
        $stage=$view.name+'-download'
        Assert-Browser 'window.__initialProbe.leaks===0 && window.__initialProbe.publicAuth===0 && window.__initialProbe.blocked===0 && !document.querySelector(".api-credential-reveal")' 'Secret, cost or public-auth boundary failed'
        Js 'document.querySelector("button[title=\u4e0b\u8f7d\u8c03\u7528\u51ed\u636e]").scrollIntoView({block:"center"});true'|Out-Null
        Assert-Browser '(()=>{const r=document.querySelector("button[title=\u4e0b\u8f7d\u8c03\u7528\u51ed\u636e]").getBoundingClientRect();return r.width>0&&r.height>0&&r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight})()' 'Download command is outside the visible viewport'
        Screenshot ($view.name+'-credential.png')
        Js 'document.querySelector("button[title=\u4e0b\u8f7d\u8c03\u7528\u51ed\u636e]").click();true'|Out-Null
        WaitJs 'window.__initialProbe.downloads===1'
        $downloadDir=Join-Path $profile 'downloads'
        for($i=0;$i -lt 40 -and -not @(Get-ChildItem $downloadDir -Filter '*.json' -ErrorAction SilentlyContinue).Count;$i++){Start-Sleep -Milliseconds 250}
        $files=@(Get-ChildItem $downloadDir -Filter '*.json')
        if($files.Count -ne 1){throw 'Expected exactly one private credential download'}
        $config=Get-Content -LiteralPath $files[0].FullName -Raw|ConvertFrom-Json
        if($config.api_key -notmatch '^mk_[A-Za-z0-9_-]+$' -or $config.credential_id -ne $fixture.orders[$view.index].credential_id){throw 'Downloaded configuration does not match shared credential'}
        $config=$null
        Remove-Item -LiteralPath $files[0].FullName -Force
        Assert-Browser '!document.querySelector(".api-credential-reveal") && !Object.keys(localStorage).some(k=>/api.?key|secret/i.test(k))' 'Downloaded secret retained in UI or browser storage'
        $result.checks+=($view.name+': real checkout; simulated payment; worker delivery; same shared credential; authenticated JSON download; cost and secret isolation')
    }
    if($script:blockedRequests -or $script:nativeDialogs){throw 'Unexpected browser request or native dialog'}
    $result.status='passed'
}catch{
    $result.status='failed';$result.failure=$_.Exception.Message;$result.stage=$stage
    if($socket){try{$result.diagnostics=Js '(()=>{const t=window.__initialProbe;return {probe:!!t,created:!!t?.created,orderStatus:t?.orderStatus,blocked:t?.blocked,leaks:t?.leaks,publicAuth:t?.publicAuth,paid:t?.payments.includes("paid"),console:location.pathname==="/console",orders:document.querySelectorAll(".order-no").length,drawer:!!document.querySelector(".order-drawer"),login:!!document.querySelector(".login-shell")}})()';Screenshot 'failure.png'}catch{}}
    throw
}
finally {
    $result|ConvertTo-Json -Depth 8|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')
    if($socket){$socket.Dispose()}
    if($chrome){Stop-Process -Id $chrome.Id -Force -ErrorAction SilentlyContinue}
    Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue
}
