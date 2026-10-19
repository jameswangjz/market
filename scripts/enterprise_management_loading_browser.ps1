param([Parameter(Mandatory=$true)][string]$TokenFile,
      [string]$BaseUrl='http://192.168.10.10:30080',
      [Parameter(Mandatory=$true)][string]$OutputDir)
$ErrorActionPreference='Stop'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$profile=Join-Path $env:TEMP ('market-enterprise-loading-'+[Guid]::NewGuid().ToString('N'))
$result=@{status='failed';checks=@();scope='installed platform administrator users page; GET/HEAD only'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $u=[Uri]$url
        return $method -cin @('GET','HEAD') -and -not $postData -and
            $u.GetLeftPart([UriPartial]::Authority) -ceq $BaseUrl -and -not $u.UserInfo
    } catch {return $false}
}
function Check([string]$expression,[string]$description) {
    if(-not (Js $expression)){throw $description}
    $result.checks+=,$description
}
try {
    foreach($file in @(@{path='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{path='development_steps_browser.ps1';names=@('Read-PrivateToken','Connect-ReadOnlyChrome')})) {
        $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $file.path),[ref]$null,[ref]$null)
        foreach($name in $file.names){$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false); . ([ScriptBlock]::Create($fn.Extent.Text))}
    }
    Connect-ReadOnlyChrome
    # Record request paths/status only; never retain authorization or response bodies.
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=@'
window.__identityRequests=[];
const originalOpen=XMLHttpRequest.prototype.open;
const originalSend=XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open=function(method,url,...args){this.__identityPath=new URL(url,location.href).pathname;return originalOpen.call(this,method,url,...args)};
XMLHttpRequest.prototype.send=function(...args){const row={path:this.__identityPath,status:null};window.__identityRequests.push(row);this.addEventListener('loadend',()=>{row.status=this.status});return originalSend.apply(this,args)};
'@}|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console"}|Out-Null
    WaitJs 'document.readyState==="complete"' 20
    $secret=ConvertTo-Json (Read-PrivateToken $TokenFile) -Compress
    Js "localStorage.setItem('market_token',$secret);true"|Out-Null
    $secret=$null
    foreach($pass in @('initial','reload')) {
        if($pass -ceq 'initial'){Cdp 'Page.navigate' @{url="$BaseUrl/console?view=users"}|Out-Null}
        else {Cdp 'Page.reload' @{ignoreCache=$true}|Out-Null}
        WaitJs 'window.__identityRequests?.some(r=>r.path==="/api/auth/me"&&r.status===200)' 30
        WaitJs 'window.__identityRequests?.some(r=>r.path==="/api/users"&&r.status===200)' 30
        WaitJs 'window.__identityRequests?.some(r=>r.path==="/api/admin/enterprises"&&r.status===200)' 30
        Check '!window.__identityRequests.some(r=>["/api/enterprise/members","/api/enterprise/departments","/api/enterprise/invitations"].includes(r.path))' ($pass+': no current-enterprise management requests')
        Check '!window.__identityRequests.some(r=>r.status===403)' ($pass+': no XHR403 responses')
        Check 'document.body.innerText.includes("\u5e73\u53f0\u6ce8\u518c\u7528\u6237")' ($pass+': users tab rendered')
    }
    if($script:blocked -or $script:nativeDialogs){throw 'Unexpected mutation or native dialog'}
    $result.status='passed'
} catch {$result.error=$_.Exception.Message}
finally {
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $result|ConvertTo-Json -Depth 8|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
}
if($result.status -cne 'passed'){throw $result.error}
Write-Output ('Passed: '+$result.checks.Count+' enterprise loading browser checks')
