param(
    [Parameter(Mandatory=$true)][string]$TokenFile,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [string]$BaseUrl='http://192.168.10.10:30080',
    [string]$ExpectedAsset='index-BS5NQMfJ.js'
)
$ErrorActionPreference='Stop'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-as-readonly-'+[Guid]::NewGuid().ToString('N'))
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$result=@{status='failed';scope='Desktop deployed detail GET only; no actions, downloads or SLA materialization';checks=@();screenshots=@()}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $u=[Uri]$url
        if($method -cnotin @('GET','HEAD') -or $postData -or $u.UserInfo -or $u.Fragment -or $u.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl){return $false}
        return $u.AbsolutePath -cin @('/console','/favicon.ico','/api/auth/me','/api/settings/application-address-policy',
            '/api/storefront/buyer-enterprises','/api/notifications/summary','/api/notifications/stream',
            '/api/notifications/sender-context','/api/notifications/config','/api/delivery-tasks','/api/after-sales') -or
            $u.AbsolutePath -cmatch '^/assets/[A-Za-z0-9_./-]+$' -or
            $u.AbsolutePath -cmatch '^/api/after-sales/[A-Za-z0-9_-]{1,36}(?:/(?:timeline|comments|attachments))?$'
    } catch {return $false}
}
foreach($item in @(@('development_steps_browser.ps1',@('Connect-ReadOnlyChrome','Read-PrivateToken')),
                   @('password_first_purchase_browser_cdp.ps1',@('Send-Cdp','Cdp','Js','WaitJs')))) {
    $tree=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $item[0]),[ref]$null,[ref]$null)
    foreach($name in $item[1]) {
        $node=$tree.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true)
        if(-not $node){throw 'Existing CDP helper missing'}
        . ([ScriptBlock]::Create($node.Extent.Text))
    }
}
function Check([string]$expression,[string]$label) {
    if(-not (Js $expression)){throw 'Read-only browser assertion failed'}
    $result.checks+=,$label
}
function Shot([string]$name) {
    Check '!document.querySelector("input[type=password]")&&!/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|mk_[A-Za-z0-9_-]{20,}|PRIVATE KEY/.test(document.body.innerText)' 'Screenshot secret guard'
    $s=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($s.data))
    $result.screenshots+=,$name
}
try {
    if(Test-Path $OutputDir){throw 'Fresh artifact directory required'}
    New-Item -ItemType Directory -Path $OutputDir|Out-Null
    $token=Read-PrivateToken $TokenFile
    Connect-ReadOnlyChrome
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    $quoted=ConvertTo-Json -InputObject $token -Compress
    $origin=ConvertTo-Json -InputObject $BaseUrl -Compress
    $observer=@'
const original=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(method,url,...args){const path=new URL(url,location.href).pathname;
if(method==='GET'&&/^\/api\/after-sales\/[A-Za-z0-9_-]{1,36}$/.test(path))this.addEventListener('load',()=>{try{
const d=JSON.parse(this.responseText);window.__asReadProof={status:this.status,order_no:d.order_no,allowed:d.allowed_actions||[]};}catch{}});
return original.call(this,method,url,...args);};
'@
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source="if(location.origin===$origin){localStorage.setItem('market_token',$quoted);$observer}"}|Out-Null
    Cdp 'Page.navigate' @{url=($BaseUrl+'/console?view=delivery')}|Out-Null
    WaitJs '!!document.querySelector(".after-sales-item .task-code")&&!document.querySelector(".loading-line")' 25
    Check "[...document.querySelectorAll('script[type=module]')].some(n=>new URL(n.src).pathname==='/assets/$ExpectedAsset')" 'Installed entry asset matched'
    Js 'document.querySelector(".after-sales-item .task-code").click();true'|Out-Null
    WaitJs '!!document.querySelector(".after-sales-modal .after-sales-meta")&&!document.querySelector(".after-sales-modal [role=status]")' 25
    Check '!!window.__asReadProof&&window.__asReadProof.status===200&&/^ORD-/.test(window.__asReadProof.order_no)&&document.querySelector(".after-sales-meta").textContent.includes(window.__asReadProof.order_no)' 'Existing ticket shows ORD from actual detail response'
    Check '!document.querySelector(".after-sales-modal [role=alert]")' 'No detail load error'
    Check '["沟通记录","处理时间线"].every(t=>[...document.querySelectorAll(".after-sales-modal h3")].some(n=>n.textContent===t))&&document.querySelector(".after-sales-modal input[type=file]")!==null' 'Comment attachment and timeline sections present'
    $actions=@'
(()=>{const map={respond:'响应工单',request_info:'要求补充材料',supplement:'补充材料',submit_resolution:'提交解决方案',confirm:'确认解决',reject_resolution:'拒绝解决方案',withdraw:'撤回工单',request_intervention:'申请平台介入',respond_intervention:'介入响应',review_intervention:'完成平台审查',force_close:'强制关闭'};
const allowed=window.__asReadProof.allowed,buttons=[...document.querySelectorAll('.after-sales-modal button[aria-pressed]')];
return {allowed_count:allowed.length,rendered_count:buttons.length,labels_match:allowed.every(a=>buttons.some(b=>b.textContent.trim()===map[a])),actions:allowed};})()
'@
    $result.action_summary=Js $actions
    if(-not $result.action_summary.labels_match){throw 'Allowed action label mismatch'}
    Check 'document.querySelectorAll(".after-sales-modal button[aria-pressed]").length===window.__asReadProof.allowed.length' 'Rendered action count matches server allowed_actions; none clicked'
    # Hide business descriptions/authors without replacing actual ORD or controls.
    Js '(()=>{const p=document.querySelector(".after-sales-modal"),s=document.createElement("style");s.textContent="#app{visibility:hidden}.after-sales-record p,.after-sales-record small{visibility:hidden}";document.head.append(s);return true})()'|Out-Null
    Shot 'desktop-detail-top.png'
    Js 'document.querySelector(".after-sales-modal").scrollTop=document.querySelector(".after-sales-modal").scrollHeight;true'|Out-Null
    Shot 'desktop-detail-sections.png'
    if($script:blocked -or $script:nativeDialogs){throw 'Read-only guard observed unexpected request/dialog'}
    $result.status='passed'
} catch {
    $result.error='Bounded read-only browser check failed; private diagnostics suppressed'
} finally {
    $result.blocked_requests=$script:blocked;$result.native_dialogs=$script:nativeDialogs
    $result.captured_at=[DateTime]::UtcNow.ToString('o');$result.expected_asset=$ExpectedAsset
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome -and -not $script:chrome.HasExited){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    $token=$null;$quoted=$null
    if(Test-Path $OutputDir){$result|ConvertTo-Json -Depth 10|Set-Content -LiteralPath (Join-Path $OutputDir 'result.json') -Encoding UTF8}
}
if($result.status -cne 'passed'){throw 'Read-only detail failed; see sanitized result.json'}
Write-Output 'Passed: existing ticket detail desktop GET-only; no action or SLA endpoint used'
