param([string]$OutputDir=(Join-Path $PSScriptRoot '../docs/evidence/clearing-v2-funded-combinations-20261018223500/browser'))
$ErrorActionPreference='Stop'
$BaseUrl='http://127.0.0.1:8131'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-funded-evidence-'+[Guid]::NewGuid().ToString('N'))
$script:socket=$null;$script:chrome=$null;$script:commandId=0;$script:blocked=0;$script:nativeDialogs=0;$script:publicAuth=0
$result=@{status='failed';checks=@();screenshots=@();scope='Synthetic read-only DTO presentation mounted in actual Vue SFCs; no backend or DB; not a financial algorithm or installed-app test'}
function Allowed-Request([string]$method,[string]$url,[string]$postData){
    try{$u=[Uri]$url;return $u.GetLeftPart([UriPartial]::Authority) -ceq $BaseUrl -and -not $u.UserInfo -and $method -cin @('GET','HEAD') -and -not $postData -and -not $u.AbsolutePath.StartsWith('/api/')}catch{return $false}
}
function Check([string]$expression,[string]$description){if(-not (Js $expression)){throw $description};$result.checks+=,$description}
try{
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    foreach($file in @(@{path='password_first_purchase_browser_cdp.ps1';names=@('Send-Cdp','Cdp','Js','WaitJs')},@{path='development_steps_browser.ps1';names=@('Connect-ReadOnlyChrome')})){
        $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $file.path),[ref]$null,[ref]$null)
        foreach($name in $file.names){$fn=$ast.Find({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false);if(-not $fn){throw ('Missing CDP helper '+$name)};. ([ScriptBlock]::Create($fn.Extent.Text))}
    }
    Connect-ReadOnlyChrome
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})}|Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Cdp 'Page.navigate' @{url=($BaseUrl+'/tests/fixtures/funded-combinations.html')}|Out-Null
    WaitJs 'window.__fundedEvidenceReady===true && !!document.querySelector("#pending .report-evidence")' 30
    Check '(()=>{const r=document.querySelector("#pending [aria-label=\"\u73b0\u91d1\u9000\u6b3e\u4e0e\u8865\u5dee\u8fd4\u8fd8\u533a\u5206\"]");const pairs=[...r.querySelectorAll("dt")].map(d=>[d.textContent,d.nextElementSibling.textContent]);return pairs.some(([k,v])=>k.includes("\u4e70\u65b9")&&v==="30.00")&&pairs.some(([k,v])=>k.includes("\u8d22\u52a1\u6536\u5165")&&v==="-32.00")&&pairs.some(([k,v])=>k.includes("\u8fd4\u8fd8\u6743\u76ca")&&v==="2.00")})()' 'Buyer cash30, financial reversal-32 and funding return2 are distinct rendered fields'
    Check 'document.querySelector("#pending").textContent.includes("\u539f\u8865\u5dee\u4ed8\u6b3e\u65b9\u8fd4\u8fd8\u5e94\u4ed8") && document.querySelector("#pending").textContent.includes("\u5f85\u4ed8\u6b3e / \u62b5\u9500")' 'Funding return is a separate pending payable, not ordinary profit'
    Check 'document.querySelector("#unknown").textContent.includes("\u8fd4\u8fd8\u6743\u76ca\uff0c\u7ed3\u7b97\u8bc1\u636e\u672a\u63d0\u4f9b") && ![...document.querySelectorAll("#unknown dd")].some(d=>d.textContent==="\u5df2\u767b\u8bb0\u4ed8\u6b3e")' 'Unknown frozen rights do not claim paid status'
    Check 'document.querySelector("#paid").textContent.includes("\u5df2\u767b\u8bb0\u4ed8\u6b3e") && document.querySelector("#offset").textContent.includes("\u5df2\u62b5\u9500")' 'Actual DTO payment and offset balances have distinct states'
    Check '["#pending","#paid","#offset"].every(s=>[...document.querySelectorAll(s+" dt")].some(d=>d.textContent==="\u94f6\u884c\u5230\u8d26\u9a8c\u8bc1"&&d.nextElementSibling.textContent==="\u5426"))' 'Manual and offset presentation explicitly retain bank verification false'
    Check '(()=>{const tables=[...document.querySelectorAll("#pending table")];const p=tables.find(t=>t.textContent.includes("\u5b9e\u9645\u6536\u76ca\u4e3b\u4f53"));const retained=[...p.querySelectorAll("tbody tr")].find(t=>t.textContent.includes("\u4fdd\u7559\u6210\u672c\u63d0\u4f9b\u65b9"));const loss=[...p.querySelectorAll("tbody tr")].find(t=>t.textContent.includes("\u6210\u672c\u4f8b\u5916\u627f\u62c5\u4e13\u5bb6"));const shares=tables.find(t=>t.textContent.includes("\u6700\u7ec8\u57fa\u7ebf\u6bd4\u4f8b"));return retained.children[2].textContent==="3.00"&&retained.children[4].textContent==="0.00"&&loss.children[2].textContent==="0.00"&&loss.children[4].textContent==="3.00"&&shares.querySelectorAll("tbody tr").length===5})()' 'Retained cost3 and independent exception loss3 remain separate from five-role profit table'
    Check '["ORD-FUNDED-001","SET-FUNDED-001","BATCH-FUNDED-A","BATCH-FUNDED-B","final-baseline-sha256","funding-proof-sha256","manual-proof-sha256"].every(t=>document.querySelector("#pending").textContent.includes(t))' 'Source order/settlement/batches/final baseline and manual funding proof are readable'
    Check 'document.querySelector("#refund").textContent.includes("30.00") && document.querySelector("#refund").textContent.includes("-32.00") && document.querySelector("#refund").textContent.includes("manual-proof-sha256") && document.querySelector("#segments").textContent.includes("BATCH-FUNDED-B")' 'Refund detail and economic segment components preserve source and cash/credit evidence'
    Check 'document.querySelectorAll("button,input,select,textarea").length===0' 'All mounted evidence components are read-only'
    Check '(()=>{const s=document.querySelector("#vc-internal");return s.textContent.includes("\u5e73\u53f0\u5185\u90e8\u5438\u6536\u6062\u590d")&&s.textContent.includes("internal-vc-projection-sha256")&&s.textContent.includes("internal-funding-proof-sha256")&&[...s.querySelectorAll("tbody td")].some(d=>d.textContent==="\u5185\u90e8\u6536\u76ca")&&![...s.querySelectorAll("tbody td")].some(d=>d.textContent==="\u5e94\u4ed8")&&!s.textContent.includes("\u5df2\u767b\u8bb0\u4ed8\u6b3e")&&!s.querySelector("[aria-label=\"\u73b0\u91d1\u9000\u6b3e\u4e0e\u8865\u5dee\u8fd4\u8fd8\u533a\u5206\"]")})()' 'VC internal absorption restoration is internal equity only, not external payable, payment or buyer refund'
    Check '(()=>{const s=document.querySelector("#vc-external");return s.textContent.includes("external-vc-projection-sha256")&&s.textContent.includes("original-manual-sha256")&&s.textContent.includes("\u4e0d\u4ee3\u8868\u5df2\u7ecf\u4ed8\u6b3e")&&[...s.querySelectorAll("tbody td")].some(d=>d.textContent==="\u5e94\u4ed8")})()' 'VC external payer return projection retains original funding proof without inferring payment'
    foreach($width in @(1440,390)){
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$width;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
        Start-Sleep -Milliseconds 300
        Check 'document.documentElement.scrollWidth<=innerWidth' ('No page horizontal overflow at '+$width)
        $name='funded-combinations-'+$width+'.png';$shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$true};[IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data));$result.screenshots+=,$name
    }
    if($script:blocked -or $script:nativeDialogs){throw 'Unexpected request or native dialog'}
    $result.status='passed';$result.blocked_requests=$script:blocked
}catch{$result.error=$_.Exception.Message;try{$result.failure_ui=Js 'document.body.innerText.slice(-12000)'}catch{}}
finally{
    if($script:socket){$script:socket.Dispose()};if($script:chrome){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    $result|ConvertTo-Json -Depth 12|Set-Content -Encoding UTF8 (Join-Path $OutputDir 'result.json')
}
if($result.status -cne 'passed'){throw $result.error};Write-Output ('Passed '+$result.checks.Count+' focused readonly Chrome checks')
