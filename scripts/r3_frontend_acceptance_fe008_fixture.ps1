param([Parameter(Mandatory=$true)][string]$OutputDir, [switch]$SourcesOnly, [switch]$LayoutOnly)
$ErrorActionPreference='Stop'
$BaseUrl='http://127.0.0.1:5189'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-fe008-fixture-'+[Guid]::NewGuid().ToString('N'))
$script:socket=$null;$script:chrome=$null;$script:commandId=0
$script:blocked=0;$script:nativeDialogs=0;$script:publicAuth=0
$result=@{status='failed';scope='real Chrome, actual Vite-compiled refund components, isolated memory responses; NOT deployed backend end-to-end';runs=@();screenshots=@();stage='startup'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
  try {$uri=[Uri]$url;return $method -cin @('GET','HEAD') -and -not $postData -and $uri.GetLeftPart([UriPartial]::Authority) -ceq $BaseUrl -and -not $uri.UserInfo -and $uri.AbsolutePath -cnotmatch '^/api(?:/|$)'}catch{return $false}
}
foreach($spec in @(@{file='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{file='development_steps_browser.ps1';names=@('Connect-ReadOnlyChrome')})) {
  $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $spec.file),[ref]$null,[ref]$null)
  foreach($name in $spec.names){$node=$ast.Find({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true);if(-not $node){throw 'Missing CDP helper'};. ([ScriptBlock]::Create($node.Extent.Text))}
}
try {
  if(Test-Path $OutputDir){throw 'Fresh output directory required'}
  New-Item -ItemType Directory -Path $OutputDir|Out-Null
  Connect-ReadOnlyChrome
  Cdp 'Runtime.enable' @{}|Out-Null
  foreach($viewport in @(@{name='desktop';width=1440;height=1000},@{name='mobile';width=390;height=844})) {
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=$viewport.width;height=$viewport.height;deviceScaleFactor=1;mobile=($viewport.name -ceq 'mobile')}|Out-Null
    Cdp 'Page.navigate' @{url=($BaseUrl+'/tests/fixtures/refund-browser.html')}|Out-Null
    WaitJs '!!window.__refund' 30
    $scenarios=@('request','review','conflict','roles','manual','quote')
    if($SourcesOnly){$scenarios=@('sources')}
    if($LayoutOnly){$scenarios=@('layout-request','layout-manual','layout-review')}
    foreach($scenario in $scenarios) {
      $result.stage=$viewport.name+' '+$scenario
      Write-Output ('running: '+$result.stage)
      $value=Js ('window.__refund.run('+($scenario|ConvertTo-Json -Compress)+').catch(error=>({fixture_failure:error.message}))')
      if($value.fixture_failure){throw ('Fixture assertion: '+$value.fixture_failure)}
      if(-not $value.checks){throw 'No completed checks'}
      $result.runs+=@{viewport=$viewport.name;scenario=$scenario;status='passed';checks=$value.checks;requests=$value.requests}
      $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
      $file=$viewport.name+'-'+$scenario+'.png'
      [IO.File]::WriteAllBytes((Join-Path $OutputDir $file),[Convert]::FromBase64String($shot.data))
      $result.screenshots+=@{file=$file;sha256=(Get-FileHash (Join-Path $OutputDir $file) -Algorithm SHA256).Hash.ToLower()}
      $result|ConvertTo-Json -Depth 12|Set-Content -LiteralPath (Join-Path $OutputDir 'result.json') -Encoding UTF8
      Write-Output ('passed: '+$result.stage)
    }
  }
  if($script:blocked -or $script:nativeDialogs){throw 'Network/native-dialog guard triggered'}
  $result.status='passed'
}catch{
  $result.failure=$_.Exception.Message
  try{$result.browser_state=Js 'JSON.stringify({path:location.pathname,text:document.body.innerText.slice(-2000)})'}catch{}
  Write-Output ('failed: '+$result.stage)
}finally{
  $result.blocked_network_requests=$script:blocked;$result.native_dialogs=$script:nativeDialogs
  $result.finished_at=[DateTime]::UtcNow.ToString('o')
  if($script:socket){$script:socket.Dispose()}
  if($script:chrome -and -not $script:chrome.HasExited){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
  if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
  if(Test-Path $OutputDir){$result|ConvertTo-Json -Depth 12|Set-Content -LiteralPath (Join-Path $OutputDir 'result.json') -Encoding UTF8}
}
if($result.status -cne 'passed'){throw 'FE008 scoped browser check failed; see result.json'}
Write-Output 'FE008 desktop/mobile isolated refund browser checks passed'
