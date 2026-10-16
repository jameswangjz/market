param([Parameter(Mandatory=$true)][string]$TokenFile,
      [string]$BaseUrl='http://192.168.10.10:30080',
      [string]$OutputDir="$env:TEMP\market-slc-browser",
      [switch]$SlaOnly)
$ErrorActionPreference='Stop'
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-slc-'+[Guid]::NewGuid().ToString('N'))
$result=@{status='failed';checks=@();screenshots=@();scope='installed administrator read-only UI; no configuration writes'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $u=[Uri]$url
        if($method -cnotin @('GET','HEAD') -or $postData -or $u.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $u.UserInfo){return $false}
        return $u.AbsolutePath -cmatch '^/(?:assets/|api/)' -or $u.AbsolutePath -cin @('/console','/favicon.ico','/logo.svg','/market-build.json')
    } catch {return $false}
}
function Check([string]$expression,[string]$description) {
    if(-not (Js $expression)){throw $description}
    $result.checks+=,$description
}
function Open-View([string]$view,[string]$ready) {
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=$view"}|Out-Null
    WaitJs $ready 30
}
function Save-SlaShot([string]$name) {
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    Js '(async()=>{await new Promise(r=>setTimeout(r,500));window.scrollTo(0,0);await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return true})()'|Out-Null
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
    $result.screenshots+=,$name
}
try {
    $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'password_first_purchase_browser_cdp.ps1'),[ref]$null,[ref]$null)
    foreach($name in @('Send-Cdp','Cdp','Js','WaitJs')) {
        $fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false)
        . ([ScriptBlock]::Create($fn.Extent.Text))
    }
    $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'development_steps_browser.ps1'),[ref]$null,[ref]$null)
    foreach($name in @('Read-PrivateToken','Connect-ReadOnlyChrome')) {
        $fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false)
        . ([ScriptBlock]::Create($fn.Extent.Text))
    }
    $token=Read-PrivateToken $TokenFile
    $origin=ConvertTo-Json $BaseUrl -Compress; $secret=ConvertTo-Json $token -Compress
    Connect-ReadOnlyChrome
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source="if(location.origin===$origin){localStorage.setItem('market_token',$secret);}"}|Out-Null
    Open-View 'sla' '!!document.querySelector(".sla-management")'
    Check 'document.querySelectorAll(".sla-management [role=tab]").length===4' 'SLA exposes four workspaces'
    WaitJs 'document.querySelectorAll(".sla-management .metric-card").length===3' 30
    Check '!document.querySelector(".sla-management details.sla-history")' 'Obsolete rule history and counter removed'
    if($SlaOnly){Save-SlaShot 'sla-overview.png'}
    Js 'document.querySelectorAll(".sla-management [role=tab]")[1].click();true'|Out-Null
    WaitJs 'document.querySelector(".sla-matrix-table tbody tr") && !document.querySelector(".sla-matrix-panel [role=alert]")' 30
    Check 'document.querySelectorAll(".sla-matrix-table tbody tr").length>=14' 'Complete metric matrix is rendered'
    Check '!document.querySelector(".sla-readonly-grid input")' 'Fixed calendar and support metadata are read-only'
    Check '[...document.querySelectorAll(".sla-management label,.sla-management input,.sla-management select,.sla-management textarea,.sla-management option")].every(e=>getComputedStyle(e).fontSize==="12px")' 'Matrix field labels, inputs and native options consistently use 12px'
    if($SlaOnly){Save-SlaShot 'sla-matrix.png'}
    Js 'document.querySelectorAll(".sla-management [role=tab]")[2].click();true'|Out-Null
    WaitJs '!!document.querySelector(".sla-management .main-list-filters") && document.querySelector(".sla-management .pagination-controls")?.getAttribute("aria-busy")==="false"' 30
    Check 'document.querySelectorAll(".sla-management .main-list-filters label").length>=4' 'Enterprise binding filters and server pagination exist'
    Check 'document.querySelectorAll(".sla-management table").length===1' 'Redundant assignment history table removed'
    Js '(()=>{const b=[...document.querySelectorAll(".sla-management .table-wrap .text-btn")].find(e=>!e.disabled);if(b)b.click();return true})()'|Out-Null
    Check '[...document.querySelectorAll(".sla-management label,.sla-management input,.sla-management select,.sla-management textarea,.sla-management option")].every(e=>getComputedStyle(e).fontSize==="12px")' 'Binding filters and edit form consistently use 12px'
    if($SlaOnly){Save-SlaShot 'sla-bindings.png'}
    Js 'document.querySelectorAll(".sla-management [role=tab]")[3].click();true'|Out-Null
    WaitJs '!!document.querySelector(".sla-evidence-panel,.sla-evidence")' 30
    $result.checks+=,'Frozen promises and evidence tab opens'
    Check '[...document.querySelectorAll(".sla-management label,.sla-management input,.sla-management select,.sla-management textarea")].every(e=>getComputedStyle(e).fontSize==="12px")' 'Evidence controls consistently use 12px'
    if(-not $SlaOnly) {
    Open-View 'delivery' 'document.querySelectorAll(".console-tabs [role=tab]").length===3'
    foreach($index in @(0,1,2)) {
        Js "document.querySelectorAll('.console-tabs [role=tab]')[$index].click();true"|Out-Null
        WaitJs '(()=>{const p=[...document.querySelectorAll(".pagination-controls")].filter(e=>e.getBoundingClientRect().height>0);return p.length===1&&p[0].getAttribute("aria-busy")==="false"})()' 30
        Check '(()=>{const f=[...document.querySelectorAll(".main-list-filters,.refund-filters")].find(e=>e.getBoundingClientRect().height>0);return !!f&&[...f.querySelectorAll("input,select")].every(e=>Math.abs(e.getBoundingClientRect().height-34)<1)})()' ('Delivery tab '+$index+' uses consistent compact filters')
        Check '(()=>{const p=[...document.querySelectorAll(".pagination-controls")].find(e=>e.getBoundingClientRect().height>0);return !!p&&!p.querySelector(".pagination-error")&&[...p.querySelectorAll("label")].every(e=>getComputedStyle(e).display==="flex")})()' ('Delivery tab '+$index+' pagination has no empty error row')
    }
    Open-View 'users' 'document.querySelectorAll("[role=tab]").length>=2'
    foreach($index in @(0,1)) {
        Js "document.querySelectorAll('[role=tab]')[$index].click();true"|Out-Null
        WaitJs '!!document.querySelector(".main-list-filters") && document.querySelector(".pagination-controls")?.getAttribute("aria-busy")==="false"' 30
        Check '!!document.querySelector(".pagination-controls select") && !!document.querySelector(".pagination-controls input")' ('Identity tab '+$index+' has page size and page jump')
    }
    Open-View 'audit' '!!document.querySelector(".audit-search-grid")'
    WaitJs 'document.querySelector(".audit-search button")?.disabled===false && document.querySelector(".audit-panel .table-wrap")?.getAttribute("aria-busy")==="false"' 30
    Check 'document.querySelectorAll(".audit-search-grid label").length===8' 'Audit filters retain eight conditions'
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    foreach($viewport in @(@{name='desktop';width=1440;height=1000},@{name='mobile';width=390;height=844})) {
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$viewport.width;height=$viewport.height;deviceScaleFactor=1;mobile=$false}|Out-Null
        Js '(async()=>{await new Promise(r=>setTimeout(r,500));await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));const f=document.querySelector(".audit-search-grid"),h=document.querySelector(".topbar")?.getBoundingClientRect().height||66;window.scrollTo(0,Math.max(0,f.getBoundingClientRect().top+scrollY-h-16));await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return true})()'|Out-Null
        Check '(()=>{const f=document.querySelector(".audit-search-grid"),p=f.getBoundingClientRect();return [...f.querySelectorAll("input,select")].every(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.left>=p.left-2&&r.right<=p.right+2})})()' ($viewport.name+' audit controls fit parent')
        if($viewport.name -ceq 'desktop') {Check 'new Set([...document.querySelectorAll(".audit-search-grid label")].map(e=>Math.round(e.getBoundingClientRect().top))).size===2' 'Desktop audit filters use two rows'}
        $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
        $name='slc-audit-'+$viewport.name+'.png'
        [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
        $result.screenshots+=,$name
    }
    } else {
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=1;mobile=$false}|Out-Null
        Save-SlaShot 'sla-mobile.png'
    }
    if($script:blocked -or $script:nativeDialogs){throw 'Unexpected mutation or native dialog'}
    $result.status='passed'
} catch {$result.error=$_.Exception.Message; $result.stage=$result.checks.Count}
finally {
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome -and -not $script:chrome.HasExited){Stop-Process -Id $script:chrome.Id -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $result|ConvertTo-Json -Depth 10|Set-Content -LiteralPath (Join-Path $OutputDir 'result.json') -Encoding UTF8
    $result|ConvertTo-Json -Depth 10
}
if($result.status -cne 'passed'){exit 1}
