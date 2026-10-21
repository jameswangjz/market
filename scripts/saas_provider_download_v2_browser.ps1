param([Parameter(Mandatory=$true)][string]$FixtureFile,
      [Parameter(Mandatory=$true)][string]$OutputDir)
$ErrorActionPreference='Stop'
$BaseUrl='http://192.168.10.10:30080'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$profile=Join-Path $env:TEMP ('market-saas-download-v2-'+[Guid]::NewGuid().ToString('N'))
$result=@{status='failed';checks=@();scope='live provider product download; GET only; no business state mutation'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $u=[Uri]$url
        return $method -ceq 'GET' -and $u.GetLeftPart([UriPartial]::Authority) -ceq $BaseUrl -and -not $postData -and -not $u.UserInfo
    } catch {return $false}
}
function Check([string]$expression,[string]$description) {
    if(-not (Js $expression)){throw $description}
    $result.checks+=,$description
}
try {
    foreach($file in @(@{path='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{path='development_steps_browser.ps1';names=@('Connect-ReadOnlyChrome')},@{path='unified_delivery_browser_cdp.ps1';names=@('Resolve-PrivatePosixFile')})) {
        $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $file.path),[ref]$null,[ref]$null)
        foreach($name in $file.names){$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false); . ([ScriptBlock]::Create($fn.Extent.Text))}
    }
    $fixture=Get-Content -Raw (Resolve-PrivatePosixFile $FixtureFile)|ConvertFrom-Json
    Connect-ReadOnlyChrome
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=@'
const originalSend=XMLHttpRequest.prototype.send;
const originalOpen=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(method,url,...rest){this.__downloadPath=new URL(url,location.href).pathname;return originalOpen.call(this,method,url,...rest)};
XMLHttpRequest.prototype.send=function(...rest){if(this.__downloadPath?.endsWith('/saas-integration-credentials-download'))this.addEventListener('load',async()=>{try{const v=JSON.parse(this.response instanceof Blob?await this.response.text():this.responseText);window.__providerExportV2={status:this.status,schema:v.schema_version,hasOidc:!!v.oidc_sso?.client_id&&!!v.oidc_sso?.client_secret,hasValidation:!!v.resource_server?.introspection_url&&!('client_secret' in v.resource_server)&&!('client_id' in v.resource_server),hasMachineCredentials:!!v.oauth2_api}}catch{window.__providerExportV2={error:true}}});return originalSend.apply(this,rest)};
'@}|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console"}|Out-Null
    WaitJs 'document.readyState==="complete"' 20
    $secret=ConvertTo-Json $fixture.token -Compress
    Js "localStorage.setItem('market_token',$secret);true"|Out-Null
    $secret=$null
    Cdp 'Page.navigate' @{url=($BaseUrl+'/console?view=products&enterprise_id='+$fixture.enterprise_id)}|Out-Null
    $name=ConvertTo-Json $fixture.product_name -Compress
    $row="[...document.querySelectorAll('tbody tr')].find(r=>r.textContent.includes($name))"
    WaitJs "!!$row" 30
    Js "$row.querySelectorAll('button').forEach(b=>{if(b.textContent.includes('\u4e0b\u8f7d\u63a5\u5165\u914d\u7f6e'))b.click()});true"|Out-Null
    WaitJs '!!window.__providerExportV2' 20
    Check 'window.__providerExportV2.status===200&&window.__providerExportV2.schema==="2.0"' 'Provider product action downloads schema2.0'
    Check 'window.__providerExportV2.hasOidc&&window.__providerExportV2.hasValidation&&!window.__providerExportV2.hasMachineCredentials' 'Downloaded file preserves OIDC and validation metadata without machine credentials'
    Check '(async()=>{const r=await fetch("/saas-integration-standard.md",{cache:"no-store"});const t=await r.text();return r.ok&&t.includes("v1.4")&&t.includes("\"schema_version\": \"2.0\"")&&t.includes("resource_server")})()' 'Front-end documentation download is v1.4 with schema2.0'
    if($script:blocked -or $script:nativeDialogs){throw 'Unexpected mutation or native dialog'}
    $result.status='passed'
} catch {$result.error=$_.Exception.Message}
finally {
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $result|ConvertTo-Json -Depth 8|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser.json')
}
if($result.status -cne 'passed'){throw $result.error}
Write-Output ('Passed: '+$result.checks.Count+' provider download browser checks')
