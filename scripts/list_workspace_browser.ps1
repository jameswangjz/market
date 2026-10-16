param([Parameter(Mandatory=$true)][string]$TokenFile,
      [string]$BaseUrl='http://192.168.10.10:30080',
      [string]$OutputDir="$env:TEMP\market-list-workspace-browser")
$ErrorActionPreference='Stop'
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-list-'+[Guid]::NewGuid().ToString('N'))
$result=@{status='failed';checks=@();screenshots=@();scope='deployed platform administrator read-only list UI; no financial mutation'}
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $u=[Uri]$url
        if($method -cnotin @('GET','HEAD') -or $postData -or $u.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or $u.UserInfo){return $false}
        if($u.AbsolutePath -cmatch '^/assets/[A-Za-z0-9_./-]+$'){return $true}
        return $u.AbsolutePath -cin @('/console','/favicon.ico','/logo.svg','/market-build.json',
          '/api/auth/me','/api/settings/application-address-policy','/api/storefront/buyer-enterprises',
          '/api/products','/api/product-categories','/api/settlement-rules','/api/orders',
          '/api/delivery-tasks','/api/after-sales','/api/refunds','/api/development/tasks',
          '/api/notifications/summary','/api/notifications/stream','/api/notifications/sender-context') -or
          $u.AbsolutePath -cmatch '^/api/gateway/routes(?:/[a-zA-Z0-9_-]+/(?:stats|revisions|alerts))?$'
    } catch {return $false}
}
function Check([string]$expression,[string]$description) {
    if(-not (Js $expression)){throw $description}
    $result.checks+=,$description
}
function Open-View([string]$view) {
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=$view"}|Out-Null
    WaitJs '!!document.querySelector(".main-list-filters") && !document.querySelector(".main-list-filters button:disabled")' 30
}
try {
    $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'password_first_purchase_browser_cdp.ps1'),[ref]$null,[ref]$null)
    foreach($name in @('Send-Cdp','Cdp','Js','WaitJs')) {
        $fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false)
        if(-not $fn){throw 'Browser transport missing'}
        . ([ScriptBlock]::Create($fn.Extent.Text))
    }
    $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'development_steps_browser.ps1'),[ref]$null,[ref]$null)
    foreach($name in @('Read-PrivateToken','Connect-ReadOnlyChrome')) {
        $fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false)
        if(-not $fn){throw 'Owned browser helper missing'}
        . ([ScriptBlock]::Create($fn.Extent.Text))
    }
    $token=Read-PrivateToken $TokenFile
    $origin=ConvertTo-Json $BaseUrl -Compress; $secret=ConvertTo-Json $token -Compress
    Connect-ReadOnlyChrome
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source="if(location.origin===$origin){localStorage.setItem('market_token',$secret);window.__listErrors=[];addEventListener('error',e=>window.__listErrors.push(e.message));}"}|Out-Null
    foreach($view in @('products','orders')) {
        Open-View $view
        Check 'document.querySelectorAll(".main-list-filters label").length>=5 && !!document.querySelector(".pagination-controls")' "$view filter and pagination controls"
        Js '(()=>{const s=document.querySelector(".pagination-controls select");s.value="10";s.dispatchEvent(new Event("change",{bubbles:true}));return true})()'|Out-Null
        WaitJs 'document.querySelector(".pagination-controls select")?.value==="10" && !document.querySelector(".main-list-filters button:disabled")' 20
        Js '(()=>{const f=document.querySelector(".main-list-filters"),i=f.querySelector("input");i.value="__LIST_QA_NO_MATCH_20261016__";i.dispatchEvent(new Event("input",{bubbles:true}));f.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));return true})()'|Out-Null
        WaitJs 'document.querySelector(".pagination-total")?.textContent.includes("0") && !document.querySelector(".main-list-filters button:disabled")' 20
        Check 'document.querySelector(".pagination-position")?.textContent.trim()==="1 / 1" && !document.querySelector(".main-list-filters [role=alert]")' "$view unmatched search and page reset"
        Js 'document.querySelector(".main-list-filters button[type=button]").click();true'|Out-Null
        WaitJs '!document.querySelector(".main-list-filters button:disabled")' 20
        if($view -ceq 'orders'){Check 'document.querySelector("main")?.textContent.includes("产品及提供方") && document.querySelector("main")?.textContent.includes("购买方")' 'orders show separate trading parties'}
    }
    Open-View 'delivery'
    Check '[...document.querySelectorAll(".settings-tabs button")].some(b=>b.textContent.includes("退款申请"))' 'delivery workspace has refund tab'
    foreach($text in @('售后工单','退款申请','交付任务')) {
        $label=ConvertTo-Json $text -Compress
        Js "[...document.querySelectorAll('.settings-tabs button')].find(b=>b.textContent.includes($label)).click();true"|Out-Null
        WaitJs '(()=>{const a=[...document.querySelectorAll(".pagination-controls")].filter(e=>e.getBoundingClientRect().height>0);return a.length===1&&a[0].getAttribute("aria-busy")==="false"})()' 20
        Check '!![...document.querySelectorAll(".main-list-filters,.refund-filters")].find(e=>e.getBoundingClientRect().height>0)' "$text independent list filters"
    }
    Open-View 'gateway'
    Check '(()=>{const s=[...document.querySelectorAll(".main-list-filters label")].find(e=>e.textContent.includes("产品状态"))?.querySelector("select");return s?.value==="published"})()' 'gateway defaults to published products'
    Check '!([...document.querySelectorAll("main button")].some(b=>/保存配置|发布路由|回滚路由/.test(b.textContent)))' 'gateway has no route edit or publish action'
    Js '(()=>{const s=[...document.querySelectorAll(".main-list-filters label")].find(e=>e.textContent.includes("产品状态")).querySelector("select");s.value="all";s.dispatchEvent(new Event("change",{bubbles:true}));document.querySelector(".main-list-filters").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));return true})()'|Out-Null
    WaitJs '!document.querySelector(".main-list-filters button:disabled")' 20
    Check '!document.querySelector(".main-list-filters [role=alert]")' 'gateway historical routes query succeeds'
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    foreach($viewport in @(@{name='desktop';width=1440;height=1000},@{name='mobile';width=390;height=844})) {
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$viewport.width;height=$viewport.height;deviceScaleFactor=1;mobile=$false}|Out-Null
        Js '(async()=>{await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));window.scrollTo(0,0);const r=document.querySelector(".main-list-filters").getBoundingClientRect();window.scrollTo(0,Math.max(0,r.top+scrollY-20));return true})()'|Out-Null
        $clip=Js '(()=>{const r=document.querySelector(".main-list-filters").getBoundingClientRect();const x=Math.max(0,r.left),y=Math.max(0,r.top);return {x:x+scrollX,y:y+scrollY,width:Math.min(innerWidth,r.right)-x,height:Math.min(innerHeight,r.bottom)-y,scale:1}})()'
        if($clip.width -lt 100 -or $clip.height -lt 30){throw 'Filter area not visible'}
        Check '(()=>{const f=document.querySelector(".main-list-filters");return [...f.querySelectorAll("input,select,button")].every(e=>{const r=e.getBoundingClientRect(),p=f.getBoundingClientRect();return r.width>0&&r.left>=p.left-2&&r.right<=p.right+2})})()' ($viewport.name+' filter controls stay within container')
        $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
        $name='gateway-filters-'+$viewport.name+'.png'
        [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data))
        $result.screenshots+=,$name
    }
    Cdp 'Emulation.setDeviceMetricsOverride' @{width=1440;height=1000;deviceScaleFactor=1;mobile=$false}|Out-Null
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=development"}|Out-Null
    WaitJs 'document.querySelector(".task-panel")?.textContent.includes("LIST-OPS-001")' 30
    Check '(async()=>{const r=await fetch("/api/development/tasks",{headers:{Authorization:"Bearer "+localStorage.getItem("market_token")}});return r.ok&&(await r.json()).items.filter(x=>x.code.startsWith("LIST-")).length===12})()' 'development API and UI include twelve LIST tasks'
    if($script:blocked -or $script:nativeDialogs){throw 'Unexpected blocked request or native dialog'}
    $result.status='passed'; $result.blocked=$script:blocked
} catch {$result.error='Browser list verification failed'; $result.stage=$result.checks.Count; $result.failure_type=$_.Exception.GetType().Name}
finally {
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome -and -not $script:chrome.HasExited){Stop-Process -Id $script:chrome.Id -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    New-Item -ItemType Directory -Force -Path $OutputDir|Out-Null
    $result|ConvertTo-Json -Depth 10|Set-Content -LiteralPath (Join-Path $OutputDir 'result.json') -Encoding UTF8
    $result|ConvertTo-Json -Depth 10
}
if($result.status -cne 'passed'){exit 1}
