param([Parameter(Mandatory=$true)][string]$TokenDirectory,
      [string]$BaseUrl='http://127.0.0.1:8126',
      [string]$OutputDir="$env:TEMP\market-clearing-management",
      [switch]$EvidenceOnly,
      [switch]$FundingOnly)
$ErrorActionPreference='Stop'
if($BaseUrl -cne 'http://127.0.0.1:8126'){throw 'Disposable localhost fixture only; launch with --management'}
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-management-'+[Guid]::NewGuid().ToString('N'))
$script:socket=$null;$script:chrome=$null;$script:commandId=0;$script:blocked=0;$script:nativeDialogs=0;$script:publicAuth=0
$script:decisionIds=@();$script:annualIds=@();$script:writes=[Collections.Generic.List[string]]::new()
$script:fundingQueries=[Collections.Generic.List[string]]::new()
$result=@{status='failed';checks=@();screenshots=@();scope='localhost disposable SQLite --management fixture only; allowlisted synthetic management writes'}
if($EvidenceOnly){$result.scope='localhost disposable --management fixture; evidence/style continuation; GET/HEAD only'}
if($FundingOnly){$result.scope='localhost disposable --management fixture; funding entry read/render only; GET/HEAD only'}
function Allowed-Request([string]$method,[string]$url,[string]$postData){
    try {
        $u=[Uri]$url
        if($u.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $u.UserInfo){return $false}
        if($method -cin @('GET','HEAD') -and -not $postData){if($u.AbsolutePath -ceq '/api/settlement-finance/funding-adjustments'){$script:fundingQueries.Add($u.Query)};return $true}
        if($EvidenceOnly -or $FundingOnly -or $method -cne 'POST' -or -not $postData){return $false}
        $b=$postData|ConvertFrom-Json;$path=$u.AbsolutePath
        if(-not $b.request_id){return $false}
        $allowed=($path -ceq '/api/settlement-parties' -and $b.name -ceq 'Synthetic browser expert' -and $b.enterprise_id -ceq 'provider_tenant' -and $b.kind -ceq 'expert' -and $b.currency -ceq 'CNY') -or
            ($path -ceq '/api/settlement-parties/products/product/allocations' -and $b.allocations.Count -eq 1 -and $b.allocations[0].weight -ceq '100') -or
            ($path -ceq '/api/settlement-cost-exceptions' -and $b.refund_id -cin @('clr2-management-refund-1','clr2-management-refund-2') -and $b.retained_cost -ceq '20.00' -and $b.reason -ceq 'Synthetic nonreturnable invoice' -and $b.evidence_refs[0] -ceq 'synthetic-invoice:1') -or
            ($path -cmatch '^/api/settlement-cost-exceptions/([^/]+)/decision$' -and $Matches[1] -cin $script:decisionIds -and $b.expected_version -eq 1 -and $b.decision -cin @('approve','reject') -and $b.reason -ceq 'Synthetic independent review') -or
            ($path -ceq '/api/settlement-reporting/annual-reports' -and $b.year -eq ((Get-Date).Year-1) -and $b.note -ceq 'Synthetic annual snapshot') -or
            ($path -cmatch '^/api/settlement-reporting/annual-reports/([^/]+)/finalize$' -and $Matches[1] -cin $script:annualIds -and $b.reconciliation_reference -ceq 'SYNTHETIC-ANNUAL-RECONCILIATION' -and $b.note -ceq 'Synthetic independent review')
        if($allowed){$script:writes.Add($path)}
        return $allowed
    } catch {return $false}
}
function Check([string]$expression,[string]$description){if(-not(Js $expression)){throw $description};$result.checks+=,$description}
function Fill([string]$selector,[string]$value,[string]$event='input'){
    $s=ConvertTo-Json $selector -Compress;$v=ConvertTo-Json $value -Compress;$e=ConvertTo-Json $event -Compress
    Js "(()=>{const x=document.querySelector($s);if(!x)throw Error('Missing input '+$s);x.value=$v;x.dispatchEvent(new Event($e,{bubbles:true}));return true})()"|Out-Null
}
function Click([string]$selector){$s=ConvertTo-Json $selector -Compress;Js "document.querySelector($s).click();true"|Out-Null}
function Enter([string]$actor){
    $token=ConvertTo-Json (Read-PrivateToken ($TokenDirectory.TrimEnd('/')+'/'+$actor+'.token')) -Compress
    Js "localStorage.setItem('market_token',$token);window.__previousManagementDocument=true;true"|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=settlements"}|Out-Null
    WaitJs '!window.__previousManagementDocument && !!document.querySelector(".settlement-tabs, main [role=tab]")' 30
    Js '[...document.querySelectorAll(".settlement-tabs button, main [role=tab]")].find(b=>b.textContent.includes("\u62a5\u8868")).click();true'|Out-Null
    WaitJs 'document.querySelector(".settlement-finance")?.getAttribute("aria-busy")==="false"' 30
}
function Management([int]$index=0){
    Js 'document.querySelector(".settlement-finance > .console-tabs").querySelectorAll("[role=tab]")[4].click();true'|Out-Null
    WaitJs '!!document.querySelector(".settlement-management")' 20
    Js "document.querySelector('.settlement-management > .console-tabs').querySelectorAll('[role=tab]')[$index].click();true"|Out-Null
    WaitJs '![...document.querySelectorAll(".settlement-management [role=status]")].some(x=>x.textContent.includes("\u52a0\u8f7d"))' 20
    Check '!document.querySelector(".settlement-management [role=alert]")' ('Management view '+$index+' loads without contract errors')
}
function Workspace([int]$index){
    Js "document.querySelector('.settlement-finance > .console-tabs').querySelectorAll('[role=tab]')[$index].click();true"|Out-Null
    WaitJs 'document.querySelector(".settlement-finance")?.getAttribute("aria-busy")==="false"' 20
    Check '!document.querySelector(".settlement-finance [role=alert]")' ('Workspace '+$index+' has no API errors')
}
try {
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    foreach($file in @(@{path='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{path='development_steps_browser.ps1';names=@('Read-PrivateToken','Connect-ReadOnlyChrome')})){
        $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $file.path),[ref]$null,[ref]$null)
        foreach($name in $file.names){$fn=$ast.Find({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false);if(-not $fn){throw 'CDP helper missing'};. ([ScriptBlock]::Create($fn.Extent.Text))}
    }
    Connect-ReadOnlyChrome
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console"}|Out-Null;WaitJs 'document.readyState==="complete"' 20
    if($FundingOnly){
        Enter 'ops';Management 3
        WaitJs 'document.querySelector(".funding-table")?.getAttribute("aria-busy")==="false"' 20
        Check 'document.querySelector(".settlement-management > .console-tabs").querySelectorAll("[role=tab]")[3].disabled===false' 'Fourth management tab is activated'
        Check '!document.querySelector(".funding-management [role=alert]") && document.querySelectorAll(".funding-table th").length===7' 'Funding component receives live props and renders its authorized API response'
        if(-not $script:fundingQueries.Count){throw 'Funding API read was not observed'}
        $result.checks+=,'Actual funding-adjustments API GET is observed'
        Check '!!document.querySelector(".funding-toolbar button[aria-label=\"\u65b0\u589e\u8865\u8db3\u63d0\u6848\"]")' 'Platform operator has the funding proposal entry'
        foreach($width in @(1440,390)){
            Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
            Js 'document.querySelector(".funding-management").scrollIntoView({block:"start"});true'|Out-Null
            Start-Sleep -Milliseconds 300
            Check 'document.documentElement.scrollWidth<=innerWidth' ('Funding entry fits '+$width)
            $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false};$name='funding-entry-'+$width+'.png'
            [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data));$result.screenshots+=,$name
        }
        Enter 'provider';Management 3
        WaitJs 'document.querySelector(".funding-table")?.getAttribute("aria-busy")==="false"' 20
        Check '!document.querySelector(".funding-management [role=alert]") && !document.querySelector(".funding-toolbar button[aria-label=\"\u65b0\u589e\u8865\u8db3\u63d0\u6848\"]") && document.querySelector(".funding-toolbar").textContent.includes("\u53ea\u8bfb")' 'Provider funding entry remains scoped and read-only'
        if(-not @($script:fundingQueries|Where-Object {$_ -match 'enterprise_id=provider_tenant' -and $_ -match 'business_view=provider'}).Count){throw 'Provider funding read did not preserve explicit enterprise/business scope'}
        $result.checks+=,'Provider funding GET pins enterprise_id and business_view'
        $result.funding_queries=@($script:fundingQueries)
    } else {
    if(-not $EvidenceOnly){
    Enter 'provider';Management 0
    Click '.settlement-management > .toolbar .secondary-btn'
    Fill '.settlement-management form.editor input[maxlength="180"]' 'Synthetic browser expert'
    Fill '.settlement-management form.editor select' 'expert' 'change'
    Check 'document.querySelector(".settlement-management form.editor input[readonly]").value==="provider_tenant"' 'Provider registration pins the enterprise scope'
    Click '.settlement-management form.editor button.primary-btn'
    WaitJs '!document.querySelector(".settlement-management form.editor") && document.querySelector(".settlement-management tbody")?.textContent.includes("Synthetic browser expert")' 20
    Check 'document.querySelector(".settlement-management tbody").textContent.includes("Synthetic browser expert")' 'Real API registers a named beneficiary through the browser'
    Fill '.settlement-management > nav.pagination-controls select' '50' 'change'
    Check 'document.querySelector(".settlement-management > nav.pagination-controls select").value==="50"' 'Management page-size control updates its actual state'
    Management 1
    WaitJs 'document.querySelector(".settlement-management .product-filter select")?.options.length>1' 20
    Check 'document.querySelector(".settlement-management .product-filter select").textContent.includes("Test product")' 'Product chooser displays authorized product names'
    Fill '.settlement-management .product-filter select' 'product' 'change'
    Click '.settlement-management .product-filter:has(select) button'
    WaitJs '!!document.querySelector(".settlement-management form.editor")' 20
    Click '.settlement-management form.editor button.secondary-btn'
    Js '(()=>{const s=document.querySelector(".settlement-management .allocation-row select");s.value=[...s.options].find(o=>o.textContent.includes("Synthetic browser expert")).value;s.dispatchEvent(new Event("change",{bubbles:true}));return true})()'|Out-Null
    Fill '.settlement-management .allocation-row input' '100'
    Click '.settlement-management form.editor button.primary-btn'
    WaitJs 'document.querySelector(".settlement-management form.editor h3")?.textContent.includes("1")' 20
    Check '!document.querySelector(".settlement-management [role=alert]")' 'Real API saves category allocation version1'
    Management 2
    Click '.settlement-management > .toolbar .secondary-btn'
    WaitJs 'document.querySelector(".settlement-management form.editor select")?.options.length>1 && !document.querySelector(".settlement-management form.editor select").disabled' 20
    Fill '.settlement-management form.editor select' 'clr2-management-refund-1' 'change'
    WaitJs '!!document.querySelector(".settlement-management form.editor dl") && !document.querySelector(".settlement-management form.editor button.primary-btn").disabled' 20
    Check 'document.querySelector(".settlement-management form.editor dl").textContent.includes("50.00")' 'Cost application shows the actual selected refund amount'
    Check 'document.querySelector(".settlement-management form.editor .table-scroll").textContent.includes("SET-CLR2-BROWSER")' 'Cost application shows frozen original settlement and hash evidence'
    Fill '.settlement-management form.editor input[inputmode=decimal]' '20.00'
    Js '(()=>{const s=document.querySelectorAll(".settlement-management form.editor select[required]")[1];s.value=[...s.options].find(o=>o.textContent.includes("Synthetic cost bearer")).value;s.dispatchEvent(new Event("change",{bubbles:true}));return true})()'|Out-Null
    Fill '.settlement-management form.editor textarea[maxlength]' 'Synthetic nonreturnable invoice'
    Fill '.settlement-management form.editor textarea:not([maxlength])' 'synthetic-invoice:1'
    Click '.settlement-management form.editor button.primary-btn'
    WaitJs '!document.querySelector(".settlement-management form.editor") && document.querySelector(".settlement-management tbody")?.textContent.includes("20.00")' 20
    Check 'document.querySelector(".settlement-management tbody").textContent.includes("20.00")' 'Browser creates a cost exception through the real API'
    Check '!document.querySelector(".settlement-management tbody button.secondary-btn")' 'Applicant and same-side users have no cost approval controls'
    $script:decisionIds=@(Js '(async()=>{const r=await fetch("/api/settlement-cost-exceptions?enterprise_id=provider_tenant&business_view=provider",{headers:{Authorization:"Bearer "+localStorage.getItem("market_token"),"X-Enterprise-Id":"provider_tenant"}});return (await r.json()).items.map(x=>x.id)})()')
    Enter 'ops';Management 2
    Check 'document.querySelectorAll(".settlement-management tbody button.secondary-btn").length===2' 'Opposite platform side sees approve and reject controls'
    Click '.settlement-management tbody button.secondary-btn'
    Fill '.settlement-management form.editor textarea' 'Synthetic independent review'
    Click '.settlement-management form.editor button.primary-btn'
    WaitJs '!document.querySelector(".settlement-management form.editor") && document.querySelector(".settlement-management tbody")?.textContent.includes("\u5df2\u6279\u51c6")' 20
    Check 'document.querySelector(".settlement-management tbody").textContent.includes("\u5df2\u6279\u51c6")' 'Independent opposite-side reviewer approves the real exception'
    Click '.settlement-management tbody button.icon-btn'
    Check 'document.querySelector(".settlement-management aside").textContent.includes("synthetic-invoice:1") && document.querySelector(".settlement-management aside").textContent.includes("Synthetic independent review")' 'Approved exception exposes evidence references and reviewer reason'
    Enter 'platform';Workspace 3
    Click '.settlement-finance > .toolbar button.primary-btn'
    WaitJs '!!document.querySelector(".finance-modal")' 10
    Fill '.finance-modal input[type=number]' ([string]((Get-Date).Year-1))
    Fill '.finance-modal textarea' 'Synthetic annual snapshot'
    Click '.finance-modal button.primary-btn'
    WaitJs '!document.querySelector(".finance-modal") && document.querySelector(".settlement-finance tbody")?.textContent.includes("\u8349\u7a3f")' 20
    Check '!document.querySelector(".settlement-finance tbody button.secondary-btn")' 'Annual snapshot maker cannot finalize own snapshot'
    $script:annualIds=@(Js '(async()=>{const r=await fetch("/api/settlement-reporting/annual-reports",{headers:{Authorization:"Bearer "+localStorage.getItem("market_token")}});return (await r.json()).items.filter(x=>x.note==="Synthetic annual snapshot").map(x=>x.id)})()')
    if($script:annualIds.Count -ne 1){throw 'Expected one isolated prior-year draft'}
    Enter 'ops';Workspace 3
    Check '!!document.querySelector(".settlement-finance tbody button.secondary-btn")' 'Independent operator can open annual finalization'
    Click '.settlement-finance tbody button.secondary-btn'
    WaitJs '!!document.querySelector(".finance-modal")' 10
    Fill '.finance-modal input[maxlength="240"]' 'SYNTHETIC-ANNUAL-RECONCILIATION'
    Fill '.finance-modal textarea' 'Synthetic independent review'
    Click '.finance-modal button.primary-btn'
    WaitJs '!document.querySelector(".finance-modal") && document.querySelector(".settlement-finance tbody")?.textContent.includes("\u6b63\u5f0f\u5feb\u7167")' 25
    Check 'document.querySelector(".settlement-finance tbody").textContent.includes("\u6b63\u5f0f\u5feb\u7167")' 'Synthetic prior-year internal-profit snapshot is independently finalized by real API'
    Fill '.annual-view input[type=number]' ([string]((Get-Date).Year-1))
    Click '.annual-view button'
    WaitJs '!!document.querySelector(".annual-view .balance-bridge")' 20
    Check 'document.querySelectorAll(".annual-view tbody tr").length>=12' 'Annual dynamic view renders twelve months and balance bridge, not JSON'
    } else {Enter 'ops'}
    Workspace 0
    Fill '.settlement-finance .main-list-filters input[type=text]' 'SET-SYNTHETIC-EVIDENCE'
    Click '.settlement-finance .main-list-filters button.primary-btn'
    WaitJs 'document.querySelector(".settlement-finance > .table-scroll tbody")?.textContent.includes("SET-SYNTHETIC-EVIDENCE")' 20
    Click '.settlement-finance > .table-scroll tbody button.icon-btn'
    WaitJs 'document.querySelectorAll(".economic-segments .source-segment").length===2' 20
    Check 'document.querySelector(".report-evidence").textContent.includes("\u5e73\u53f0") && document.querySelectorAll(".report-evidence > .table-scroll table tbody tr").length>=5' 'Report detail renders authoritative five-party rates and amounts'
    Check '[...document.querySelectorAll(".economic-segments .source-segment")].every(s=>s.textContent.includes("ORD-CLR2-BROWSER")&&s.textContent.includes("SET-CLR2-BROWSER")&&s.textContent.includes("BATCH-CLR2-BROWSER")&&/[a-f0-9]{64}/.test(s.textContent)&&s.textContent.includes("-50.00")&&s.querySelectorAll("table:last-child tbody tr").length===5)' 'Synthetic read-only manifest renders both source business numbers, original batches, final hashes and five-party segment amounts'
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$true}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir 'report-evidence-1440.png'),[Convert]::FromBase64String($shot.data));$result.screenshots+=,'report-evidence-1440.png'
    Management 2
    Js 'document.querySelector(".settlement-management tbody button.icon-btn")?.click();true'|Out-Null
    foreach($width in @(1440,390)){
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
        Js 'document.querySelector(".settlement-management").scrollIntoView({block:"start"});true'|Out-Null
        Start-Sleep -Milliseconds 300
        Check '(()=>{const r=document.querySelector(".settlement-management").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&document.documentElement.scrollWidth<=innerWidth})()' ('Management layout fits '+$width)
        $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false};$name='management-'+$width+'.png'
        [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data));$result.screenshots+=,$name
    }
    Enter 'security'
    Check 'document.querySelector(".settlement-finance > .console-tabs").querySelectorAll("[role=tab]").length===4' 'Read-only security role receives no management capability'
    Check '!document.querySelector(".settlement-management")' 'Read-only role cannot see management write forms'
    }
    if($script:blocked -or $script:nativeDialogs){throw 'Unexpected request or native dialog'}
    $result.writes=@($script:writes);$result.status='passed'
} catch {$result.error=$_.Exception.Message;try{$result.failure_ui=Js 'document.querySelector("main")?.innerText.slice(0,6000)'}catch{}}
finally {
    if($script:socket){$script:socket.Dispose()};if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $result|ConvertTo-Json -Depth 12|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
}
if($result.status -cne 'passed'){throw $result.error};Write-Output ('Passed: '+$result.checks.Count+' management browser checks')
