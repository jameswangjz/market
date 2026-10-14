param([Parameter(Mandatory=$true)][string]$FixtureFile,
      [Parameter(Mandatory=$true)][string]$OutputDir)
$ErrorActionPreference='Stop'
$socket=$null; $chrome=$null
$profile=Join-Path $env:TEMP ('market-r2-readonly-'+[Guid]::NewGuid().ToString('N'))
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$script:commandId=0; $script:blockedRequests=0; $script:nativeDialogs=0
$result=@{status='pending';scope='Deployed UI read-only inspection; no SaaS synchronization mutations';checks=@();startupDiagnostics=@{}}
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'unified_delivery_browser_cdp.ps1'),[ref]$null,[ref]$null)
foreach($name in @('Resolve-PrivatePosixFile','Send-Cdp','Cdp','Js','WaitJs','Assert-Browser','Connect-OwnedChrome')) {
    $fn=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true)
    if(-not $fn){throw "Missing browser utility $name"}
    . ([ScriptBlock]::Create($fn.Extent.Text))
}
function Allowed-Request([string]$method,[string]$url) {
    $uri=[Uri]$url
    return $uri.GetLeftPart([UriPartial]::Authority) -eq 'http://192.168.10.10:30080' -and $method -in @('GET','HEAD','OPTIONS')
}
function Set-Actor([string]$token) {
    if($script:injectId){Cdp 'Page.removeScriptToEvaluateOnNewDocument' @{identifier=$script:injectId}|Out-Null}
    $quoted=ConvertTo-Json -InputObject $token -Compress
    $script:injectId=(Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source="if(location.origin==='http://192.168.10.10:30080'){localStorage.setItem('market_token',$quoted)}"}).identifier
}
function Shot([string]$name) {
    Js 'new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))'|Out-Null
    Assert-Browser '!document.querySelector("input[type=password]")' 'Secret input must not be captured'
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir ($name+'.png')),[Convert]::FromBase64String($shot.data))
}
try {
    New-Item -ItemType Directory -Path $OutputDir -Force|Out-Null
    if(@(Get-ChildItem -LiteralPath $OutputDir -Force).Count){throw 'Fresh output directory required'}
    $fixture=Get-Content -Raw (Resolve-PrivatePosixFile $FixtureFile)|ConvertFrom-Json
    Connect-OwnedChrome
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Set-Actor $fixture.tenantToken
    $result.stage='buyer order list'
    Cdp 'Page.navigate' @{url=('http://192.168.10.10:30080/console?view=orders&enterprise_id='+$fixture.buyerEnterpriseId)}|Out-Null
    $order=ConvertTo-Json -InputObject $fixture.orderNo -Compress
    WaitJs "[...document.querySelectorAll('.order-no')].some(n=>n.textContent.trim()===$order)"
    $result.stage='buyer order drawer'
    Js "[...document.querySelectorAll('.order-no')].find(n=>n.textContent.trim()===$order).closest('tr').querySelectorAll('button').forEach(b=>{if(b.title==='\u67e5\u770b\u8ba2\u5355')b.click()});true"|Out-Null
    WaitJs '[...document.querySelectorAll(".order-drawer button")].some(b=>b.textContent.trim()==="\u7528\u6237\u7ba1\u7406")'
    $result.stage='SaaS sync modal'
    Assert-Browser '![...document.querySelectorAll(".order-drawer button")].some(b=>/\u5173\u95ed\u79df\u6237|\u6062\u590d\u79df\u6237/.test(b.textContent))' 'Tenant lifecycle buttons must be absent'
    Js '[...document.querySelectorAll(".order-drawer button")].find(b=>b.textContent.trim()==="\u7528\u6237\u7ba1\u7406").click();true'|Out-Null
    WaitJs '!!document.querySelector(".saas-sync-management .saas-sync-columns")&&document.querySelectorAll(".saas-sync-tree").length===2&&!document.querySelector(".saas-sync-management [aria-busy=true]")'
    Assert-Browser '(()=>{const e=document.querySelector(".saas-sync-modal-card");const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+40))})()' 'SaaS dialog must be in front of the order drawer'
    Assert-Browser '[...document.querySelectorAll(".saas-sync-row")].every(r=>{const c=getComputedStyle(r);return c.flexDirection==="row"&&c.marginTop==="0px"&&c.marginBottom==="0px"})' 'Tree rows must remain compact and horizontal inside a modal'
    Shot 'saas-sync-desktop'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=1;mobile=$true}|Out-Null
    Shot 'saas-sync-mobile'
    $result.checks+='Buyer order opens two-tree user management; tenant lifecycle buttons absent; desktop/mobile screenshots'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Set-Actor $fixture.adminToken
    $result.stage='category settings'
    Cdp 'Page.navigate' @{url='http://192.168.10.10:30080/console?view=settings'}|Out-Null
    WaitJs 'Number(document.querySelector(".sidebar .nav-badge")?.textContent)>0 && !document.querySelector(".loading-line")'
    Start-Sleep -Milliseconds 1500
    WaitJs '[...document.querySelectorAll(".sidebar button")].some(b=>b.textContent.trim()==="\u7cfb\u7edf\u8bbe\u7f6e")'
    Js '[...document.querySelectorAll(".sidebar button")].find(b=>b.textContent.trim()==="\u7cfb\u7edf\u8bbe\u7f6e").click();true'|Out-Null
    WaitJs '[...document.querySelectorAll("button")].some(b=>b.textContent.trim()==="\u4ea7\u54c1\u76ee\u5f55")'
    Js '[...document.querySelectorAll("button")].find(b=>b.textContent.trim()==="\u4ea7\u54c1\u76ee\u5f55").click();true'|Out-Null
    WaitJs '!!document.querySelector(".category-settings")'
    Js 'new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))'|Out-Null
    Assert-Browser '!!document.querySelector(".category-settings") && !document.querySelector(".metric-grid")' 'Category settings must remain visible after rendering'
    Shot 'categories-desktop'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=1;mobile=$true}|Out-Null
    Shot 'categories-mobile'
    $result.checks+='Platform category settings visible on desktop/mobile without mutations'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Set-Actor $fixture.providerToken
    $result.stage='provider catalog'
    Cdp 'Page.navigate' @{url=('http://192.168.10.10:30080/console?view=products&enterprise_id='+$fixture.providerEnterpriseId)}|Out-Null
    WaitJs 'document.body.textContent.includes("SaaS-APP1")'
    $result.checks+='Provider catalog contains SaaS-APP1 under validated enterprise context'
    WaitJs 'Number(document.querySelector(".sidebar .nav-badge")?.textContent)>0 && !document.querySelector(".loading-line")'
    Start-Sleep -Milliseconds 1500
    Js '[...document.querySelectorAll(".sidebar button")].find(b=>b.textContent.trim()==="\u6e05\u7b97\u5206\u8d26").click();true'|Out-Null
    WaitJs 'document.querySelector("h1")?.textContent.includes("\u4f01\u4e1a\u6e05\u7b97") && !document.querySelector(".loading-line")'
    Assert-Browser '!!document.querySelector("main .data-table") && ![...document.querySelectorAll("main button")].some(b=>/\u751f\u6210\u6307\u5b9a\u6708\u4efd|\u652f\u4ed8\u6279\u6b21/.test(b.textContent))' 'Provider must not receive platform batch funding actions'
    Shot 'provider-settlements-desktop'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=1;mobile=$true}|Out-Null
    Shot 'provider-settlements-mobile'
    $result.checks+='Provider settlement navigation and read-only list inspected; proposals and locking not mutated'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Js '[...document.querySelectorAll(".sidebar button")].find(b=>b.textContent.trim().startsWith("\u6d88\u606f\u4e2d\u5fc3")).click();true'|Out-Null
    WaitJs '!!document.querySelector(".message-center") && document.querySelector(".message-center [aria-busy]")?.getAttribute("aria-busy")==="false"'
    Assert-Browser '!document.querySelector(".message-center [role=alert]")' 'Scoped message center must load without errors'
    Shot 'provider-messages-desktop'
    $result.checks+='Current-enterprise provider message center loaded without reading or deleting individual messages'
    if($script:nativeDialogs -or $script:blockedRequests){throw 'Unexpected browser mutation or native dialog'}
    $result.status='passed'
}catch{
    $result.status='failed';$result.failure=$_.Exception.Message
    if($socket){$result.diagnostics=Js '({path:location.pathname,orderRows:document.querySelectorAll(".order-no").length,drawer:!!document.querySelector(".order-drawer"),buttons:[...document.querySelectorAll(".order-drawer button")].map(b=>b.textContent.trim())})';Shot 'failure'}
    throw
}
finally{
    $result|ConvertTo-Json -Depth 8|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
    if($socket){$socket.Dispose()}
    if($chrome){Stop-Process -Id $chrome.Id -Force -ErrorAction SilentlyContinue}
    Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue
}
