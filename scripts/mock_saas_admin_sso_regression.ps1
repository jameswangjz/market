param([Parameter(Mandatory=$true)][string]$FixtureFile,
      [Parameter(Mandatory=$true)][string]$OutputDir)
$ErrorActionPreference='Stop'
$BaseUrl='http://192.168.10.10:30080'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$profile=Join-Path $env:TEMP ('market-mock-admin-sso-'+[Guid]::NewGuid().ToString('N'))
$result=@{status='failed';checks=@();scope='existing delivered SaaS; browser launch, consent, callback and local session; no payment or delivery state changes'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $u=[Uri]$url
        if($u.GetLeftPart([UriPartial]::Authority) -cnotin @($BaseUrl,'http://192.168.10.10:30083') -or $u.UserInfo){return $false}
        if($method -ceq 'GET'){return $true}
        return $method -ceq 'POST' -and $u.GetLeftPart([UriPartial]::Authority) -ceq $BaseUrl -and
            ($u.AbsolutePath -ceq ('/api/orders/'+$script:fixture.order_id+'/delivery-launch') -or
             $u.AbsolutePath -cmatch '^/api/oidc/browser/[a-zA-Z0-9_-]+/approve$')
    } catch {return $false}
}
try {
    foreach($file in @(@{path='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{path='development_steps_browser.ps1';names=@('Connect-ReadOnlyChrome')},@{path='unified_delivery_browser_cdp.ps1';names=@('Resolve-PrivatePosixFile')})) {
        $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $file.path),[ref]$null,[ref]$null)
        foreach($name in $file.names){$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false); . ([ScriptBlock]::Create($fn.Extent.Text))}
    }
    $script:fixture=Get-Content -Raw (Resolve-PrivatePosixFile $FixtureFile)|ConvertFrom-Json
    Connect-ReadOnlyChrome
    Cdp 'Page.navigate' @{url="$BaseUrl/console"}|Out-Null
    WaitJs 'document.readyState==="complete"' 20
    $secret=ConvertTo-Json $script:fixture.token -Compress
    Js "localStorage.setItem('market_token',$secret);true"|Out-Null
    $secret=$null
    $order=ConvertTo-Json $script:fixture.order_id -Compress
    $target=Js "(async()=>{const r=await fetch('/api/orders/'+$order+'/delivery-launch',{method:'POST',headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}});if(!r.ok)throw Error('Launch rejected');return (await r.json()).launch_url})()"
    if(([Uri]$target).GetLeftPart([UriPartial]::Authority) -cne 'http://192.168.10.10:30083'){throw 'Unexpected launch origin'}
    if($fixture.product_id) {
        if(([Uri]$target).AbsolutePath -cne ('/apps/'+$fixture.product_id) -or $target.Contains('oidc_')){throw 'Launch did not use stable product entry'}
        if(-not $target.Contains('product_id='+$fixture.product_id) -or -not $target.Contains('enterprise_id='+$fixture.enterprise_id)){throw 'Launch context missing'}
        $result.checks+=,'Stable product entry carries authoritative product/enterprise context without OIDC client ID'
    }
    Cdp 'Page.navigate' @{url=$target}|Out-Null
    WaitJs '!!document.querySelector(".oidc-consent footer button.primary-btn")' 30
    $result.checks+=,'Platform authorized launch reaches OIDC browser consent'
    Js 'document.querySelector(".oidc-consent footer button.primary-btn").click();true'|Out-Null
    WaitJs 'location.origin==="http://192.168.10.10:30083"&&location.pathname==="/app"&&!!document.querySelector("main h1")' 30
    $result.checks+=,'Authorization-code PKCE callback establishes real mock SaaS session'
    if(-not (Js 'document.body.innerText.includes("DEVELOPMENT MOCK SaaS")')){throw 'Mock application banner missing'}
    Cdp 'Page.reload' @{ignoreCache=$true}|Out-Null
    WaitJs 'location.pathname==="/app"&&!!document.querySelector("main h1")' 20
    $result.checks+=,'Mock SaaS session remains valid on reload'
    if($script:blocked -or $script:nativeDialogs){throw 'Unexpected write or native dialog'}
    $result.status='passed'
} catch {$result.error=$_.Exception.Message}
finally {
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $result|ConvertTo-Json -Depth 8|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'sso-result.json')
}
if($result.status -cne 'passed'){throw $result.error}
Write-Output ('Passed: '+$result.checks.Count+' live SSO regression checks')
