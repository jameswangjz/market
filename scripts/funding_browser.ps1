param([Parameter(Mandatory=$true)][string]$TokenDirectory,
      [string]$BaseUrl='http://127.0.0.1:8126',
      [string]$OutputDir="$env:TEMP\market-funding-browser",
      [switch]$ContinueRoles,
      [string]$PriorResult="$env:TEMP\market-clr2-funding-transaction-final\result.json")
$ErrorActionPreference='Stop'
if($BaseUrl -cne 'http://127.0.0.1:8126'){throw 'Only isolated localhost:8126 is allowed'}
if($ContinueRoles -and -not $PSBoundParameters.ContainsKey('OutputDir')){$OutputDir="$env:TEMP\market-clr2-funding-roles-continuation"}
if($ContinueRoles -and (Test-Path -LiteralPath (Join-Path $OutputDir 'result.json'))){throw 'Continuation evidence already exists; select a new output directory'}
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-funding-'+[Guid]::NewGuid().ToString('N'))
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$script:writeCount=0; $script:actor=''; $script:proposal='cccccccccccccccccccccccccccccccc'
$script:stage='startup'; $script:allowedPaths=[Collections.Generic.HashSet[string]]::new()
$result=@{status='failed';checks=@();screenshots=@();scope='isolated SQLite; synthetic funding only; three allowlisted POSTs'}
if($ContinueRoles){$result.scope='read-only role continuation; existing fixed covered proposal; zero POSTs'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $u=[Uri]$url
        if($u.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $u.UserInfo -or $u.Fragment){return $false}
        if($method -cin @('GET','HEAD') -and -not $postData){
            if($u.AbsolutePath -cmatch '(credential|secret|download)'){return $false}
            $null=$script:allowedPaths.Add($method+' '+$u.AbsolutePath); return $true
        }
        if($ContinueRoles){return $false}
        if($method -cne 'POST' -or $u.Query -or $script:writeCount -ge 3){return $false}
        $b=$postData|ConvertFrom-Json
        if($b.request_id -cnotmatch '^[A-Za-z0-9-]{1,180}$'){return $false}
        $keys=@($b.PSObject.Properties.Name|Sort-Object)
        $allowed=$false
        if($script:writeCount -eq 0 -and $script:actor -ceq 'platform' -and $u.AbsolutePath -ceq '/api/settlement-finance/funding-adjustments') {
            $allowed=($keys -join ',') -ceq 'amount,currency,evidence_refs,kind,reason,request_id,settlement_id' -and
                $b.settlement_id -ceq 'clr2-funding-settlement' -and $b.kind -ceq 'platform_absorption' -and
                $b.amount -ceq '10.00' -and $b.currency -ceq 'CNY' -and $b.reason -ceq 'Synthetic funding browser evidence' -and
                @($b.evidence_refs).Count -eq 1 -and $b.evidence_refs[0] -ceq 'synthetic-funding:only'
        } elseif($script:writeCount -eq 1 -and $script:actor -ceq 'ops' -and $u.AbsolutePath -ceq "/api/settlement-finance/funding-adjustments/$script:proposal/decision") {
            $allowed=($keys -join ',') -ceq 'decision,expected_version,note,request_id' -and $b.expected_version -eq 1 -and
                $b.decision -ceq 'approved' -and $b.note -ceq 'Independent synthetic funding review'
        } elseif($script:writeCount -eq 2 -and $script:actor -ceq 'ops' -and $u.AbsolutePath -ceq "/api/settlement-finance/funding-adjustments/$script:proposal/verify") {
            $allowed=($keys -join ',') -ceq 'expected_version,request_id' -and $b.expected_version -eq 2
        }
        if($allowed){$script:writeCount++; $null=$script:allowedPaths.Add('POST '+$u.AbsolutePath)}
        return $allowed
    } catch {return $false}
}
function Check([string]$expression,[string]$description) {
    if(-not (Js $expression)){throw $description}
    $result.checks+=,$description
}
function Enter([string]$actor) {
    $script:stage='enter-'+$actor
    $script:actor=$actor
    $secret=ConvertTo-Json (Read-PrivateToken ($TokenDirectory.TrimEnd('/')+'/'+$actor+'.token')) -Compress
    Js "localStorage.setItem('market_token',$secret);window.__oldFundingPage=true;true"|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=settlements"}|Out-Null
    WaitJs '!window.__oldFundingPage && document.readyState==="complete"' 30
    $script:stage='report-tabs-'+$actor
    WaitJs '!!document.querySelector(".settlement-tabs, main [role=tab]")' 30
    Js '[...document.querySelectorAll(".settlement-tabs button, main [role=tab]")].find(b=>b.textContent.includes("\u62a5\u8868")).click();true'|Out-Null
    WaitJs 'document.querySelector(".settlement-finance")?.getAttribute("aria-busy")==="false"' 30
}
function Funding {
    $script:stage='funding-panel-'+$script:actor
    WaitJs '!!document.querySelector(".settlement-finance > .console-tabs [role=tab]:last-child")' 20
    Js 'document.querySelector(".settlement-finance > .console-tabs [role=tab]:last-child").click();true'|Out-Null
    WaitJs '!!document.querySelector(".settlement-management")' 20
    Js '(()=>{const b=document.querySelectorAll(".settlement-management > .console-tabs [role=tab]")[3];if(!b||b.disabled)throw Error();b.click();return true})()'|Out-Null
    WaitJs '!!document.querySelector(".funding-management") && document.querySelector(".funding-table")?.getAttribute("aria-busy")==="false"' 20
    Check '!document.querySelector(".funding-management [role=alert]")' 'Funding API contract has no errors'
}
function Submit-Dialog {
    Js 'document.querySelector(".funding-dialog form").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));true'|Out-Null
    WaitJs '!document.querySelector(".funding-dialog") && document.querySelector(".funding-table")?.getAttribute("aria-busy")==="false"' 20
}
function Assert-BankFalse([string]$scope='') {
    $script:stage='covered-bankfalse-'+$script:actor
    $query=ConvertTo-Json $scope -Compress
    $proof=Js "(async()=>{const r=await fetch('/api/settlement-finance/funding-adjustments'+$query,{headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}});if(!r.ok)return {http_status:r.status,verified:false};const d=await r.json();const p=d.items.find(x=>x.id==='$script:proposal');return {http_status:r.status,verified:p?.settlement_id==='clr2-funding-settlement'&&p?.status==='covered'&&p?.version===3&&p.bank_receipt_verified===false}})()"
    $result.http_status=[int]$proof.http_status
    if(-not $proof.verified){throw 'fixed_covered_bankfalse_check_failed'}
    $result.checks+=,'Actual API covered result retains bank_receipt_verified=false'
}
try {
    if($ContinueRoles){
        $script:stage='prior-evidence'
        $prior=Get-Content -LiteralPath $PriorResult -Raw|ConvertFrom-Json
        if($prior.checks.Count -lt 7 -or $prior.checks -cnotcontains 'Actual API covered result retains bank_receipt_verified=false' -or $prior.screenshots -cnotcontains 'funding-1440.png' -or $prior.screenshots -cnotcontains 'funding-390.png'){throw 'Prior completed funding evidence missing'}
        $result.prior_successful_write_count=3
        $result.write_evidence='prior normal chain evidence plus fixed covered/version3 GET; no write replay'
    }
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    foreach($file in @(@{path='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{path='development_steps_browser.ps1';names=@('Read-PrivateToken','Connect-ReadOnlyChrome')})) {
        $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $file.path),[ref]$null,[ref]$null)
        foreach($name in $file.names){$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false);if(-not $fn){throw 'CDP helper missing'};. ([ScriptBlock]::Create($fn.Extent.Text))}
    }
    Connect-ReadOnlyChrome
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console"}|Out-Null
    WaitJs 'document.readyState==="complete"' 20
    if(-not $ContinueRoles){
    Enter 'platform'; Funding
    Js 'document.querySelector(".funding-management button[aria-label=\"\u65b0\u589e\u8865\u8db3\u63d0\u6848\"]").click();true'|Out-Null
    WaitJs '!!document.querySelector(".funding-dialog select option[value=clr2-funding-settlement]")' 20
    Js '(()=>{const d=document.querySelector(".funding-dialog"),s=d.querySelector("select");s.value="clr2-funding-settlement";s.dispatchEvent(new Event("change",{bubbles:true}));const inputs=d.querySelectorAll("input");const amount=d.querySelector("input[inputmode=decimal]");amount.value="10.00";amount.dispatchEvent(new Event("input",{bubbles:true}));const a=d.querySelectorAll("textarea");["Synthetic funding browser evidence","synthetic-funding:only"].forEach((v,i)=>{a[i].value=v;a[i].dispatchEvent(new Event("input",{bubbles:true}))});return true})()'|Out-Null
    Check 'document.querySelector(".funding-dialog select").selectedOptions[0].textContent.includes("SET-FUNDING-FIXTURE")' 'Named SET selector uses the fixed internal settlement identity'
    Submit-Dialog
    WaitJs "document.querySelector('.funding-table').textContent.includes('$script:proposal')" 20
    Check '!document.querySelector(".funding-table button[aria-label=\"\u5ba1\u6279\u8865\u8db3\u63d0\u6848\"]")' 'Creator cannot approve own proposal; no self-review POST'
    Enter 'ops'; Funding
    Js 'document.querySelector(".funding-table button[aria-label=\"\u5ba1\u6279\u8865\u8db3\u63d0\u6848\"]").click();true'|Out-Null
    WaitJs '!!document.querySelector(".funding-dialog textarea")' 10
    Js '(()=>{const n=document.querySelector(".funding-dialog textarea");n.value="Independent synthetic funding review";n.dispatchEvent(new Event("input",{bubbles:true}));return true})()'|Out-Null
    Submit-Dialog
    WaitJs '!!document.querySelector(".funding-table button[aria-label=\"\u6838\u9a8c\u8d44\u91d1\u8986\u76d6\"]")' 20
    Js 'document.querySelector(".funding-table button[aria-label=\"\u6838\u9a8c\u8d44\u91d1\u8986\u76d6\"]").click();true'|Out-Null
    Submit-Dialog
    Assert-BankFalse
    foreach($width in @(1440,390)) {
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
        Js 'document.querySelector(".funding-management").scrollIntoView({block:"start"});true'|Out-Null
        Check '(()=>{const r=document.querySelector(".funding-management").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1})()' ('Funding panel fits viewport '+$width)
        $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
        $name="funding-$width.png"; [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data)); $result.screenshots+=,$name
    }
    }
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Enter 'provider'
    Assert-BankFalse '?enterprise_id=provider_tenant&business_view=provider'
    Funding
    $script:stage='provider-readonly-controls'
    Check '!document.querySelector(".funding-management button[aria-label=\"\u65b0\u589e\u8865\u8db3\u63d0\u6848\"]") && document.querySelectorAll(".funding-actions button").length===document.querySelectorAll(".funding-table tbody tr").length' 'Provider sees detail actions only; no write controls'
    Assert-BankFalse '?enterprise_id=provider_tenant&business_view=provider'
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir 'funding-provider-readonly.png'),[Convert]::FromBase64String($shot.data)); $result.screenshots+=,'funding-provider-readonly.png'
    Enter 'security'
    $script:stage='security-readonly-controls'
    Check '!document.querySelector(".funding-dialog") && !document.querySelector(".settlement-management")' 'Security has no management write surface'
    Assert-BankFalse
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir 'funding-security-readonly.png'),[Convert]::FromBase64String($shot.data)); $result.screenshots+=,'funding-security-readonly.png'
    $expectedWrites=3; if($ContinueRoles){$expectedWrites=0}
    if($script:blocked -or $script:nativeDialogs -or $script:writeCount -ne $expectedWrites){throw 'Unexpected request, dialog, or POST count'}
    $result.status='passed'; $result.post_count=$script:writeCount
} catch {$result.error='Funding browser acceptance failed; private diagnostics suppressed'}
finally {
    $result.stage=$script:stage; $result.post_count=$script:writeCount
    $result.allowed_request_paths=@($script:allowedPaths|Sort-Object)
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $result|ConvertTo-Json -Depth 8|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
}
if($result.status -cne 'passed'){throw $result.error}
Write-Output ('Passed: '+$result.checks.Count+' funding-only browser checks')
