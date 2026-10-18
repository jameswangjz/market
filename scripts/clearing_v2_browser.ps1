param([Parameter(Mandatory=$true)][string]$TokenDirectory,
      [string]$BaseUrl='http://127.0.0.1:8126',
      [string]$OutputDir="$env:TEMP\market-clearing-v2",
      [switch]$ReadOnly)
$ErrorActionPreference='Stop'
if(-not $ReadOnly -and $BaseUrl -cne 'http://127.0.0.1:8126'){throw 'Synthetic writable fixture only'}
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-clearing-'+[Guid]::NewGuid().ToString('N'))
$result=@{status='failed';checks=@();screenshots=@();scope='isolated disposable SQLite fixture; synthetic funds only'}
if($ReadOnly){$result.scope='installed clearing UI; GET/HEAD only; no business writes'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $u=[Uri]$url
        if($u.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $u.UserInfo){return $false}
        return ($method -cin @('GET','HEAD') -and -not $postData) -or
            (-not $ReadOnly -and $method -ceq 'POST' -and $u.AbsolutePath -cmatch '^/api/settlement-finance/transactions(?:/[a-f0-9]+/confirm)?$')
    } catch {return $false}
}
function Check([string]$expression,[string]$description) {
    if(-not (Js $expression)){throw $description}
    $result.checks+=,$description
}
function Tab([int]$index) {
    Js "document.querySelectorAll('.settlement-finance > .console-tabs')[0].querySelectorAll('[role=tab]')[$index].click();true"|Out-Null
    WaitJs 'document.querySelector(".settlement-finance")?.getAttribute("aria-busy")==="false"' 20
    Check '!document.querySelector(".settlement-finance [role=alert]")' ('Workspace tab '+$index+' has no API contract error')
}
function Enter([string]$actor) {
    $value=Read-PrivateToken ($TokenDirectory+'/'+$actor+'.token')
    $secret=ConvertTo-Json $value -Compress
    $origin=ConvertTo-Json $BaseUrl -Compress
    Js "localStorage.setItem('market_token',$secret);true"|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=settlements"}|Out-Null
    WaitJs '!!document.querySelector(".settlement-tabs")' 30
    Js '[...document.querySelectorAll(".settlement-tabs button")].find(b=>b.textContent.includes("\u62a5\u8868")).click();true'|Out-Null
    WaitJs 'document.querySelector(".settlement-finance")?.getAttribute("aria-busy")==="false"' 30
}
try {
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    foreach($file in @(@{path='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{path='development_steps_browser.ps1';names=@('Read-PrivateToken','Connect-ReadOnlyChrome')})) {
        $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $file.path),[ref]$null,[ref]$null)
        foreach($name in $file.names){$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false); . ([ScriptBlock]::Create($fn.Extent.Text))}
    }
    Connect-ReadOnlyChrome
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    # Navigate to the allowed origin before placing a fixture-only credential.
    Cdp 'Page.navigate' @{url="$BaseUrl/console"}|Out-Null
    WaitJs 'document.readyState==="complete"' 20
    Enter 'platform'
    Check 'document.querySelectorAll(".settlement-finance > .console-tabs")[0].querySelectorAll("[role=tab]").length===4' 'Four standard workspace tabs render'
    if(-not $ReadOnly){Check 'document.querySelector(".settlement-finance").textContent.includes("150.00") && document.querySelector(".settlement-finance").textContent.includes("140.00")' 'Report displays income150 and provider cost-plus-profit140'}
    foreach($i in @(1,2,3,0)){Tab $i}
    if(-not $ReadOnly){
    Tab 1
    Js '[...document.querySelectorAll(".settlement-finance tbody tr")].find(r=>r.textContent.includes("100.00")).querySelector("button.secondary-btn").click();true'|Out-Null
    WaitJs '!!document.querySelector(".finance-modal")' 10
    WaitJs 'document.querySelectorAll(".finance-modal input[type=checkbox]").length===2 && !document.querySelector(".finance-modal input[type=checkbox]").disabled' 20
    Js 'document.querySelectorAll(".finance-modal input[type=checkbox]").forEach(c=>{if(!c.checked)c.click()});true'|Out-Null
    Check 'document.querySelector(".finance-modal .payment-total").textContent.includes("140.00")' 'Cost100 and profit40 combine into a140 payment with separate allocations'
    Check 'document.querySelector(".finance-modal").contains(document.activeElement)' 'Payment draft dialog receives keyboard focus'
    Js '(()=>{const m=document.querySelector(".finance-modal");const r=m.querySelector("input[maxlength]");r.value="SYNTHETIC-BROWSER-RECEIPT";r.dispatchEvent(new Event("input",{bubbles:true}));const n=m.querySelector("textarea");n.value="Synthetic browser payment evidence";n.dispatchEvent(new Event("input",{bubbles:true}));m.querySelector("form").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));return true})()'|Out-Null
    WaitJs '!document.querySelector(".finance-modal") && document.querySelector(".settlement-finance")?.getAttribute("aria-busy")==="false"' 20
    Tab 2
    Check 'document.querySelector(".settlement-finance tbody").textContent.includes("SYNTHETIC-BROWSER-RECEIPT")' 'Browser creates a real draft through the fixture API'
    Check '!document.querySelector(".settlement-finance tbody button.secondary-btn")' 'Creator cannot review own payment voucher'
    Enter 'ops'
    Tab 2
    Js 'document.querySelector(".settlement-finance tbody button.secondary-btn").click();true'|Out-Null
    WaitJs '!!document.querySelector(".finance-modal")' 10
    Js '(()=>{const m=document.querySelector(".finance-modal");const n=m.querySelector("textarea");n.value="Independent synthetic review";n.dispatchEvent(new Event("input",{bubbles:true}));m.querySelector("form").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));return true})()'|Out-Null
    WaitJs '!document.querySelector(".finance-modal") && document.querySelector(".settlement-finance")?.getAttribute("aria-busy")==="false"' 20
    Check 'document.querySelector(".settlement-finance tbody").textContent.includes("\u5df2\u786e\u8ba4")' 'Different operator confirms voucher through browser'
    Tab 1
    Check '[...document.querySelectorAll(".settlement-finance tbody tr")].some(r=>r.textContent.includes("100.00")&&r.textContent.includes("0.00"))' 'Confirmed voucher updates cost balance'
    }
    foreach($width in @(1440,390)) {
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
        Check '(()=>{const r=document.querySelector(".settlement-finance").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1})()' ('Workspace fits viewport '+$width)
        $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
        $name='clearing-'+$width+'.png'
        [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
        $result.screenshots+=,$name
    }
    if($script:blocked -or $script:nativeDialogs){throw 'Unexpected request or native dialog'}
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
Write-Output ('Passed: '+$result.checks.Count+' clearing browser checks')
