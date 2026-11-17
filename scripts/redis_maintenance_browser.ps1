param(
    [Parameter(Mandatory=$true)][string]$TokenFile,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [string]$BaseUrl='http://192.168.10.10:30080'
)
$ErrorActionPreference='Stop'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-maintenance-'+[Guid]::NewGuid().ToString('N'))
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$result=@{status='failed';checks=@();scope='inspect task and confirmation cancel only; no seal, resume, gap or restart'}
foreach($source in @(@{file='development_steps_browser.ps1';names=@('Read-PrivateToken','Connect-ReadOnlyChrome')},@{file='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')})) {
    $tree=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $source.file),[ref]$null,[ref]$null)
    foreach($name in $source.names) {
        $node=$tree.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true)
        if(-not $node){throw 'Required CDP helper missing'}
        . ([ScriptBlock]::Create($node.Extent.Text))
    }
}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $uri=[Uri]$url
        if($uri.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $uri.UserInfo){return $false}
        if($method -cin @('GET','HEAD')) {
            return $uri.AbsolutePath -cnotmatch '(?i)(credential|download|/oauth/|/oidc/)'
        }
        if($method -ceq 'POST' -and $uri.AbsolutePath -ceq '/api/admin/maintenance/redis/tasks') {
            $body=$postData|ConvertFrom-Json
            return $body.action -ceq 'inspect' -and -not $body.accept_gap
        }
    } catch {}
    return $false
}
function Shot([string]$name) {
    if(-not (Js '(()=>!/eyJ[A-Za-z0-9_-]+.[A-Za-z0-9_-]+.[A-Za-z0-9_-]+/.test(document.body.innerText))()')){throw 'Screenshot token guard failed'}
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
}
try {
    New-Item -ItemType Directory -Path $OutputDir -Force|Out-Null
    $token=Read-PrivateToken $TokenFile
    Connect-ReadOnlyChrome
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    $encoded=$token|ConvertTo-Json -Compress
    $origin=$BaseUrl|ConvertTo-Json -Compress
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source="if(location.origin===$origin){localStorage.setItem('market_token',$encoded)}"}|Out-Null
    Cdp 'Page.navigate' @{url=($BaseUrl+'/console?view=settings')}|Out-Null
    WaitJs '!![...document.querySelectorAll(".sidebar-bottom button")].find(b=>b.textContent.trim()==="\u7cfb\u7edf\u8bbe\u7f6e")' 60
    Js '[...document.querySelectorAll(".sidebar-bottom button")].find(b=>b.textContent.trim()==="\u7cfb\u7edf\u8bbe\u7f6e").click();true'|Out-Null
    WaitJs '!![...document.querySelectorAll(".settings-tabs button")].find(b=>b.textContent.trim()==="\u7cfb\u7edf\u7ef4\u62a4")' 60
    Js '[...document.querySelectorAll(".settings-tabs button")].find(b=>b.textContent.trim()==="\u7cfb\u7edf\u7ef4\u62a4").click();true'|Out-Null
    WaitJs 'document.querySelectorAll(".redis-actions button").length===4'
    WaitJs 'document.querySelector(".redis-maintenance").innerText.includes("\u5f53\u524d\u6765\u6e90\u5065\u5eb7")'
    $result.checks+='system maintenance tab and four actions loaded'
    Js 'document.querySelector(".redis-actions button").click();true'|Out-Null
    WaitJs '(()=>{const row=document.querySelector(".redis-task-table tbody tr");return !!row&&row.innerText.includes("\u6210\u529f")&&!document.querySelector(".redis-actions button").disabled})()' 90
    $result.task_no=Js 'document.querySelector(".redis-task-table tbody tr td").innerText.split("\n")[0]'
    $result.checks+='browser inspect task completed through PostgreSQL executor'
    Shot 'maintenance-desktop.png'
    Js '(()=>{const t=document.querySelector(".redis-maintenance textarea");t.value="Browser confirmation cancellation test";t.dispatchEvent(new Event("input",{bubbles:true}));return true})()'|Out-Null
    Js 'document.querySelectorAll(".redis-actions button")[1].click();true'|Out-Null
    WaitJs '!!document.querySelector(".action-dialog")'
    Js 'document.querySelector(".action-dialog .secondary-btn").click();true'|Out-Null
    WaitJs '!document.querySelector(".action-dialog")'
    $result.checks+='prepare confirmation opened then cancelled without submitting mutation'
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=1;mobile=$true}|Out-Null
    WaitJs 'window.innerWidth===390'
    if(-not (Js 'document.documentElement.scrollWidth<=window.innerWidth+2')){throw 'Mobile page overflow'}
    Shot 'maintenance-mobile.png'
    $result.checks+='mobile viewport has no page-level horizontal overflow'
    if($script:blocked -or $script:nativeDialogs){throw 'Forbidden request or native dialog observed'}
    $result.status='passed'
} catch {
    $result.failure='Bounded browser check failed; private exception suppressed'
    try { $result.safe_state=Js 'JSON.stringify({path:location.pathname,panel:!!document.querySelector(".redis-maintenance"),buttons:document.querySelectorAll(".redis-actions button").length,login:!!document.querySelector("input[type=password]")})' } catch {}
} finally {
    if($script:socket){try{Js 'localStorage.removeItem("market_token");true'|Out-Null}catch{}; try{Send-Cdp 'Browser.close' @{}|Out-Null}catch{}; $script:socket.Dispose()}
    if($script:chrome -and -not $script:chrome.HasExited){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    $token=$null; $encoded=$null
    $result.blocked=$script:blocked; $result.native_dialogs=$script:nativeDialogs
    $result|ConvertTo-Json -Depth 8|Set-Content -LiteralPath (Join-Path $OutputDir 'result.json') -Encoding UTF8
}
if($result.status -cne 'passed'){throw 'Maintenance browser check failed; inspect sanitized result.json'}
Write-Output 'PASS: bounded maintenance browser verification; no Redis mutation submitted'
