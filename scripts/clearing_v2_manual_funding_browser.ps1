param([Parameter(Mandatory=$true)][string]$TokenDirectory,
      [string]$OutputDir="$env:TEMP\market-clr2-manual-funding",
      [switch]$ContinueRegistered,
      [switch]$ContinueReviewed)
$ErrorActionPreference='Stop'
$BaseUrl='http://127.0.0.1:8126'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-manual-funding-'+[Guid]::NewGuid().ToString('N'))
$script:socket=$null;$script:chrome=$null;$script:commandId=0;$script:blocked=0;$script:nativeDialogs=0;$script:publicAuth=0
$script:actor='';$script:evidenceId='';$script:writes=0
$proposal='cccccccccccccccccccccccccccccccc'
$result=@{status='failed';checks=@();screenshots=@();scope='localhost8126 isolated SQLite; exactly manual register, independent review, existing cover'}
function Allowed-Request([string]$method,[string]$url,[string]$postData){
    try{
        $u=[Uri]$url
        if($u.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $u.UserInfo -or $u.Query -and $method -ceq 'POST'){return $false}
        if($method -cin @('GET','HEAD') -and -not $postData){return $true}
        if($method -cne 'POST' -or $script:writes -ge 3){return $false}
        $b=$postData|ConvertFrom-Json
        if(-not $b.request_id){return $false}
        $allowed=$false
        if($script:writes -eq 0 -and $script:actor -ceq 'platform' -and $u.AbsolutePath -ceq '/api/settlement-finance/manual-funding-evidence'){
            $allowed=$b.funding_request_id -ceq $proposal -and $b.amount -ceq '10.00' -and $b.currency -ceq 'CNY' -and
                $b.reference -ceq 'SYNTHETIC-MANUAL-001' -and $b.evidence_refs.Count -eq 1 -and
                $b.evidence_refs[0] -ceq 'synthetic-manual:receipt' -and $b.note -ceq 'Synthetic manual registration only' -and
                @($b.PSObject.Properties.Name).Count -eq 8
        }elseif($script:writes -eq 1 -and $script:actor -ceq 'ops' -and $script:evidenceId -and $u.AbsolutePath -ceq ('/api/settlement-finance/manual-funding-evidence/'+$script:evidenceId+'/review')){
            $allowed=$b.expected_version -eq 1 -and $b.decision -ceq 'verified' -and $b.note -ceq 'Synthetic independent receipt review' -and @($b.PSObject.Properties.Name).Count -eq 4
        }elseif($script:writes -eq 2 -and $script:actor -ceq 'ops' -and $u.AbsolutePath -ceq ('/api/settlement-finance/funding-adjustments/'+$proposal+'/verify')){
            $allowed=$b.expected_version -eq 2 -and $b.receipt_reference -ceq ('manual:'+$script:evidenceId) -and @($b.PSObject.Properties.Name).Count -eq 3
        }
        if($allowed){$script:writes++};return $allowed
    }catch{return $false}
}
function Manual {
    Management 3
    WaitJs '!!document.querySelector(".manual-funding") && !document.querySelector(".manual-funding [role=alert]")' 20
}
function Submit {
    Js 'document.querySelector(".manual-dialog form").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));true'|Out-Null
    WaitJs '!document.querySelector(".manual-dialog")' 20
}
try{
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    foreach($file in @(@{path='scripts/password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{path='scripts/development_steps_browser.ps1';names=@('Read-PrivateToken','Connect-ReadOnlyChrome')},@{path='scripts/clearing_v2_management_browser.ps1';names=@('Check','Fill','Click','Enter','Management')})){
        $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Get-Location) $file.path),[ref]$null,[ref]$null)
        foreach($name in $file.names){$fn=$ast.Find({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false);if(-not $fn){throw ('Missing helper '+$name)};. ([ScriptBlock]::Create($fn.Extent.Text))}
    }
    Connect-ReadOnlyChrome
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1100;deviceScaleFactor=1;mobile=$false}|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console"}|Out-Null;WaitJs 'document.readyState==="complete"' 20
    $script:actor='platform';Enter 'platform';Manual
    if(-not $ContinueRegistered -and -not $ContinueReviewed){
    Click '.manual-funding > header .tools button:last-child'
    WaitJs 'document.querySelector(".manual-dialog select")?.options.length===2' 20
    Fill '.manual-dialog select' $proposal 'change'
    Check 'document.querySelector(".manual-dialog").textContent.includes("10.00") && !document.querySelector(".manual-dialog input[inputmode=decimal]")' 'Full gap is fixed from actual approved proposal, not client-entered amount'
    Fill '.manual-dialog input[maxlength="240"]' 'SYNTHETIC-MANUAL-001'
    Fill '.manual-dialog input[type=date]' '2026-01-01'
    Fill '.manual-dialog textarea:not([maxlength])' 'synthetic-manual:receipt'
    Fill '.manual-dialog textarea[maxlength="4000"]' 'Synthetic manual registration only'
    Submit
    WaitJs 'document.querySelector(".manual-funding tbody").textContent.includes("SYNTHETIC-MANUAL-001")' 20
    Check '!document.querySelector(".manual-funding tbody button")' 'Registrar has no self-review or unreviewed cover control'
    }else{$script:writes=1;$result.prior_registration='existing isolated synthetic receipt; registration is not replayed'}
    $script:evidenceId=Js '(async()=>{const r=await fetch("/api/settlement-finance/manual-funding-evidence",{headers:{Authorization:"Bearer "+localStorage.getItem("market_token")}});const d=await r.json();if(d.items.length!==1||!["registered","verified"].includes(d.items[0].status)||d.items[0].bank_receipt_verified!==false)throw Error("Bad manual DTO");return d.items[0].id})()'
    $result.checks+=,'Actual registration is immutable pending manual review, never bank verified'
    $script:actor='ops';Enter 'ops';Manual
    WaitJs 'document.querySelector(".manual-funding tbody").textContent.includes("SYNTHETIC-MANUAL-001") && !!document.querySelector(".manual-funding tbody button")' 20
    if(-not $ContinueReviewed){
    Click '.manual-funding tbody button'
    Fill '.manual-dialog textarea' 'Synthetic independent receipt review'
    Submit
    }else{$script:writes=2;$result.prior_review='existing independently reviewed synthetic evidence; review is not replayed'}
    WaitJs 'document.querySelector(".manual-funding tbody").textContent.includes("\u4eba\u5de5\u51ed\u636e\u5df2\u590d\u6838") && !!document.querySelector(".manual-funding tbody button")' 20
    Check '!document.querySelector(".manual-funding tbody button[aria-label*=\u590d\u6838]")' 'Independent review consumes version1 and removes repeat-review control'
    Click '.manual-funding tbody button'
    Check 'document.querySelector(".manual-dialog").textContent.includes("SYNTHETIC-MANUAL-001") && !document.querySelector(".manual-dialog input")' 'Existing cover uses reviewed evidence, no arbitrary receipt reference input'
    Submit
    WaitJs 'document.querySelector(".funding-management tbody")?.textContent.includes("\u7f3a\u53e3\u5df2\u8986\u76d6")' 20
    Check '(async()=>{const r=await fetch("/api/settlement-finance/funding-adjustments",{headers:{Authorization:"Bearer "+localStorage.getItem("market_token")}});const row=(await r.json()).items.find(x=>x.id==="cccccccccccccccccccccccccccccccc");return row.status==="covered"&&row.version===3&&row.bank_receipt_verified===false})()' 'Actual existing cover result is version3 with bank_receipt_verified false'
    foreach($width in @(1440,390)){
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=1100;deviceScaleFactor=1;mobile=$false}|Out-Null
        Start-Sleep -Milliseconds 300
        Check 'document.documentElement.scrollWidth<=innerWidth' ('Manual proof layout fits '+$width)
        $name='manual-funding-'+$width+'.png';$shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$true};[IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data));$result.screenshots+=,$name
    }
    if($script:writes -ne 3 -or $script:blocked -or $script:nativeDialogs){throw 'Unexpected write count, blocked request or native dialog'}
    $result.status='passed';$result.write_count=$script:writes
}catch{$result.error=$_.Exception.Message;try{$result.failure_ui=Js 'document.querySelector("main")?.innerText.slice(-9000)'}catch{}}
finally{
    if($script:socket){$script:socket.Dispose()};if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    $result|ConvertTo-Json -Depth 12|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
}
if($result.status -cne 'passed'){throw $result.error};Write-Output ('Passed '+$result.checks.Count+' focused manual funding Chrome checks')
