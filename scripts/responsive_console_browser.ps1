param([Parameter(Mandatory=$true)][string]$TokenFile,
      [string]$BaseUrl='http://192.168.10.10:30080',
      [string]$OutputDir="$env:TEMP\market-responsive-console")
$ErrorActionPreference='Stop'
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-responsive-'+[Guid]::NewGuid().ToString('N'))
$result=@{status='failed';checks=@();screenshots=@();layouts=@();scope='installed console read-only responsive layout; simulated CSS viewport and DPR, not physical monitor calibration'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {$u=[Uri]$url;return $method -cin @('GET','HEAD') -and -not $postData -and $u.GetLeftPart([UriPartial]::Authority) -ceq $BaseUrl -and -not $u.UserInfo} catch {return $false}
}
function Check([string]$expression,[string]$description) {
    if(-not (Js $expression)){throw $description}
    $result.checks+=,$description
}
function Set-Screen($screen) {
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=$screen.w;height=$screen.h;deviceScaleFactor=$screen.dpr;mobile=$false}|Out-Null
    Js '(async()=>{await new Promise(r=>setTimeout(r,350));await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return true})()'|Out-Null
}
function Check-Layout([string]$name) {
    Check '(()=>{const m=document.querySelector(".main-shell").getBoundingClientRect(),c=document.querySelector(".content").getBoundingClientRect();return Math.abs(c.left-m.left)<2&&Math.abs(c.right-m.right)<2&&Math.abs(m.right-document.documentElement.clientWidth)<2})()' ($name+' content fills available main region')
    Check 'getComputedStyle(document.querySelector(".app-shell")).zoom==="1" && !document.documentElement.style.getPropertyValue("--ui-scale")' ($name+' has no second DPI zoom')
    Check 'document.documentElement.scrollWidth<=innerWidth+1' ($name+' has no page horizontal overflow')
    $result.layouts+=,(Js 'JSON.stringify({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,content:document.querySelector(".content").getBoundingClientRect().width,main:document.querySelector(".main-shell").getBoundingClientRect().width,heading:getComputedStyle(document.querySelector(".page-heading h1")||document.querySelector("h1")).fontSize})')
}
function Shot([string]$name) {
    Js 'window.scrollTo(0,0);true'|Out-Null
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
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source="if(location.origin===$origin){localStorage.setItem('market_token',$secret);}"}|Out-Null
    Set-Screen @{w=1366;h=768;dpr=1.25}
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=products"}|Out-Null
    WaitJs '!!document.querySelector(".main-shell .content .table-wrap") && document.querySelector(".pagination-controls")?.getAttribute("aria-busy")==="false"' 40
    $screens=@(@{name='laptop';w=1366;h=768;dpr=1.25},@{name='fullhd';w=1920;h=1080;dpr=1},@{name='qhd';w=2560;h=1440;dpr=1},@{name='hidpi';w=2560;h=1440;dpr=2},@{name='ultrawide';w=3440;h=1440;dpr=1},@{name='portrait';w=900;h=1200;dpr=1.5},@{name='mobile';w=390;h=844;dpr=2},@{name='back-to-laptop';w=1366;h=768;dpr=1.25})
    foreach($screen in $screens) {
        Set-Screen $screen
        Check-Layout $screen.name
        if($screen.name -cin @('laptop','qhd','mobile')){Shot ($screen.name+'.png')}
    }
    Set-Screen @{w=2560;h=1440;dpr=1}
    Js 'document.querySelector(".desktop-collapse").click();true'|Out-Null
    Js '(async()=>{await new Promise(r=>setTimeout(r,350));return true})()'|Out-Null
    Check-Layout 'collapsed-sidebar'
    Check 'document.querySelector(".sidebar").classList.contains("collapsed")' 'Sidebar collapse remains supported'
    Js 'document.querySelector(".desktop-collapse").click();true'|Out-Null
    $viewLabels=@{orders='\u8ba2\u5355\u4e2d\u5fc3';gateway='API \u7f51\u5173';sla='SLA \u4fdd\u969c';settings='\u7cfb\u7edf\u8bbe\u7f6e'}
    foreach($view in @('orders','gateway','sla','settings')) {
        $label=$viewLabels[$view]
        Js "(()=>{const b=[...document.querySelectorAll('.sidebar .nav-item')].find(e=>e.textContent.includes('$label'));if(!b)throw new Error('Navigation entry missing');b.click();return true})()"|Out-Null
        WaitJs "!!document.querySelector('.sidebar .nav-item.active')?.textContent.includes('$label')" 30
        Js '(async()=>{await new Promise(r=>setTimeout(r,500));return true})()'|Out-Null
        Check-Layout $view
        if($view -ceq 'settings'){Check '!!document.querySelector(".content .settings-tabs")' 'Settings-specific width cap removed';Shot 'settings-qhd.png'}
    }
    if($script:blocked -or $script:nativeDialogs){throw 'Unexpected mutation or native dialog'}
    $result.status='passed'
} catch {$result.error=$_.Exception.Message}
finally {
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $result|ConvertTo-Json -Depth 12|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
}
if($result.status -cne 'passed'){throw $result.error}
Write-Output ('Passed: '+$result.checks.Count+' responsive checks')
