param(
  [string]$BaseUrl='http://127.0.0.1:5187',
  [Parameter(Mandatory=$true)][string]$OutputDir
)
$ErrorActionPreference='Stop'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-fe006-fixture-'+[Guid]::NewGuid().ToString('N'))
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$result=@{status='failed';scope='real Chrome, Vite-compiled actual FE006 components, dependency-injected in-memory responses; NOT backend end-to-end';runs=@();screenshots=@();stage='startup'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
  try { $uri=[Uri]$url; return $method -cin @('GET','HEAD') -and -not $postData -and
    $uri.GetLeftPart([UriPartial]::Authority) -ceq $BaseUrl -and -not $uri.UserInfo -and $uri.AbsolutePath -cnotmatch '^/api(?:/|$)' } catch { return $false }
}
function Save-FixtureShot([string]$filename) {
  $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
  [IO.File]::WriteAllBytes((Join-Path $OutputDir $filename),[Convert]::FromBase64String($shot.data))
  $result.screenshots+=@{file=$filename;sha256=(Get-FileHash (Join-Path $OutputDir $filename) -Algorithm SHA256).Hash.ToLower()}
}
foreach($spec in @(@{file='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{file='development_steps_browser.ps1';names=@('Connect-ReadOnlyChrome')})) {
  $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $spec.file),[ref]$null,[ref]$null)
  foreach($name in $spec.names) {
    $node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true)
    if(-not $node){throw 'Required CDP helper missing'}
    . ([ScriptBlock]::Create($node.Extent.Text))
  }
}
try {
  if($BaseUrl -cne 'http://127.0.0.1:5187'){throw 'Loopback fixture origin required'}
  if(Test-Path $OutputDir){throw 'Fresh artifact directory required'}
  New-Item -ItemType Directory -Path $OutputDir|Out-Null
  Connect-ReadOnlyChrome
  Cdp 'Runtime.enable' @{}|Out-Null
  foreach($viewport in @(@{name='desktop';width=1440;height=1000},@{name='mobile';width=390;height=844})) {
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=$viewport.width;height=$viewport.height;deviceScaleFactor=1;mobile=($viewport.name -eq 'mobile')}|Out-Null
    Cdp 'Page.navigate' @{url=($BaseUrl+'/tests/fixtures/after-sales-browser.html')}|Out-Null
    WaitJs '!!window.__fe006' 30
    foreach($scenario in @('create','provider','buyer','roles','collaboration','dirty','race','terminal')) {
      $result.stage=$viewport.name+' '+$scenario
      Write-Output ('running: '+$result.stage)
      $value=Js ('window.__fe006.run('+($scenario|ConvertTo-Json -Compress)+').catch(error=>({fixture_failure:error.message}))')
      if($value.fixture_failure){throw ('Isolated fixture assertion: '+$value.fixture_failure)}
      if(-not $value -or -not $value.checks){throw 'Fixture scenario did not return checks'}
      $result.runs+=@{viewport=$viewport.name;scenario=$scenario;status='passed';checks=$value.checks;requests=$value.requests}
      $filename=$viewport.name+'-'+$scenario+'.png'
      Save-FixtureShot $filename
      if($scenario -ceq 'collaboration') {
        Js 'document.querySelector(".after-sales-modal").scrollTop=document.querySelector(".after-sales-modal").scrollHeight;true'|Out-Null
        Save-FixtureShot ($viewport.name+'-authors-timeline.png')
      }
      $result|ConvertTo-Json -Depth 12|Set-Content -LiteralPath (Join-Path $OutputDir 'result.json') -Encoding UTF8
      Write-Output ('passed: '+$result.stage)
    }
    foreach($role in @('admin','monitor')) {
      Js ('window.__fe006.snapshot('+($role|ConvertTo-Json -Compress)+')')|Out-Null
      Save-FixtureShot ($viewport.name+'-'+$role+'-actions.png')
    }
  }
  if($script:blocked -or $script:nativeDialogs){throw 'Network or native dialog guard triggered'}
  $result.status='passed'
} catch {
  $result.failure=$_.Exception.Message
  try {$result.browser_state=Js 'JSON.stringify({path:location.pathname,text:document.body.innerText.slice(-1500),adapterErrors:window.__fe006?.adapterErrors,requests:window.__fe006?.calls.map(x=>({method:x.method,path:x.path}))})'}catch{}
  Write-Output ('failed stage: '+$result.stage)
} finally {
  $result.blocked_network_requests=$script:blocked; $result.native_dialogs=$script:nativeDialogs
  $result.finished_at=[DateTime]::UtcNow.ToString('o')
  if($script:socket){$script:socket.Dispose()}
  if($script:chrome -and -not $script:chrome.HasExited){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
  if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
  if(Test-Path $OutputDir){$result|ConvertTo-Json -Depth 12|Set-Content -LiteralPath (Join-Path $OutputDir 'result.json') -Encoding UTF8}
}
if($result.status -cne 'passed'){throw 'FE006 isolated browser acceptance failed; see result.json'}
Write-Output 'FE006 isolated desktop/mobile browser scenarios passed; no backend end-to-end claim'
