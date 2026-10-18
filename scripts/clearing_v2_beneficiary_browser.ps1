param([Parameter(Mandatory=$true)][string]$TokenDirectory,
      [string]$BaseUrl='http://127.0.0.1:8126',
      [string]$OutputDir="$env:TEMP\market-clearing-beneficiary",
      [switch]$CheckParticipantLabels)
$ErrorActionPreference='Stop'
if($BaseUrl -cne 'http://127.0.0.1:8126'){throw 'Only the local disposable beneficiary fixture is supported'}
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-beneficiary-'+[Guid]::NewGuid().ToString('N'))
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$script:paths=[Collections.Generic.List[string]]::new()
$script:reportQueries=[Collections.Generic.List[string]]::new()
$script:platformReportPhase=$false
$result=@{status='failed';checks=@();screenshots=@();scope='disposable synthetic SQLite fixture; browser GET/HEAD only'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $u=[Uri]$url
        if($u.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $u.UserInfo -or $postData -or $method -cnotin @('GET','HEAD')){return $false}
        $script:paths.Add($u.AbsolutePath)
        if($u.AbsolutePath -ceq '/api/settlement-reporting/report'){$script:reportQueries.Add($u.Query)}
        if($u.AbsolutePath -cmatch '^/api/(enterprise-settlements|settlements|settlement-finance)(/|$)'){return $script:platformReportPhase}
        return $true
    } catch {return $false}
}
function Check([string]$expression,[string]$description) {
    if(-not (Js $expression)){throw $description}
    $result.checks+=,$description
}
function Ready {
    WaitJs 'document.querySelector(".settlement-finance")?.getAttribute("aria-busy")==="false" && !!document.querySelector(".settlement-finance tbody tr")' 30
    Check '!document.querySelector(".settlement-finance [role=alert]")' 'Report has no API contract error'
}
function Enter([string]$actor) {
    $secret=ConvertTo-Json (Read-PrivateToken ($TokenDirectory.TrimEnd('/')+'/'+$actor+'.token')) -Compress
    Js "localStorage.setItem('market_token',$secret);window.__previousOwnDocument=true;true"|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=earnings"}|Out-Null
    WaitJs '!window.__previousOwnDocument && document.readyState==="complete"' 20
    Ready
    Check '[...document.querySelectorAll(".nav-item")].some(x=>x.textContent.includes("\u6211\u7684\u6536\u76ca"))' ($actor+' has the My Earnings entry')
    Check '![...document.querySelectorAll(".nav-item")].some(x=>x.textContent.includes("\u6e05\u7b97\u5206\u8d26"))' ($actor+' does not receive the full settlement entry')
}
function Private-Report {
    Check 'document.querySelectorAll(".settlement-finance > .console-tabs")[0].querySelectorAll("[role=tab]").length===1' 'Only the report workspace tab is available'
    Check '!/\u4f9b\u8d27\u6210\u672c|\u5e73\u53f0\u5206\u6210|\u63d0\u4f9b\u65b9\u5229\u6da6\u5206\u6210|\u8d21\u732e\u6392\u540d|PRIVATE-/.test(document.querySelector(".settlement-finance").textContent)' 'No cost, third-party share, ranking, buyer order or private voucher is displayed'
    Check '![...document.querySelectorAll(".settlement-finance button")].some(x=>/\u767b\u8bb0|\u4ed8\u6b3e|\u590d\u6838|\u521b\u5efa/.test(x.textContent)) && !document.querySelector(".finance-modal")' 'No financial write controls are available'
}
function Select-Identity([string]$identity) {
    $id=ConvertTo-Json $identity -Compress
    Js "(()=>{const s=document.querySelector('.report-filter-bar select');s.value=$id;window.__expectedOwnName=s.selectedOptions[0].textContent.split(String.fromCharCode(183))[0].trim();s.dispatchEvent(new Event('change',{bubbles:true}));return true})()"|Out-Null
    WaitJs 'document.querySelector(".workspace-heading span")?.textContent.trim()===window.__expectedOwnName' 20
    Ready
    WaitJs "document.querySelector('.report-filter-bar select')?.value===$id" 10
}
function Export-Own([string]$identity) {
    Js 'window.__ownCsv=null;window.__originalBlobUrl=window.__originalBlobUrl||URL.createObjectURL;URL.createObjectURL=function(blob){window.__ownCsv=blob;return window.__originalBlobUrl.call(URL,blob)};[...document.querySelectorAll(".settlement-finance .toolbar button")].find(b=>b.textContent.includes("\u660e\u7ec6")).click();true'|Out-Null
    WaitJs '!!window.__ownCsv' 20
    $csv=Js '(async()=>await window.__ownCsv.text())()'
    if($csv -match 'PRIVATE-|180\.00|258\.00'){throw 'CSV leaked private counterpart or original order amounts'}
    $rows=@($csv|ConvertFrom-Csv)
    $idHeader=ConvertFrom-Json '"\u6536\u76ca\u4e3b\u4f53\u5173\u8054ID"'
    $costHeader=ConvertFrom-Json '"\u4f9b\u8d27\u6210\u672c"'
    if(-not $rows.Count){throw 'Expected own fixture CSV data'}
    foreach($row in $rows){
        if($row.$idHeader -cne $identity){throw 'CSV contains another beneficiary identity'}
        if($row.PSObject.Properties.Name -contains $costHeader -and $row.$costHeader -cne '0.00'){throw 'CSV contains actual supplier costs'}
    }
    $result.checks+=,('Own-only CSV scope '+$identity)
}
try {
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    foreach($file in @(@{path='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{path='development_steps_browser.ps1';names=@('Read-PrivateToken','Connect-ReadOnlyChrome')})) {
        $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $file.path),[ref]$null,[ref]$null)
        foreach($name in $file.names){$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false);if(-not $fn){throw 'CDP helper missing'};. ([ScriptBlock]::Create($fn.Extent.Text))}
    }
    Connect-ReadOnlyChrome
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console"}|Out-Null
    WaitJs 'document.readyState==="complete"' 20
    Enter 'expert'
    Check '(()=>{const s=document.querySelector(".report-filter-bar select"),css=getComputedStyle(s),band=getComputedStyle(s.parentElement.parentElement);return s.options.length===2&&css.height==="34px"&&css.fontSize==="12px"&&css.borderTopWidth==="1px"&&css.borderRadius==="4px"&&band.borderTopWidth==="0px"&&band.backgroundColor==="rgba(0, 0, 0, 0)"})()' 'Pure expert identity selector has both authorized choices and compact unframed platform styling'
    Select-Identity 'clr2-expert-own'
    Check 'document.querySelector(".settlement-finance").textContent.includes("12.00") && document.querySelector(".settlement-finance").textContent.includes("5.00")' 'Expert report uses real positions and confirmed partial payment'
    Private-Report
    Export-Own 'clr2-expert-own'
    Select-Identity 'clr2-expert-channel'
    WaitJs 'document.querySelector(".settlement-finance").textContent.includes("Fixture Expert Channel")' 10
    Check 'document.querySelector(".settlement-finance").textContent.includes("7.00") && !document.querySelector(".settlement-finance").textContent.includes("12.00")' 'Switching identity replaces the report without stale expert values'
    Private-Report
    Export-Own 'clr2-expert-channel'
    Check '(async()=>{const r=await fetch("/api/settlement-reporting/report?business_view=beneficiary&participant_id=clr2-other-expert",{headers:{Authorization:"Bearer "+localStorage.getItem("market_token")}});return r.status===403})()' 'Foreign beneficiary report is rejected by the real backend'
    Check '(async()=>{const r=await fetch("/api/settlement-reporting/export?business_view=beneficiary&participant_id=clr2-other-expert",{headers:{Authorization:"Bearer "+localStorage.getItem("market_token")}});return r.status===403})()' 'Foreign beneficiary CSV is rejected by the real backend'
    Enter 'channel'
    Check 'document.querySelector(".report-filter-bar select").options.length===1 && document.querySelector(".settlement-finance").textContent.includes("3.00")' 'Pure channel sees only its own identity and amount'
    Private-Report
    Export-Own 'clr2-channel-own'
    Enter 'expert_member'
    Check 'document.querySelector(".report-filter-bar select").options.length===1 && document.querySelector(".settlement-finance").textContent.includes("8.00")' 'Current-enterprise ordinary member sees own expert income without provider privileges'
    Private-Report
    Export-Own 'clr2-member-expert'
    if(-not ($script:reportQueries|Where-Object {$_ -match 'participant_id=clr2-member-expert' -and $_ -match 'enterprise_id=buyer_tenant'})){throw 'Enterprise interceptor scope was not preserved'}
    $result.checks+=,'Current-enterprise header and explicit report query agree'
    foreach($width in @(1440,390)) {
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
        Start-Sleep -Milliseconds 300
        $geometry=Js '(()=>{const r=document.querySelector(".settlement-finance").getBoundingClientRect();return {left:r.left,right:r.right,width:innerWidth,documentWidth:document.documentElement.scrollWidth,overflow:[...document.querySelectorAll("body *")].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.right>innerWidth+1&&!e.closest(".table-scroll,.sidebar")}).slice(0,6).map(e=>({tag:e.tagName,classes:e.className,width:e.getBoundingClientRect().width}))}})()'
        $result[('geometry'+$width)]=$geometry
        $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
        $name='beneficiary-'+$width+'.png'
        [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
        $result.screenshots+=,$name
        Check '(()=>{const r=document.querySelector(".settlement-finance").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&document.documentElement.scrollWidth<=innerWidth+1})()' ('Own-report layout fits '+$width)
    }
    if($CheckParticipantLabels) {
        $script:platformReportPhase=$true
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
        $secret=ConvertTo-Json (Read-PrivateToken ($TokenDirectory.TrimEnd('/')+'/platform.token')) -Compress
        Js "localStorage.setItem('market_token',$secret);window.__previousOwnDocument=true;true"|Out-Null
        Cdp 'Page.navigate' @{url="$BaseUrl/console?view=settlements"}|Out-Null
        WaitJs '!window.__previousOwnDocument && !!document.querySelector(".settlement-tabs")' 30
        Js '[...document.querySelectorAll(".settlement-tabs button")].find(b=>b.textContent.includes("\u62a5\u8868")).click();true'|Out-Null
        Ready
        Js '[...document.querySelectorAll(".settlement-finance [role=tab]")].find(b=>b.textContent.trim()==="\u6536\u76ca\u4e3b\u4f53").click();true'|Out-Null
        WaitJs '[...document.querySelectorAll(".settlement-finance th")].some(t=>t.textContent==="\u627f\u62c5\u635f\u5931")' 10
        Check '[...document.querySelectorAll(".settlement-finance th")].some(t=>t.textContent==="\u5408\u8ba1\u51c0\u5e94\u6536\uff08\u6210\u672c+\u5229\u6da6-\u635f\u5931\uff09")' 'Platform participant net receivable label explicitly subtracts loss'
        Check '(async()=>{const response=await fetch("/api/settlement-reporting/report",{headers:{Authorization:"Bearer "+localStorage.getItem("market_token")}});if(!response.ok)return false;const data=await response.json();const table=document.querySelector(".settlement-finance .table-scroll table");const rows=[...table.tBodies[0].rows];return data.participants.length>0&&data.participants.every(p=>{const row=rows.find(r=>r.cells[0].textContent===p.participant_name);return row&&["cost_amount","profit_share","loss_amount","total_receivable"].every((key,i)=>row.cells[3+i].textContent===String(p[key]).replace(/\B(?=(\d{3})+(?!\d))/g,","))})})()' 'Platform cost, ordinary signed profit, positive loss and net total display actual API decimal strings independently'
        Js 'document.querySelector(".settlement-finance table").scrollIntoView({block:"start"});true'|Out-Null
        $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
        [IO.File]::WriteAllBytes((Join-Path $OutputDir 'participant-labels-1440.png'),[Convert]::FromBase64String($shot.data))
        $result.screenshots+=,'participant-labels-1440.png'
    }
    if($script:blocked -or $script:nativeDialogs){throw 'Unexpected write, full-settlement request or native dialog'}
    $result.status='passed'
} catch {$result.error=$_.Exception.Message}
finally {
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $result|ConvertTo-Json -Depth 10|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
}
if($result.status -cne 'passed'){throw $result.error}
Write-Output ('Passed: '+$result.checks.Count+' beneficiary browser checks')
