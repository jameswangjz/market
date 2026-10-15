$ErrorActionPreference='Stop'
$errors=$null; $tokens=$null
$path=Join-Path $PSScriptRoot 'development_steps_browser.ps1'
$ast=[Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Development steps browser syntax invalid'}
foreach($name in @('Allowed-Request','Read-ExpectedSteps','Read-PrivateToken','Assert-NoMutations')) {
    $nodes=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false))
    if($nodes.Count -ne 1){throw 'Expected function missing'}
    . ([ScriptBlock]::Create($nodes[0].Extent.Text))
}
$BaseUrl='http://192.168.10.10:30080'
$script:cases=0
function Check([bool]$value){$script:cases++;if(-not $value){throw ('Guard assertion failed: '+$script:cases)}}
$planParameter=$ast.ParamBlock.Parameters|Where-Object {$_.Name.VariablePath.UserPath -ceq 'PlanFile'}
Check ($null -eq $planParameter.DefaultValue)
$planDefault=@($ast.EndBlock.Statements|Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and $_.Extent.Text.StartsWith('if(-not $PlanFile)')
})
Check ($planDefault.Count -eq 1)
$PlanFile=''
$defaultBlock=[ScriptBlock]::Create('param([string]$PSScriptRoot)' + "`n" + $planDefault[0].Extent.Text)
. $defaultBlock (Split-Path -Parent $path)
Check (Test-Path -LiteralPath $PlanFile -PathType Leaf)
Check (@(Read-ExpectedSteps $PlanFile).Count -eq 32)
$PlanFile='explicit-plan-path.json'
. $defaultBlock (Split-Path -Parent $path)
Check ($PlanFile -ceq 'explicit-plan-path.json')
foreach($method in @('GET','HEAD')) {
    foreach($endpoint in @('/console','/console?view=development','/assets/app.js',
        '/api/auth/me','/api/development/tasks','/api/notifications/stream')) {
        Check (Allowed-Request $method ($BaseUrl+$endpoint) '')
    }
}
foreach($method in @('POST','PATCH','PUT','DELETE','OPTIONS','get')) {
    foreach($endpoint in @('/api/development/tasks','/api/development/tasks/TRD-R3-BE-001','/api/auth/login')) {
        Check (-not (Allowed-Request $method ($BaseUrl+$endpoint) '{}'))
    }
}
foreach($url in @('https://example.invalid/console','http://user:pass@192.168.10.10:30080/console',
    "$BaseUrl/console?view=orders","$BaseUrl/console?view=development&token=private",
    "$BaseUrl/api/development/tasks?token=private","$BaseUrl/api/notifications/stream?token=private",
    "$BaseUrl/api/files/secret/download","$BaseUrl/api/orders","$BaseUrl/console#private",
    'ws://192.168.10.10:30080/console')) {
    Check (-not (Allowed-Request 'GET' $url ''))
}
Check (-not (Allowed-Request 'GET' "$BaseUrl/api/development/tasks" '{"state":"done"}'))
$expected=@(Read-ExpectedSteps (Join-Path $PSScriptRoot '../backend/app/development_steps.json'))
Check ($expected.Count -eq 32)
Check (($expected|ForEach-Object {$_.subtasks.Count}|Measure-Object -Sum).Sum -eq 124)
Check ($expected[0].subtasks[0].id -ceq ($expected[0].code+'-S01'))
$script:blocked=0; $script:nativeDialogs=0; Assert-NoMutations
foreach($pair in @(@(1,0),@(0,1))) {
    $script:blocked=$pair[0]; $script:nativeDialogs=$pair[1]; $rejected=$false
    try{Assert-NoMutations}catch{$rejected=$true}
    Check $rejected
}
$source=$ast.Extent.Text
Check ($source.Contains("'Fetch.enable'") -and $source.Contains("'Network.setBypassServiceWorker'"))
Check ($source.Contains("localStorage.setItem('market_token',"))
Check (-not $source.Contains('requestSubmit('))
Check ($source.Contains("'Send-Cdp','Cdp','Js','WaitJs'"))
Check (-not $source.Contains('Connect-OwnedChrome'))
Check ($source.Contains('private diagnostics suppressed'))
Write-Output ("Development steps syntax and guards passed: {0} assertions; no browser/network or private token reads" -f $script:cases)
