param([Parameter(Mandatory=$true)][string]$TokenDirectory,
      [string]$OutputDir="$env:TEMP\market-clr2-multi-cost",
      [switch]$ReadOnly)
$ErrorActionPreference='Stop'
$BaseUrl='http://127.0.0.1:8129'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-multi-cost-'+[Guid]::NewGuid().ToString('N'))
$script:socket=$null;$script:chrome=$null;$script:commandId=0;$script:blocked=0;$script:nativeDialogs=0;$script:publicAuth=0
$script:proposalId='';$script:postPaths=[Collections.Generic.List[string]]::new()
$result=@{status='failed';checks=@();screenshots=@();scope='isolated localhost8129 real synthetic VC sources; only one cost proposal and one opposite-party decision are writable'}
function Allowed-Request([string]$method,[string]$url,[string]$postData){
    try {
        $u=[Uri]$url
        if($u.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $u.UserInfo){return $false}
        if($method -cin @('GET','HEAD') -and -not $postData){return $true}
        if($ReadOnly){return $false}
        if($method -cne 'POST' -or -not $postData){return $false}
        $b=$postData|ConvertFrom-Json
        if(-not $b.request_id){return $false}
        $allowed=$false
        if($u.AbsolutePath -ceq '/api/settlement-cost-exceptions'){
            $allowed=$b.refund_id -ceq 'multi-cost-ui' -and $b.schema_version -eq 2 -and $b.retained_cost -ceq '0.03' -and
                $b.reason -ceq 'Synthetic multi cost invoice evidence' -and $b.source_allocations.Count -eq 2 -and
                @($b.source_allocations|Where-Object {$_.retained_cost -ceq '0.01'}).Count -eq 1 -and
                @($b.source_allocations|Where-Object {$_.retained_cost -ceq '0.02'}).Count -eq 1 -and
                @($b.source_allocations|Where-Object {$_.evidence_refs.Count -ne 1 -or $_.evidence_refs[0] -cnotmatch '^synthetic-multi-invoice:[12]$'}).Count -eq 0
        } elseif($script:proposalId -and $u.AbsolutePath -ceq ('/api/settlement-cost-exceptions/'+$script:proposalId+'/decision')){
            $allowed=$b.expected_version -eq 1 -and $b.decision -ceq 'approve' -and $b.reason -ceq 'Synthetic whole-proposal independent review'
        }
        if($allowed){$script:postPaths.Add($u.AbsolutePath)}
        return $allowed
    } catch {return $false}
}
function Screenshot([string]$name){
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$true}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data));$result.screenshots+=,$name
}
try {
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    foreach($file in @(@{path='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},
        @{path='development_steps_browser.ps1';names=@('Read-PrivateToken','Connect-ReadOnlyChrome')},
        @{path='clearing_v2_management_browser.ps1';names=@('Check','Fill','Click','Enter','Management')})){
        $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $file.path),[ref]$null,[ref]$null)
        foreach($name in $file.names){$fn=$ast.Find({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false);if(-not $fn){throw 'CDP helper missing'};. ([ScriptBlock]::Create($fn.Extent.Text))}
    }
    Connect-ReadOnlyChrome
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1100;deviceScaleFactor=1;mobile=$false}|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console"}|Out-Null;WaitJs 'document.readyState==="complete"' 20
    if($ReadOnly){
        Enter 'ops';Management 2
        Click '.settlement-management tbody button.icon-btn'
        WaitJs 'document.querySelectorAll(".settlement-management .cost-source").length===2 && !document.querySelector(".settlement-management [role=status]")' 25
        Check '[...document.querySelectorAll(".settlement-management .cost-source")].every(s=>/ORD/.test(s.textContent)&&/SET/.test(s.textContent)&&/BATCH/.test(s.textContent)&&/[a-f0-9]{64}/.test(s.textContent))' 'Latest dist renders both approved source business identities and baseline hashes'
        Check 'document.querySelector(".settlement-management").textContent.includes("synthetic-multi-invoice:1") && document.querySelector(".settlement-management").textContent.includes("synthetic-multi-invoice:2")' 'Latest dist preserves both frozen source invoices'
        foreach($width in @(1440,390)){
            Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=1100;deviceScaleFactor=1;mobile=$false}|Out-Null
            Start-Sleep -Milliseconds 300
            Screenshot ('multi-cost-final-'+$width+'.png')
            Check 'document.documentElement.scrollWidth<=innerWidth' ('Readonly source evidence fits '+$width)
        }
        Management 3
        WaitJs 'document.querySelector(".manual-funding") && !document.querySelector(".manual-funding [role=alert]")' 25
        Check 'document.querySelector(".manual-funding").textContent.includes("\u4eba\u5de5\u4ed8\u6b3e\u51ed\u636e")' 'New manual proof panel renders using actual scoped backend endpoint'
        if($script:postPaths.Count -or $script:blocked -or $script:nativeDialogs){throw 'Readonly continuation attempted unsafe behavior'}
        $result.status='passed'
        return
    }
    Enter 'provider';Management 2
    Js 'window.__multiCostBefore=null;(async()=>{const r=await fetch("/api/__fixture__/multi-cost/state",{headers:{Authorization:"Bearer "+localStorage.getItem("market_token")}});window.__multiCostBefore=await r.json();return true})()'|Out-Null
    $before=Js 'JSON.stringify(window.__multiCostBefore)'
    Click '.settlement-management > .toolbar .secondary-btn'
    WaitJs 'document.querySelector(".settlement-management form.editor select")?.options.length>1 && !document.querySelector(".settlement-management form.editor select").disabled' 20
    Fill '.settlement-management form.editor select' 'multi-cost-ui' 'change'
    WaitJs 'document.querySelectorAll("form.editor .cost-source").length===2 && !document.querySelector("form.editor button.primary-btn").disabled' 30
    Check '[...document.querySelectorAll("form.editor .cost-source")].every(s=>/ORD/.test(s.textContent)&&/SET/.test(s.textContent)&&/BATCH/.test(s.textContent)&&/[a-f0-9]{64}/.test(s.textContent))' 'Both actual sources display order, settlement, batch business numbers and final baseline hash'
    Check '[...document.querySelectorAll("form.editor .cost-source input")].every(x=>x.getAttribute("inputmode")==="decimal")' 'Source identities are server-bound; only amounts are editable inputs'
    Check '[...document.querySelectorAll("form.editor .cost-source")].every(s=>s.textContent.includes("\u672c\u6b21\u53ef\u4fdd\u7559\u6210\u672c\u4e0a\u9650"))' 'Each source displays its ordinary refund cost ceiling'
    Js 'document.querySelectorAll("form.editor .cost-source").forEach((s,i)=>{const a=s.querySelector("input");a.value=i?"0.02":"0.01";a.dispatchEvent(new Event("input",{bubbles:true}));const e=s.querySelector("textarea");e.value="synthetic-multi-invoice:"+(i+1);e.dispatchEvent(new Event("input",{bubbles:true}))});true'|Out-Null
    Js '(()=>{const s=document.querySelectorAll("form.editor select[required]")[1];s.value=[...s.options].find(o=>o.textContent.includes("Synthetic multi cost bearer")).value;s.dispatchEvent(new Event("change",{bubbles:true}));return true})()'|Out-Null
    Fill 'form.editor textarea[maxlength="4000"]' 'Synthetic multi cost invoice evidence'
    Check 'document.querySelector(".cost-total").textContent.includes("0.03")' 'Two decimal-string allocations produce exact0.03 total'
    Fill 'form.editor .cost-source input' '999999999999.99'
    Js 'document.querySelector("form.editor").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));true'|Out-Null
    WaitJs 'document.querySelector(".settlement-management [role=alert]")?.textContent.includes("\u4e0a\u9650")' 10
    if($script:postPaths.Count){throw 'Over-cap form unexpectedly reached a write API'}
    $result.checks+=,'Over-cap source is rejected before any network write'
    Fill 'form.editor .cost-source input' '0.01'
    foreach($width in @(1440,390)){
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=1100;deviceScaleFactor=1;mobile=$false}|Out-Null
        Start-Sleep -Milliseconds 200
        Check 'document.documentElement.scrollWidth<=innerWidth' ('Multi-source application layout fits '+$width)
        Screenshot ('multi-cost-application-'+$width+'.png')
    }
    Click 'form.editor button.primary-btn'
    WaitJs '!document.querySelector("form.editor") && document.querySelector(".settlement-management tbody")?.textContent.includes("0.03")' 25
    Check '!document.querySelector(".settlement-management tbody button.secondary-btn")' 'Applicant and same-side users cannot approve the whole proposal'
    $script:proposalId=Js '(async()=>{const r=await fetch("/api/settlement-cost-exceptions?enterprise_id=provider_tenant&business_view=provider",{headers:{Authorization:"Bearer "+localStorage.getItem("market_token"),"X-Enterprise-Id":"provider_tenant"}});const rows=(await r.json()).items.filter(x=>x.refund_id==="multi-cost-ui");if(rows.length!==1)throw Error("Expected exactly one whole proposal");window.__multiProposal=rows[0];return rows[0].id})()'
    Check 'window.__multiProposal.schema_version===2 && window.__multiProposal.source_allocations.length===2 && window.__multiProposal.source_allocations.every(s=>s.evidence_refs.length===1) && window.__multiProposal.retained_cost==="0.03"' 'Actual backend freezes both per-source invoices in one version2-schema proposal'
    Enter 'ops';Management 2
    Click '.settlement-management tbody button.secondary-btn'
    WaitJs 'document.querySelectorAll("form.editor .cost-source").length===2 && !document.querySelector("form.editor [role=status]")' 25
    Check 'document.querySelectorAll("form.editor .cost-source input,form.editor .cost-source textarea").length===0 && document.querySelector("form.editor").textContent.includes("synthetic-multi-invoice:1") && document.querySelector("form.editor").textContent.includes("synthetic-multi-invoice:2")' 'Reviewer sees every frozen source and invoice, with no per-source edit or separate approval'
    Fill 'form.editor textarea' 'Synthetic whole-proposal independent review'
    Screenshot 'multi-cost-whole-review-390.png'
    Click 'form.editor button.primary-btn'
    WaitJs '!document.querySelector("form.editor") && document.querySelector(".settlement-management tbody")?.textContent.includes("\u5df2\u6279\u51c6")' 25
    Check 'document.querySelectorAll(".settlement-management tbody tr").length===1 && !document.querySelector(".settlement-management tbody button.secondary-btn")' 'One opposite-party decision approves the whole proposal exactly once'
    $beforeJson=ConvertTo-Json $before -Compress
    Check "(async()=>{const r=await fetch('/api/__fixture__/multi-cost/state',{headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}});return JSON.stringify(await r.json())===$beforeJson})()" 'Buyer actual-paid amount, refund amount, source baselines and completion hashes remain unchanged'
    if($script:postPaths.Count -ne 2){throw 'Expected exactly one proposal write and one independent decision'}
    if($script:blocked -or $script:nativeDialogs){throw 'Unexpected request or native dialog'}
    $result.post_paths=@($script:postPaths);$result.status='passed'
} catch {$result.error=$_.Exception.Message;try{$result.failure_ui=Js 'document.querySelector("main")?.innerText.slice(-10000)'}catch{}}
finally {
    if($script:socket){$script:socket.Dispose()};if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $result|ConvertTo-Json -Depth 12|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
}
if($result.status -cne 'passed'){throw $result.error};Write-Output ('Passed: '+$result.checks.Count+' multi-cost browser checks')
