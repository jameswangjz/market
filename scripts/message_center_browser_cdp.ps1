param(
    [string]$BaseUrl = 'http://192.168.10.10:30080',
    [string]$Username = 'admin@market.local',
    [Parameter(Mandatory=$true)][string]$Password,
    [string]$OutputDir = "$env:TEMP\market-message-e2e-results"
)
$ErrorActionPreference = 'Stop'
$target = @(Invoke-RestMethod 'http://127.0.0.1:9222/json/list' | Where-Object { $_.type -eq 'page' })[0]
$socket = New-Object System.Net.WebSockets.ClientWebSocket
$socket.ConnectAsync([Uri]$target.webSocketDebuggerUrl, [Threading.CancellationToken]::None).GetAwaiter().GetResult()
$script:commandId = 0
function Cdp($method, $params) {
    $script:commandId++
    $id = $script:commandId
    $payload = @{id=$id;method=$method;params=$params} | ConvertTo-Json -Depth 30 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $socket.SendAsync([ArraySegment[byte]]::new($bytes), [Net.WebSockets.WebSocketMessageType]::Text, $true, [Threading.CancellationToken]::None).GetAwaiter().GetResult()
    while ($true) {
        $memory = [IO.MemoryStream]::new()
        do {
            $buffer = New-Object byte[] 65536
            $part = $socket.ReceiveAsync([ArraySegment[byte]]::new($buffer), [Threading.CancellationToken]::None).GetAwaiter().GetResult()
            $memory.Write($buffer,0,$part.Count)
        } while (!$part.EndOfMessage)
        $answer = [Text.Encoding]::UTF8.GetString($memory.ToArray()) | ConvertFrom-Json
        $memory.Dispose()
        if ($answer.id -eq $id) {
            if ($answer.error) { throw ($answer.error | ConvertTo-Json -Compress) }
            return $answer.result
        }
    }
}
function Js([string]$expression) {
    $response = Cdp 'Runtime.evaluate' @{expression=$expression;awaitPromise=$true;returnByValue=$true}
    if ($response.exceptionDetails) { throw ($response.exceptionDetails | ConvertTo-Json -Depth 10 -Compress) }
    return $response.result.value
}
function WaitJs([string]$expression) {
    for ($i=0;$i -lt 100;$i++) {
        if (Js $expression) { return }
        Start-Sleep -Milliseconds 300
    }
    throw "Browser condition timed out: $expression"
}
function Screenshot([string]$name) {
    $shot = Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name), [Convert]::FromBase64String($shot.data))
}
New-Item -ItemType Directory -Force $OutputDir | Out-Null
try {
    Cdp 'Page.enable' @{} | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false} | Out-Null
    Cdp 'Page.navigate' @{url=$BaseUrl} | Out-Null
    WaitJs 'document.readyState === "complete"'
    Js 'localStorage.removeItem("market_token");location.reload();true' | Out-Null
    WaitJs '!!document.querySelector("input[autocomplete=username]")'
    $u = $Username | ConvertTo-Json -Compress
    $p = $Password | ConvertTo-Json -Compress
    Js "(()=>{let u=document.querySelector('input[autocomplete=username]'),p=document.querySelector('input[autocomplete=current-password]');u.value=$u;u.dispatchEvent(new Event('input',{bubbles:true}));p.value=$p;p.dispatchEvent(new Event('input',{bubbles:true}));u.closest('form').requestSubmit();return true})()" | Out-Null
    WaitJs '!!document.querySelector(".sidebar")'
    Js 'document.querySelector(".notification-btn").click();true' | Out-Null
    WaitJs '!!document.querySelector(".notification-preview")'
    Js 'document.querySelector(".notification-preview-head .text-btn").click();true' | Out-Null
    WaitJs '!!document.querySelector(".message-center") && !document.querySelector(".message-center .table-wrap").getAttribute("aria-busy").includes("true")'
    WaitJs 'Array.from(document.querySelectorAll(".message-center button")).some(b=>b.textContent.trim()==="\u5199\u6d88\u606f")'
    Js 'Array.from(document.querySelectorAll(".message-center button")).find(b=>b.textContent.trim()==="\u5199\u6d88\u606f").click();true' | Out-Null
    WaitJs '!!document.querySelector("[aria-label=\"\u5199\u6d88\u606f\"]")'
    $title = 'MC-BROWSER-' + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $t = $title | ConvertTo-Json -Compress
    Js "(()=>{let m=document.querySelector('[aria-label=\"\u5199\u6d88\u606f\"]'),t=m.querySelector('input[maxlength=\"220\"]'),body=m.querySelector('textarea'),s=m.querySelector('select[multiple]');t.value=$t;t.dispatchEvent(new Event('input',{bubbles:true}));body.value='Browser lifecycle verification';body.dispatchEvent(new Event('input',{bubbles:true}));let option=Array.from(s.options).find(o=>o.textContent.includes($u));if(!option)throw Error('admin recipient not found');option.selected=true;s.dispatchEvent(new Event('change',{bubbles:true}));Array.from(m.querySelectorAll('button')).find(b=>b.textContent.trim()==='\u4fdd\u5b58\u8349\u7a3f').click();return true})()" | Out-Null
    WaitJs "Array.from(document.querySelectorAll('.message-title button')).some(b=>b.textContent===$t) && !document.querySelector('[aria-label=\"\u5199\u6d88\u606f\"]')"
    Screenshot 'message-center-draft-desktop.png'
    Js "(()=>{let row=Array.from(document.querySelectorAll('tbody tr')).find(r=>r.textContent.includes($t));Array.from(row.querySelectorAll('button')).find(b=>b.textContent.trim()==='\u53d1\u9001').click();return true})()" | Out-Null
    WaitJs '!!document.querySelector("[aria-label=\"\u786e\u8ba4\u64cd\u4f5c\"]")'
    Js 'document.querySelector("[aria-label=\"\u786e\u8ba4\u64cd\u4f5c\"] .primary-btn").click();true' | Out-Null
    WaitJs '!document.querySelector("[aria-label=\"\u786e\u8ba4\u64cd\u4f5c\"]")'
    Js 'Array.from(document.querySelectorAll(".message-tabs button")).find(b=>b.textContent.trim()==="\u6536\u4ef6\u7bb1").click();true' | Out-Null
    WaitJs "Array.from(document.querySelectorAll('.message-title button')).some(b=>b.textContent===$t)"
    Js "Array.from(document.querySelectorAll('.message-title button')).find(b=>b.textContent===$t).click();true" | Out-Null
    WaitJs '!!document.querySelector("[aria-label=\"\u6d88\u606f\u8be6\u60c5\"]")'
    Screenshot 'message-center-detail-desktop.png'
    Js 'document.querySelector("[aria-label=\"\u6d88\u606f\u8be6\u60c5\"] [aria-label=\"\u5173\u95ed\"]").click();true' | Out-Null
    Js 'document.querySelector(".message-center [aria-label=\"\u6d88\u606f\u8bbe\u7f6e\"]").click();true' | Out-Null
    WaitJs '!!document.querySelector(".platform-settings")'
    Screenshot 'message-center-settings-desktop.png'
    Js 'document.querySelector("[aria-label=\"\u6d88\u606f\u8bbe\u7f6e\"] [aria-label=\"\u5173\u95ed\"]").click();true' | Out-Null
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=390;height=844;deviceScaleFactor=2;mobile=$true} | Out-Null
    Start-Sleep -Seconds 1
    Screenshot 'message-center-mobile.png'
    $geometry = Js '({width:innerWidth,bodyWidth:document.documentElement.scrollWidth,errors:Array.from(document.querySelectorAll(".message-center [role=alert]")).map(n=>n.textContent)})'
    if ($geometry.errors.Count) { throw ($geometry.errors -join ';') }
    if ($geometry.bodyWidth -gt $geometry.width+1) { throw 'Mobile document overflows horizontally' }
    @{status='passed';title=$title;geometry=$geometry;checks=@('login','bell preview','draft save','draft publish','inbox detail/read','settings','desktop/mobile screenshots')} | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 (Join-Path $OutputDir 'browser-results.json')
    Write-Output 'Browser lifecycle checks passed'
} finally {
    $socket.Dispose()
}
