param([string]$OutputDir = "$env:TEMP\market-ledger-e2e-results")
$shared = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'message_center_browser_cdp.ps1')
$start = $shared.IndexOf('$ErrorActionPreference')
$end = $shared.IndexOf('New-Item -ItemType Directory')
Invoke-Expression $shared.Substring($start, $end-$start)
New-Item -ItemType Directory -Force $OutputDir | Out-Null
try {
    Cdp 'DOM.enable' @{} | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false} | Out-Null
    Js 'Array.from(document.querySelectorAll(".nav-item")).find(n=>n.textContent.includes("\u6e05\u7b97\u5206\u8d26")).click();true' | Out-Null
    WaitJs 'Array.from(document.querySelectorAll(".tabs button")).some(n=>n.textContent.includes("\u5bf9\u8d26\u5dee\u5f02"))'
    Js 'Array.from(document.querySelectorAll(".tabs button")).find(n=>n.textContent.includes("\u5bf9\u8d26\u5dee\u5f02")).click();true' | Out-Null
    WaitJs '!!document.querySelector(".ledger-comparison input[type=file]") && !document.querySelector(".ledger-comparison .table-wrap[aria-busy=true]")'
    WaitJs '!!document.querySelector(".ledger-toolbar select").value'
    $batchId = Js 'document.querySelector(".ledger-toolbar select").value'
    if (!$batchId) { throw 'Existing batch required for read-only business-state acceptance' }
    $fixture = Join-Path $OutputDir 'independent-acceptance-fixture.json'
    @{entries=@(@{entry_no='QA-INDEPENDENT-20261009';order_no='ORD-QA-NONEXISTENT-20261009';amount='100.00';currency='CNY'})} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $fixture
    $root = Cdp 'DOM.getDocument' @{}
    $inputNode = Cdp 'DOM.querySelector' @{nodeId=$root.root.nodeId;selector='.ledger-comparison input[type=file]'}
    Cdp 'DOM.setFileInputFiles' @{nodeId=$inputNode.nodeId;files=@($fixture)} | Out-Null
    WaitJs 'Array.from(document.querySelectorAll(".ledger-import-row button")).some(b=>!b.disabled)'
    Js 'document.querySelector(".ledger-import-row button").click();true' | Out-Null
    WaitJs '!!document.querySelector(".ledger-modal")'
    if (Js '!!document.querySelector(".ledger-modal [role=alert]")') { throw 'Ledger import error' }
    WaitJs '!!document.querySelector(".ledger-resolution input")'
    $unknown = Js 'document.querySelector(".ledger-modal").textContent.includes("ORD-QA-NONEXISTENT-20261009")'
    if (!$unknown) { throw 'Independent unexpected order not displayed' }
    Screenshot 'ledger-difference-desktop.png'
    Js '(()=>{let t=document.querySelector(".ledger-resolution textarea"),i=document.querySelector(".ledger-resolution input");t.value="QA acceptance: independent nonexistent order fixture, not an actual payment";i.value="QA-LEDGER-20261009";[t,i].forEach(n=>n.dispatchEvent(new Event("input",{bubbles:true})));t.closest("form").requestSubmit();return true})()' | Out-Null
    WaitJs '!document.querySelector(".ledger-resolution input") && document.querySelector(".ledger-modal").textContent.includes("QA-LEDGER-20261009")'
    Js 'window.__ledgerDownload=null;URL.createObjectURL=function(blob){window.__ledgerDownload=blob;return window.__originalCreateObjectURL.call(URL,blob)};Array.from(document.querySelectorAll(".ledger-modal button")).find(b=>b.textContent.includes("JSON")).click();true' | Out-Null
    WaitJs '!!window.__ledgerDownload'
    $report = Js '(async()=>{let r=JSON.parse(await window.__ledgerDownload.text());return {id:r.id,status:r.status,real:r.real_channel_verified,rows:r.items.length}})()'
    if ($report.status -ne 'resolved' -or $report.real -ne $false) { throw 'Invalid report download metadata' }
    Screenshot 'ledger-resolved-desktop.png'
    Js 'document.querySelector(".ledger-modal .drawer-head button").click();true' | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=2;mobile=$true} | Out-Null
    Js 'document.querySelector(".ledger-comparison").scrollIntoView();true' | Out-Null
    Start-Sleep -Milliseconds 700
    Screenshot 'ledger-mobile.png'
    $geometry = Js '({width:innerWidth,bodyWidth:document.documentElement.scrollWidth})'
    if ($geometry.bodyWidth -gt $geometry.width+1) { throw 'Ledger page overflows mobile viewport' }
    @{status='passed';batch_id=$batchId;report=$report;geometry=$geometry;source='independent fictional ledger entry; no actual payment or order mutation'} | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'ledger-browser-results.json')
    Write-Output 'Ledger browser checks passed'
} finally { $socket.Dispose() }
