param([string]$BaseUrl='http://192.168.10.10:30080', [string]$OutputDir="$env:TEMP\market-order-access-refresh")
$ErrorActionPreference='Stop'
$socket=$null; $chrome=$null
$profile=Join-Path $env:TEMP ('market-access-refresh-'+[Guid]::NewGuid().ToString('N'))
$script:commandId=0; $script:blockedRequests=0; $script:nativeDialogs=0
$script:baseUrl=$BaseUrl
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$result=@{status='pending'; scope='Synthetic HTTP responses; real deployed frontend; no live payment, download, SaaS provisioning or SSO'; startupDiagnostics=@{}; checks=@()}
# Reuse the owned-browser bootstrap, not the previous acceptance entry point.
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'unified_delivery_browser_cdp.ps1'),[ref]$null,[ref]$null)
foreach($name in @('Send-Cdp','Cdp','Js','WaitJs','Assert-Browser','Connect-OwnedChrome')) {
    $fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true)
    if(-not $fn){throw "Missing browser utility $name"}
    . ([ScriptBlock]::Create($fn.Extent.Text))
}
function Allowed-Request([string]$method,[string]$url) {
    $uri=[Uri]$url
    return $method -eq 'GET' -and $uri.GetLeftPart([UriPartial]::Authority) -eq $BaseUrl -and
        ($uri.AbsolutePath -eq '/console' -or $uri.AbsolutePath.StartsWith('/assets/') -or $uri.AbsolutePath -in @('/favicon.ico','/logo.svg'))
}
$instrumentation=@'
(() => {
localStorage.setItem('market_token','synthetic-browser-only');
const probe=window.__refreshProbe={orders:0,posts:[],unexpected:[]};
const enterprise={id:'fixture-enterprise',name:'UI fixture',verification_status:'verified',status:'active',eligible:true};
const user={id:'fixture-user',name:'UI fixture',verified_status:'verified',activation_status:'active',is_active:true,platform_role:'',enterprise_memberships:[{enterprise_id:enterprise.id,status:'active',role:'super_admin'}]};
const base={buyer_enterprise_id:enterprise.id,snapshot_version:1,payment_status:'paid',main_status:'completed',delivery_status:'accepted',refunded_amount:0,amount:10,created_at:'2026-10-01T00:00:00Z'};
const open=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(method,url,...args){this.fixtureMethod=method;this.fixturePath=new URL(url,location.origin).pathname;return open.call(this,method,url,...args)};
XMLHttpRequest.prototype.send=function(){
const path=this.fixturePath,method=this.fixtureMethod;
if(method==='POST'&&path==='/api/orders/fixture-saas/delivery-launch'){probe.posts.push(path);return;}
let body;
if(method!=='GET'){probe.unexpected.push(method+' '+path);throw Error('Fixture denies writes');}
if(path==='/api/auth/me')body={user,enterprise,role:'super_admin',membership:{status:'active'}};
else if(path==='/api/storefront/buyer-enterprises')body={items:[enterprise]};
else if(path==='/api/notifications/summary')body={unread:0,unacknowledged_urgent:0};
else if(path==='/api/notifications/sender-context')body={can_send:false};
else if(path==='/api/orders'){
probe.orders++;
body={items:[{...base,id:'fixture-file',order_no:'ORD-UI-FILE',product_name:'File fixture',delivery_method:'file',...(probe.orders===1?{main_status:'fulfilling',delivery_status:'in_delivery'}:{})},
{...base,id:'fixture-saas',order_no:'ORD-UI-SAAS',product_name:'SaaS fixture',delivery_method:'tenant_access'},
{...base,id:'fixture-pending',order_no:'ORD-UI-PENDING',product_name:'Pending fixture',delivery_method:'tenant_access',main_status:'fulfilling',delivery_status:'in_delivery'}]};
}else if(path==='/api/orders/fixture-saas/delivery-access')body={order_id:'fixture-saas',method:'tenant_access',ready:true,reason:'',files:[],api:null,saas:{subscription_id:'fixture-subscription',launch_path:'/api/orders/fixture-saas/delivery-launch',expires_at:'2030-01-01T00:00:00Z',application_url:'https://app.fixture.invalid/entry'}};
else {probe.unexpected.push(method+' '+path);throw Error('Unknown fixture request');}
Object.defineProperty(this,'readyState',{configurable:true,value:4});
Object.defineProperty(this,'status',{configurable:true,value:200});
Object.defineProperty(this,'statusText',{configurable:true,value:'OK'});
Object.defineProperty(this,'responseText',{configurable:true,value:JSON.stringify(body)});
Object.defineProperty(this,'response',{configurable:true,value:JSON.stringify(body)});
this.getAllResponseHeaders=()=> 'content-type: application/json\r\ncache-control: no-store';
setTimeout(()=>{if(this.onloadend)this.onloadend();else if(this.onreadystatechange)this.onreadystatechange()},0);
};
window.fetch=()=>Promise.reject(Error('Synthetic fixture denies fetch'));
window.EventSource=function(){this.close=()=>{}};
probe.cell=id=>[...document.querySelectorAll('.order-no')].find(n=>n.textContent.trim()===id)?.closest('tr')?.querySelectorAll('td')[1];
})()
'@
try {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
    Connect-OwnedChrome
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$instrumentation}|Out-Null
    foreach($viewport in @(@{name='desktop';width=1440;height=900},@{name='mobile';width=390;height=844})) {
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$viewport.width;height=$viewport.height;deviceScaleFactor=1;mobile=($viewport.name -eq 'mobile')}|Out-Null
        Cdp 'Page.navigate' @{url="$BaseUrl/console?view=orders"}|Out-Null
        WaitJs 'window.__refreshProbe?.cell("ORD-UI-SAAS")?.querySelector("button")?.textContent.includes("https://app.fixture.invalid/entry")'
        Assert-Browser 'window.__refreshProbe.cell("ORD-UI-PENDING").textContent.trim()==="\u4ea4\u4ed8\u4e2d" && !window.__refreshProbe.cell("ORD-UI-PENDING").querySelector("button")' 'Pending SaaS must not be launchable'
        WaitJs 'window.__refreshProbe.orders>=2 && !!window.__refreshProbe.cell("ORD-UI-FILE")?.querySelector("button")'
        Assert-Browser 'window.__refreshProbe.cell("ORD-UI-FILE").querySelector("button").textContent.trim()==="\u4e0b\u8f7d\u6587\u4ef6" && !document.querySelector("[aria-labelledby=delivery-access-title]")' 'Download entry must refresh without opening details or downloading'
        Assert-Browser 'window.__refreshProbe.unexpected.length===0' 'Unexpected fixture API request'
        $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
        [IO.File]::WriteAllBytes((Join-Path $OutputDir ($viewport.name+'.png')),[Convert]::FromBase64String($shot.data))
        Js 'window.__refreshProbe.cell("ORD-UI-SAAS").querySelector("button").click();true'|Out-Null
        WaitJs 'window.__refreshProbe.posts.length===1'
        Assert-Browser 'window.__refreshProbe.posts[0]==="/api/orders/fixture-saas/delivery-launch" && location.pathname==="/console"' 'SaaS entry must use authenticated launch, not an unguarded external link'
        $result.checks+=($viewport.name+': file auto-refresh; actual SaaS address; pending SaaS disabled; authenticated launch request')
    }
    if($script:blockedRequests -or $script:nativeDialogs){throw 'Unexpected external request or native dialog'}
    $result.status='passed'
}catch{$result.status='failed';$result.failure=$_.Exception.Message;throw}
finally{
    $result|ConvertTo-Json -Depth 8|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
    if($socket){$socket.Dispose()}
    if($chrome){Stop-Process -Id $chrome.Id -Force -ErrorAction SilentlyContinue}
    Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue
}
