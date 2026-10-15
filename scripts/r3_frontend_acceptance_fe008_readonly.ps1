param(
  [string]$BaseUrl='http://192.168.10.10:30080',
  [Parameter(Mandatory=$true)][string]$OutputDir,
  [Parameter(Mandatory=$true)][ValidatePattern('^index-[A-Za-z0-9_-]+\.js$')][string]$ExpectedAsset,
  [Parameter(Mandatory=$true)][string]$DeploymentTag,
  [string]$TokenFile='',
  [ValidateSet('Admin','Buyer')][string]$Actor='Admin',
  [string]$EnterpriseId='',
  [ValidatePattern('^(ORD-[A-Za-z0-9_-]+)?$')][string]$OrderNo='',
  [switch]$RunAfterRelease,
  [switch]$SelfTest
)
$ErrorActionPreference='Stop'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-fe008-readonly-'+[Guid]::NewGuid().ToString('N'))
$script:socket=$null;$script:chrome=$null;$script:commandId=0
$script:blocked=0;$script:nativeDialogs=0;$script:publicAuth=0
$script:networkStatus=@();$script:consoleTypes=@();$script:deniedRequests=@();$token=$null
$result=@{status='pending';scope='deployed desktop refund read-only UI/API; no request, quote, assessment, review or execution';deployment=$DeploymentTag;expected_asset=$ExpectedAsset;checks=@();gaps=@();findings=@();screenshots=@();stage='startup'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
  try {
    $uri=[Uri]$url
    $allowed=$method -cin @('GET','HEAD') -and -not $postData -and
      $uri.GetLeftPart([UriPartial]::Authority) -ceq $BaseUrl -and -not $uri.UserInfo -and
      $uri.AbsolutePath -cnotmatch '(?i)(credential|secret|download|/oauth/|/oidc/|/evaluate(?:/|$))' -and
      $uri.AbsolutePath -cnotmatch '^/api/after-sales/[^/]+/sla/?$'
    if(-not $allowed){$script:deniedRequests+=@{method=$method;path=$uri.AbsolutePath}}
    return $allowed
  }catch{return $false}
}
if($SelfTest) {
  foreach($bad in @(@('POST',"$BaseUrl/api/orders/a/refund-quote",'{}'),@('POST',"$BaseUrl/api/refunds/a/manual-assessments",'{}'),@('POST',"$BaseUrl/api/orders/a/refund-requests",'{}'),@('POST',"$BaseUrl/api/refunds/a/review",'{}'),@('GET',"$BaseUrl/api/after-sales/a/sla",''),@('GET',"$BaseUrl/api/files/a/download",''),@('GET','https://example.invalid/api/refunds',''))) {
    if(Allowed-Request $bad[0] $bad[1] $bad[2]){throw 'Read-only guard accepted forbidden request'}
  }
  foreach($path in @('/console?view=delivery','/api/refunds?page=1&page_size=20','/api/refunds/a','/api/orders/a')) {
    if(-not (Allowed-Request GET ($BaseUrl+$path) '')){throw 'Read-only guard rejected permitted GET'}
  }
  Write-Output 'Passed offline FE008 guards; no browser or network';return
}
if(-not $RunAfterRelease){throw 'Explicit RunAfterRelease switch required; wait for rollout confirmation'}
foreach($spec in @(@{file='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{file='development_steps_browser.ps1';names=@('Connect-ReadOnlyChrome','Read-PrivateToken')})) {
  $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $spec.file),[ref]$null,[ref]$null)
  foreach($name in $spec.names) {
    $node=$ast.Find({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true)
    if(-not $node){throw 'Missing existing Chrome helper'}
    $definition=$node.Extent.Text
    if($name -ceq 'Cdp') {
      $marker="} elseif(`$answer.method -eq 'Page.javascriptDialogOpening') {"
      $observer=@'
} elseif($answer.method -eq 'Network.responseReceived') {
    $script:networkStatus+=@{path=([Uri]$answer.params.response.url).AbsolutePath;status=[int]$answer.params.response.status}
} elseif($answer.method -eq 'Runtime.consoleAPICalled') {
    $script:consoleTypes+=[string]$answer.params.type
} elseif($answer.method -eq 'Runtime.exceptionThrown') {
    $script:consoleTypes+='exceptionThrown'
} elseif($answer.method -eq 'Page.javascriptDialogOpening') {
'@
      if(-not $definition.Contains($marker)){throw 'Observation hook missing'}
      $definition=$definition.Replace($marker,$observer)
    }
    . ([ScriptBlock]::Create($definition))
  }
}
function Check([string]$expression,[string]$label) {
  if(-not (Js $expression)){throw $label}
  $result.checks+=,$label;Write-Output ('passed: '+$label)
}
function Navigate([string]$path) {
  Cdp 'Page.navigate' @{url=($BaseUrl+$path)}|Out-Null
  WaitJs '!!document.querySelector(".sidebar") && !document.querySelector(".loading-line")' 25
  $required=if($path -like '*view=orders*'){'/api/orders'}else{'/api/refunds'}
  $requiredJson=ConvertTo-Json -InputObject $required -Compress
  WaitJs "performance.getEntriesByType('resource').some(r=>new URL(r.name).pathname===$requiredJson&&r.responseStatus===200)&&!document.querySelector('.loading-line')" 25
  $actual=Js '(()=>{const script=document.querySelector("script[type=module][src]");return script?new URL(script.src).pathname:""})()'
  if($actual -cne ('/assets/'+$ExpectedAsset)){throw 'Entry asset differs from expected release'}
  $result.actual_asset=$actual
}
function Shot([string]$name,[string]$selector) {
  Check '!document.querySelector("input[type=password],.api-credential-reveal") && !/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|mk_[A-Za-z0-9_-]{24,}/.test(document.body.innerText)' 'screenshot privacy gate'
  $quoted=ConvertTo-Json -InputObject $selector -Compress
  $clip=Js "(()=>{const n=document.querySelector($quoted);n.scrollIntoView({block:'center'});const r=n.getBoundingClientRect(),x=Math.max(0,r.left),y=Math.max(0,r.top);return{x:x+scrollX,y:y+scrollY,width:Math.min(innerWidth,r.right)-x,height:Math.min(innerHeight,r.bottom)-y,scale:1}})()"
  $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false;clip=$clip}
  [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
  $result.screenshots+=@{file=$name;sha256=(Get-FileHash (Join-Path $OutputDir $name) -Algorithm SHA256).Hash.ToLower()}
}
try {
  if($BaseUrl -cne ([Uri]$BaseUrl).GetLeftPart([UriPartial]::Authority)){throw 'Origin required'}
  if(Test-Path $OutputDir){throw 'Fresh output directory required'}
  New-Item -ItemType Directory -Path $OutputDir|Out-Null
  $token=Read-PrivateToken $TokenFile
  Connect-ReadOnlyChrome
  Cdp 'Runtime.enable' @{}|Out-Null
  $origin=ConvertTo-Json -InputObject $BaseUrl -Compress;$quoted=ConvertTo-Json -InputObject $token -Compress
  Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source="if(location.origin===$origin){localStorage.setItem('market_token',$quoted)}"}|Out-Null
  Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
  $result.actor=$Actor
  $scope=if($EnterpriseId){'&enterprise_id='+[Uri]::EscapeDataString($EnterpriseId)}else{''}
  $result.stage='refund list';Write-Output 'running: deployed FE008 refund list'
  Navigate ('/console?view=delivery'+$scope)
  WaitJs '!!document.querySelector(".refund-list") && !document.querySelector(".refund-list [role=status]")' 25
  Check '!document.querySelector(".refund-list [role=alert]")' 'actual refund list rendered without API error'
  Check 'performance.getEntriesByType("resource").some(r=>new URL(r.name).pathname==="/api/refunds"&&r.responseStatus===200)' 'actual GET refunds returned 200'
  Shot 'desktop-refund-list-readonly.png' '.refund-center'
  if($Actor -ceq 'Buyer') {
    if(-not $OrderNo -or -not $EnterpriseId){throw 'Buyer requires explicit OrderNo and EnterpriseId; no random order selection'}
    $result.stage='buyer order drawer';Write-Output 'running: buyer order drawer'
    Navigate ('/console?view=orders'+$scope)
    $number=ConvertTo-Json -InputObject $OrderNo -Compress
    $rowExpression="(()=>{const rows=[...document.querySelectorAll('tbody tr')];return rows.find(r=>r.querySelector('.order-no')?.textContent.trim()===$number)})()"
    if(-not (Js ("!!"+$rowExpression))) {
      $result.gaps+='Explicit buyer order is not in current visible list; drawer not verified'
    } else {
      Js ("(()=>{const row="+$rowExpression+";const b=row.querySelector('button[title=查看订单]');if(!b)throw Error('Order detail button missing');b.click();return true})()")|Out-Null
      WaitJs '!!document.querySelector(".order-drawer .refund-list") && !document.querySelector(".order-drawer .refund-list [role=status]")' 25
      Check '!document.querySelector(".order-drawer .refund-list [role=alert]")' 'buyer drawer refund list loaded'
      Check '(()=>{const p=document.querySelector(".order-drawer .refund-center");return [...p.querySelectorAll("button")].some(b=>b.textContent.trim()==="申请退款"&&!b.disabled)})()' 'buyer administrator has ordinary refund request entry'
      Js '(()=>{const p=document.querySelector(".order-drawer .refund-center");[...p.querySelectorAll("button")].find(b=>b.textContent.trim()==="申请退款").click();return true})()'|Out-Null
      WaitJs '!!document.querySelector(".order-drawer .refund-request-form")' 10
      Check '(()=>{const p=document.querySelector(".order-drawer .refund-request-form");return p.querySelector("button[type=submit]").disabled&&[...p.querySelectorAll("input,textarea")].every(n=>!n.value)&&p.innerText.includes("unknown")})()' 'untouched buyer request form is empty, unknown and submit disabled'
      Shot 'desktop-buyer-refund-entry-readonly.png' '.order-drawer .refund-request-form'
    }
  } else {
    Check '!document.querySelector(".refund-request-form")' 'admin list does not create buyer refund application'
  }
  $result.status=if($result.findings.Count -or $script:blocked -or $script:nativeDialogs){'failed'}elseif($result.gaps.Count){'partial'}else{'passed'}
}catch{$result.status='failed';$result.failure=$_.Exception.Message}
finally {
  $result.network_status=$script:networkStatus;$result.console_types=$script:consoleTypes
  $result.denied_requests=$script:deniedRequests
  $result.blocked_network_requests=$script:blocked;$result.native_dialogs=$script:nativeDialogs
  $result.finished_at=[DateTime]::UtcNow.ToString('o')
  if($script:socket){$script:socket.Dispose()}
  if($script:chrome -and -not $script:chrome.HasExited){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
  if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
  $token=$null;$quoted=$null
  if(Test-Path $OutputDir){$result|ConvertTo-Json -Depth 10|Set-Content -LiteralPath (Join-Path $OutputDir 'result.json') -Encoding UTF8}
}
if($result.status -ceq 'failed'){throw 'Deployed FE008 read-only smoke failed; see per-section result.json'}
Write-Output ('Deployed FE008 scoped status: '+$result.status)
