param([Parameter(Mandatory=$true)][string]$TokenFile,
      [string]$BaseUrl='http://192.168.10.10:30080',
      [string]$OutputDir="$env:TEMP\market-gateway-modal",
      [switch]$Before)
$ErrorActionPreference='Stop'
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-gateway-'+[Guid]::NewGuid().ToString('N'))
$result=@{status='failed';checks=@();screenshots=@();scope='installed gateway read-only UI; no business writes'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $u=[Uri]$url
        return $method -cin @('GET','HEAD') -and -not $postData -and $u.GetLeftPart([UriPartial]::Authority) -ceq $BaseUrl -and -not $u.UserInfo
    } catch {return $false}
}
function Check([string]$expression,[string]$description) {
    if(-not (Js $expression)){throw $description}
    $result.checks+=,$description
}
function Shot([string]$name) {
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
    $result.screenshots+=,$name
}
try {
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    foreach($file in @(@{path='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{path='development_steps_browser.ps1';names=@('Read-PrivateToken','Connect-ReadOnlyChrome')})) {
        $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $file.path),[ref]$null,[ref]$null)
        foreach($name in $file.names){$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false); . ([ScriptBlock]::Create($fn.Extent.Text))}
    }
    $token=Read-PrivateToken $TokenFile
    $origin=ConvertTo-Json $BaseUrl -Compress; $secret=ConvertTo-Json $token -Compress
    Connect-ReadOnlyChrome
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source="if(location.origin===$origin){localStorage.setItem('market_token',$secret);}"}|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=gateway"}|Out-Null
    WaitJs 'document.querySelector(".pagination-controls")?.getAttribute("aria-busy")==="false"' 40
    Js '(()=>{const s=document.querySelector(".main-list-filters select");s.value="all";s.dispatchEvent(new Event("change",{bubbles:true}));document.querySelector(".main-list-filters").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));return true})()'|Out-Null
    WaitJs '!!document.querySelector("button[title=\"\u67e5\u770b\u8def\u7531\u8be6\u60c5\"]") && document.querySelector(".pagination-controls")?.getAttribute("aria-busy")==="false"' 30
    Js 'document.querySelector("button[title=\"\u67e5\u770b\u8def\u7531\u8be6\u60c5\"]").click();true'|Out-Null
    WaitJs '!!document.querySelector(".route-details") && ![...document.querySelectorAll(".pagination-controls")].some(e=>e.getAttribute("aria-busy")==="true")' 30
    $result.layout=Js 'JSON.stringify([...document.querySelectorAll(".pagination-controls")].map(p=>({label:p.getAttribute("aria-label"),width:p.clientWidth,children:[...p.children].map(e=>({tag:e.tagName,top:e.getBoundingClientRect().top,width:e.getBoundingClientRect().width,direction:getComputedStyle(e).flexDirection}))})))'
    if($Before){Shot 'gateway-before.png';$result.status='passed'}
    else {
        Check 'document.querySelectorAll(".route-details [role=tab]").length===3' 'Gateway details use three standard tabs'
        Check '!!document.querySelector(".route-details[role=dialog], [role=dialog] .route-details")' 'Gateway details open in a dialog'
        Check '[...document.querySelectorAll(".pagination-controls")].every(p=>{const y=[...p.children].filter(e=>!e.classList.contains("pagination-error")).map(e=>e.getBoundingClientRect().top+e.getBoundingClientRect().height/2);return Math.max(...y)-Math.min(...y)<2})' 'Desktop list and dialog pagination align on one row'
        foreach($i in @(1,2,0)) {
            Js "document.querySelectorAll('.route-details [role=tab]')[$i].click();true"|Out-Null
            WaitJs '![...document.querySelectorAll(".route-details .pagination-controls")].some(e=>e.getAttribute("aria-busy")==="true")' 30
            Check "document.querySelectorAll('.route-details [role=tab]')[$i].getAttribute('aria-selected')==='true'" ('Tab '+$i+' is selectable')
        }
        Shot 'gateway-modal-desktop.png'
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=1;mobile=$false}|Out-Null
        Js '(async()=>{await new Promise(r=>setTimeout(r,500));return true})()'|Out-Null
        Check '(()=>{const d=document.querySelector(".route-details[role=dialog], [role=dialog]:has(.route-details)");const r=d.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1})()' 'Mobile dialog stays within viewport'
        Check '[...document.querySelectorAll(".route-details .pagination-controls label")].every(e=>getComputedStyle(e).flexDirection==="row"&&e.getBoundingClientRect().height<=31)' 'Mobile pagination labels remain compact horizontal controls'
        Shot 'gateway-modal-mobile.png'
        Cdp 'Input.dispatchKeyEvent' @{type='keyDown';key='Escape';code='Escape';windowsVirtualKeyCode=27}|Out-Null
        Cdp 'Input.dispatchKeyEvent' @{type='keyUp';key='Escape';code='Escape';windowsVirtualKeyCode=27}|Out-Null
        WaitJs '!document.querySelector(".route-details")' 10
        $result.checks+=,'Escape closes gateway details'
        if($script:blocked -or $script:nativeDialogs){throw 'Unexpected mutation or native dialog'}
        $result.status='passed'
    }
} catch {$result.error=$_.Exception.Message}
finally {
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $result|ConvertTo-Json -Depth 12|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
}
if($result.status -cne 'passed'){throw $result.error}
Write-Output ('Passed: '+$result.checks.Count+' gateway checks')
