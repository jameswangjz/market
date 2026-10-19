param([Parameter(Mandatory=$true)][string]$FixtureFile,
      [string]$BaseUrl='http://192.168.10.10:30083',
      [Parameter(Mandatory=$true)][string]$OutputDir)
$ErrorActionPreference='Stop'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$profile=Join-Path $env:TEMP ('market-mock-admin-'+[Guid]::NewGuid().ToString('N'))
$result=@{status='failed';checks=@();screenshots=@();scope='live mock configuration UI; only owned synthetic client writes; no platform order mutation'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $u=[Uri]$url
        if($u.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $u.UserInfo){return $false}
        if($method -ceq 'GET'){return $true}
        if($method -ceq 'POST' -and $u.AbsolutePath -ceq '/admin/api/check'){return $true}
        if($method -ceq 'PUT' -and $u.AbsolutePath -ceq '/admin/api/clients') {
            $body=$postData|ConvertFrom-Json
            return $body.client_id -ceq $script:fixture.client_id -and $body.product_id -ceq $script:fixture.product_id
        }
        return $false
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
    $script:fixture=Get-Content -Raw (Resolve-PrivatePosixFile $FixtureFile)|ConvertFrom-Json
    Connect-ReadOnlyChrome
    Cdp 'Page.navigate' @{url="$BaseUrl/admin"}|Out-Null
    WaitJs '!!document.querySelector("#admin-login")' 20
    Check '(async()=>{const r=await fetch("/admin/api/clients");return r.status===403})()' 'Unauthenticated client list denied'
    $key=ConvertTo-Json $script:fixture.admin_key -Compress
    Js "document.querySelector('#admin-key').value=$key;document.querySelector('#login-form').requestSubmit();true"|Out-Null
    $key=$null
    WaitJs '!document.querySelector("#workspace").hidden' 20
    Check 'document.querySelector("#admin-key").value===""' 'Admin key input cleared after login'
    Check 'document.querySelector("[name=issuer]").readOnly&&document.querySelector("[name=redirect_uri]").readOnly' 'Issuer and callback fixed to deployed trusted configuration'
    $config=@{}
    foreach($name in @('client_id','client_secret','product_id','product_name')){$config[$name]=$script:fixture.$name}
    $encoded=$config|ConvertTo-Json -Compress
    Js "(()=>{const fields=$encoded;const f=document.querySelector('#client-form');for(const [k,v] of Object.entries(fields))f.elements.namedItem(k).value=v;f.requestSubmit();return true})()"|Out-Null
    $encoded=$null; $config=$null
    $client=ConvertTo-Json $script:fixture.client_id -Compress
    WaitJs "[...document.querySelectorAll('#clients-list tbody tr')].some(r=>r.cells[0].textContent===$client)" 20
    Check 'document.querySelector("[name=client_secret]").value===""' 'Client secret cleared after saving'
    Check "(()=>{const r=[...document.querySelectorAll('#clients-list tbody tr')].find(r=>r.cells[0].textContent===$client);return r.querySelector('input[aria-label]').value.includes('/apps/')&&!r.querySelector('input[aria-label]').value.includes($client)})()" 'Admin table exposes stable product entry without OIDC client ID'
    Js "[...document.querySelectorAll('#clients-list tbody tr')].find(r=>r.cells[0].textContent===$client).querySelectorAll('button').forEach(b=>{if(b.textContent==='\u7f16\u8f91')b.click()});true"|Out-Null
    Check 'document.querySelector("#application-url").readOnly&&document.querySelector("#application-url").value.includes("/apps/")' 'Edit form presents a read-only stable application entry'
    Check 'document.querySelector("[name=client_secret]").value===""&&document.querySelector("[name=client_id]").readOnly' 'Edit does not expose stored secret and locks client identity'
    Js 'document.querySelector("[name=product_name]").value="Updated browser verification";document.querySelector("#client-form").requestSubmit();true'|Out-Null
    WaitJs "[...document.querySelectorAll('#clients-list tbody tr')].some(r=>r.cells[0].textContent===$client&&r.cells[2].textContent==='Updated browser verification')" 20
    $result.checks+=,'Client metadata update succeeds with blank secret'
    Js 'document.querySelector("#discovery-check").click();true'|Out-Null
    WaitJs 'document.querySelector("#admin-status").textContent.includes("\u68c0\u67e5\u901a\u8fc7")' 20
    $result.checks+=,'Real trusted-backchannel Discovery check succeeds'
    Check 'localStorage.length===0&&sessionStorage.length===0' 'No credentials retained in browser storage'
    foreach($width in @(1440,390)) {
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
        Check 'document.documentElement.scrollWidth<=innerWidth+1' ('No page horizontal overflow at '+$width)
        Check '[...document.querySelectorAll("input[type=password]")].every(n=>!n.value)' 'No filled password fields before screenshot'
        New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
        $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
        $name='admin-'+$width+'.png'
        [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
        $result.screenshots+=,$name
    }
    Js 'document.querySelector("#logout").click();true'|Out-Null
    Check 'document.querySelector("#workspace").hidden&&document.querySelectorAll("#clients-list tbody tr").length===0' 'Logout clears configuration view'
    if($script:blocked -or $script:nativeDialogs){throw 'Unexpected write or browser dialog'}
    $result.status='passed'
} catch {$result.error=$_.Exception.Message}
finally {
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $result|ConvertTo-Json -Depth 8|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
}
if($result.status -cne 'passed'){throw $result.error}
Write-Output ('Passed: '+$result.checks.Count+' mock SaaS administration browser checks')
