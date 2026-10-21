param([string]$FixtureFile, [string]$OutputDir="$env:TEMP\market-saas-live")
$ErrorActionPreference='Stop'
$socket=$null; $chrome=$null
$profile=Join-Path $env:TEMP ('market-saas-live-'+[Guid]::NewGuid().ToString('N'))
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$script:commandId=0; $script:blockedRequests=0; $script:nativeDialogs=0
$result=@{status='pending'; scope='Live deployed platform and local SaaS; real HTTP, no response mocks'; checks=@(); startupDiagnostics=@{}}
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'unified_delivery_browser_cdp.ps1'),[ref]$null,[ref]$null)
foreach($name in @('Resolve-PrivatePosixFile','Send-Cdp','Cdp','Js','WaitJs','Assert-Browser','Connect-OwnedChrome')) {
    $fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true)
    if(-not $fn){throw "Missing browser utility $name"}
    . ([ScriptBlock]::Create($fn.Extent.Text))
}
function Allowed-Request([string]$method,[string]$url) {
    $uri=[Uri]$url
    if($uri.GetLeftPart([UriPartial]::Authority) -notin @('http://192.168.10.10:30080','http://192.168.10.10:30083')) {return $false}
    if($method -eq 'GET'){return $true}
    return $method -eq 'POST' -and ($uri.AbsolutePath -eq ('/api/orders/'+$fixture.orderId+'/delivery-launch') -or $uri.AbsolutePath.StartsWith('/oidc/') -or $uri.AbsolutePath.StartsWith('/api/oidc/browser/'))
}
function Set-Actor([string]$token) {
    if($script:injectId){Cdp 'Page.removeScriptToEvaluateOnNewDocument' @{identifier=$script:injectId}|Out-Null}
    $quoted=ConvertTo-Json -InputObject $token -Compress
    $source="if(location.origin==='http://192.168.10.10:30080'){localStorage.setItem('market_token',$quoted)}"
    $script:injectId=(Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$source}).identifier
}
function Shot([string]$name) {
    Assert-Browser '!document.querySelector("input[type=password]")' 'Secret input must not be captured'
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir ($name+'.png')),[Convert]::FromBase64String($shot.data))
}
try {
    $fixture=Get-Content -Raw (Resolve-PrivatePosixFile $FixtureFile)|ConvertFrom-Json
    New-Item -ItemType Directory -Path $OutputDir -Force|Out-Null
    Connect-OwnedChrome
    Cdp 'Page.setDownloadBehavior' @{behavior='deny'}|Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=900;deviceScaleFactor=1;mobile=$false}|Out-Null
    Set-Actor $fixture.tenantToken
    Cdp 'Page.navigate' @{url=('http://192.168.10.10:30080/console?view=orders&enterprise_id='+$fixture.buyerEnterpriseId)}|Out-Null
    $order=ConvertTo-Json -InputObject $fixture.orderNo -Compress
    WaitJs "[...document.querySelectorAll('.order-no')].some(n=>n.textContent.trim()===$order)"
    $cell="[...document.querySelectorAll('.order-no')].find(n=>n.textContent.trim()===$order).closest('tr').querySelectorAll('td')[1]"
    WaitJs "$cell.querySelector('button')?.textContent.includes('192.168.10.10:30083')"
    Shot 'order-access'
    Js "$cell.querySelector('button').click();true"|Out-Null
    WaitJs '!!document.querySelector(".oidc-consent footer button.primary-btn")'
    Shot 'oidc-consent'
    Js 'document.querySelector(".oidc-consent footer button.primary-btn").click();true'|Out-Null
    WaitJs 'location.origin==="http://192.168.10.10:30083"&&location.pathname==="/app"&&!!document.querySelector("main h1")'
    Shot 'saas-session'
    $result.checks+= 'Paid order -> authenticated launch -> OIDC consent -> real mock SaaS session'
    Set-Actor $fixture.providerToken
    Cdp 'Page.navigate' @{url=('http://192.168.10.10:30080/console?view=products&enterprise_id='+$fixture.providerEnterpriseId)}|Out-Null
    WaitJs 'document.body.textContent.includes("SaaS-APP1")&&document.body.textContent.includes("\u4e0b\u8f7d\u63a5\u5165\u914d\u7f6e")'
    Shot 'provider-credentials'
    $product=ConvertTo-Json -InputObject $fixture.productId -Compress
    Assert-Browser "(async()=>{const headers={Authorization:'Bearer '+localStorage.getItem('market_token')};const a=await fetch('/api/products/'+$product+'/saas-integration-credentials-download',{headers});if(a.status!==200||a.headers.get('cache-control')!=='no-store')return false;const v=await a.json();const r=v.resource_server;return v.schema_version==='2.0'&&v.product.id===$product&&!('oauth2_api' in v)&&r.authentication==='Bearer'&&r.validation_method==='introspection'&&r.introspection_url===r.issuer.replace(/\/$/,'')+'/oauth/introspect'&&r.audience==='market-resource'&&Array.isArray(r.scopes)&&r.scopes.includes('saas.department')&&!['client_id','client_secret','token_url'].some(k=>k in r)&&!!v.oidc_sso.client_id&&!!v.oidc_sso.client_secret&&v.instructions.length>0})()" 'Unified provider configuration must include resource validation metadata and OIDC login credentials without caching'
    Assert-Browser '[...document.querySelectorAll("tr")].filter(r=>r.textContent.includes("SaaS-APP1")).some(r=>[...r.querySelectorAll("button")].filter(b=>b.textContent.includes("\u4e0b\u8f7d\u63a5\u5165\u914d\u7f6e")).length===1&&!r.textContent.includes("OIDC\u767b\u5f55\u51ed\u636e"))' 'SaaS product must expose exactly one configuration download action'
    Js '[...document.querySelectorAll("tr")].find(r=>r.textContent.includes("SaaS-APP1")).querySelectorAll("button").forEach(b=>{if(b.textContent.includes("\u4e0b\u8f7d\u63a5\u5165\u914d\u7f6e"))b.click()});true'|Out-Null
    WaitJs 'document.body.textContent.includes("SaaS\u5e94\u7528\u63a5\u5165\u914d\u7f6e\u5df2\u4e0b\u8f7d")'
    $result.checks+= 'Provider UI shows one unified configuration action; live download authorized, independent credentials, no-store'
    Set-Actor $fixture.adminToken
    Cdp 'Page.navigate' @{url='http://192.168.10.10:30080/console?view=settings'}|Out-Null
    WaitJs 'Number(document.querySelector(".sidebar .nav-badge")?.textContent)>0 && !document.querySelector(".loading-line")'
    Start-Sleep -Milliseconds 1500
    Js '[...document.querySelectorAll(".sidebar button")].find(b=>b.textContent.trim()==="\u7cfb\u7edf\u8bbe\u7f6e").click();true'|Out-Null
    WaitJs '[...document.querySelectorAll("button")].some(b=>b.textContent.trim()==="\u5e94\u7528\u63a5\u5165")'
    Js '[...document.querySelectorAll("button")].find(b=>b.textContent.trim()==="\u5e94\u7528\u63a5\u5165").click();true'|Out-Null
    WaitJs 'document.body.textContent.includes("\u5141\u8bb8\u5e94\u7528\u63a5\u5165\u5730\u5740\u4f7f\u7528HTTP")&&!!document.querySelector(".checkbox-line input:checked")'
    Shot 'http-setting-desktop'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=1;mobile=$true}|Out-Null
    Shot 'http-setting-mobile'
    $result.checks+= 'Live system configuration HTTP toggle visible on desktop and mobile'
    if($script:nativeDialogs){throw 'Unexpected native browser dialog'}
    $result.status='passed'
}catch{$result.status='failed';$result.failure=$_.Exception.Message;throw}
finally{
    $result|ConvertTo-Json -Depth 8|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
    if($socket){$socket.Dispose()}
    if($chrome){Stop-Process -Id $chrome.Id -Force -ErrorAction SilentlyContinue}
    Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue
}
