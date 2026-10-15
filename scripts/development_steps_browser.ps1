<#
Read-only deployed development-step acceptance; run after API/Web deployment.
Windows PowerShell example (TokenFile is a WSL absolute path, never a token):
  & .\scripts\development_steps_browser.ps1 -TokenFile /tmp/private-r3/admin.token
TokenFile: plain JWT, regular 0600 file inside a 0700 WSL directory.
PlanFile defaults to packaged backend/app/development_steps.json (32 R3/124 steps).
No login, fixtures, task updates or non-GET/HEAD requests. SSE GET is permitted.
Artifacts: sanitized result.json plus clipped task-panel PNGs, no raw DTO/HTML.
This verifies deployed UI/API; it does not prove server-side GET immutability.
#>
param(
    [Parameter(Mandatory=$true)][string]$TokenFile,
    [string]$BaseUrl='http://192.168.10.10:30080',
    [string]$PlanFile,
    [string]$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe',
    [string]$OutputDir="$env:TEMP\market-development-steps-browser"
)
$ErrorActionPreference='Stop'
if(-not $PlanFile){$PlanFile=Join-Path $PSScriptRoot '../backend/app/development_steps.json'}
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$script:stage='input validation'; $script:artifactDir=''
$profile=Join-Path $env:TEMP ('market-dev-steps-'+[Guid]::NewGuid().ToString('N'))
$token=$null; $tokenJson=$null
$result=@{status='failed';scope='real deployed development UI/API; GET/HEAD only';checks=@();screenshots=@()}

function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $uri=[Uri]$url
        if($method -cnotin @('GET','HEAD') -or $postData -or
           $uri.GetLeftPart([UriPartial]::Authority) -cne $BaseUrl -or
           $uri.UserInfo -or $uri.Fragment){return $false}
        if($uri.Query -and -not ($uri.AbsolutePath -ceq '/console' -and
            $uri.Query -ceq '?view=development')){return $false}
        return $uri.AbsolutePath -cin @('/console','/favicon.ico','/logo.svg',
            '/api/auth/me','/api/development/tasks','/api/settings/application-address-policy',
            '/api/storefront/buyer-enterprises','/api/notifications/summary',
            '/api/notifications/config','/api/notifications/stream','/api/notifications/sender-context') -or
            $uri.AbsolutePath -cmatch '^/assets/[A-Za-z0-9_./-]+$'
    } catch {return $false}
}

function Read-PrivateToken([string]$path) {
    if(-not $path.StartsWith('/') -or $path -match '[\r\n]'){throw 'Private token path invalid'}
    $description=& wsl.exe --exec stat --printf '%a|%F' -- $path 2>$null
    if($LASTEXITCODE -ne 0 -or [string]$description -cne '600|regular file'){throw 'Token file must be regular 0600'}
    $parent=$path.Substring(0,$path.LastIndexOf('/'))
    $description=& wsl.exe --exec stat --printf '%a|%F' -- $parent 2>$null
    if($LASTEXITCODE -ne 0 -or [string]$description -cne '700|directory'){throw 'Token directory must be 0700'}
    $windowsPath=& wsl.exe --exec wslpath -w $path 2>$null
    if($LASTEXITCODE -ne 0 -or -not $windowsPath){throw 'Private token path conversion failed'}
    $value=[IO.File]::ReadAllText([string]$windowsPath).Trim()
    if($value.Length -gt 16384 -or $value -cnotmatch '^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$'){
        throw 'Private token format invalid'
    }
    return $value
}

function Read-ExpectedSteps([string]$path) {
    $document=Get-Content -LiteralPath $path -Raw -Encoding UTF8|ConvertFrom-Json
    $expected=@()
    foreach($property in $document.tasks.PSObject.Properties) {
        if($property.Name -cnotmatch '^TRD-R3-[A-Z]+-\d{3}$'){continue}
        $steps=@(); $i=0
        foreach($title in $property.Value) {
            if($title -isnot [string] -or -not $title.Trim()){throw 'Expected step content invalid'}
            $i++; $steps+=@{id=('{0}-S{1:D2}' -f $property.Name,$i);title=$title;status='untracked'}
        }
        if(-not $steps.Count){throw 'Expected task has no steps'}
        $expected+=@{code=$property.Name;subtasks=$steps}
    }
    $total=($expected|ForEach-Object {$_.subtasks.Count}|Measure-Object -Sum).Sum
    if($expected.Count -ne 32 -or $total -ne 124){throw 'Expected 32 R3 tasks and 124 steps'}
    return $expected
}

function Connect-ReadOnlyChrome {
    if(-not(Test-Path -LiteralPath $ChromePath -PathType Leaf)){throw 'Chrome missing'}
    New-Item -ItemType Directory -Path $profile|Out-Null
    $acl=[Security.AccessControl.DirectorySecurity]::new(); $acl.SetAccessRuleProtection($true,$false)
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User; $acl.SetOwner($sid)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
    Set-Acl -LiteralPath $profile -AclObject $acl
    $script:chrome=Start-Process $ChromePath -ArgumentList @('--headless=new','--remote-debugging-port=0',
        "--user-data-dir=`"$profile`"",'--no-first-run','--disable-background-networking',
        '--disable-sync','--disable-extensions','about:blank') -PassThru
    $target=$null; $port=0
    for($i=0;$i -lt 40 -and -not $target;$i++) {
        try {
            $file=Join-Path $profile 'DevToolsActivePort'
            if(Test-Path -LiteralPath $file) {
                $lines=@(Get-Content -LiteralPath $file)
                if($lines.Count -eq 2 -and $lines[0] -match '^\d{1,5}$') {
                    $port=[int]$lines[0]
                    $pages=Invoke-RestMethod "http://127.0.0.1:$port/json/list" -TimeoutSec 2
                    $target=@($pages|Where-Object {$_.type -ceq 'page' -and $_.url -ceq 'about:blank'})|Select-Object -First 1
                }
            }
        } catch {}
        if(-not $target){Start-Sleep -Milliseconds 250}
    }
    if(-not $target){throw 'Owned Chrome startup timed out'}
    $endpoint=[Uri]$target.webSocketDebuggerUrl
    if($endpoint.Scheme -cne 'ws' -or $endpoint.Host -cne '127.0.0.1' -or $endpoint.Port -ne $port -or
       $endpoint.UserInfo -or $endpoint.Query -or $endpoint.Fragment -or
       $endpoint.AbsolutePath -notmatch '^/devtools/page/[A-Za-z0-9-]+$'){throw 'Unexpected CDP endpoint'}
    $script:socket=[Net.WebSockets.ClientWebSocket]::new()
    $deadline=[Threading.CancellationTokenSource]::new(10000)
    try{$null=$script:socket.ConnectAsync($endpoint,$deadline.Token).GetAwaiter().GetResult()}finally{$deadline.Dispose()}
    foreach($method in @('Page.enable','Network.enable')){Cdp $method @{}|Out-Null}
    Cdp 'Network.setCacheDisabled' @{cacheDisabled=$true}|Out-Null
    Cdp 'Network.setBypassServiceWorker' @{bypass=$true}|Out-Null
    Cdp 'Fetch.enable' @{patterns=@(@{urlPattern='*';requestStage='Request'})}|Out-Null
    Cdp 'Browser.setDownloadBehavior' @{behavior='deny'}|Out-Null
}

function Assert-NoMutations {
    if($script:blocked -or $script:nativeDialogs){throw 'Denied request or native dialog observed'}
}

function Save-PanelScreenshot([string]$name) {
    $safe=Js "(()=>{const p=document.querySelector('.task-panel');if(!p)return false;const t=p.innerText;return !t.includes($tokenJson)&&!p.querySelector('input,textarea')&&!/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|mk_[A-Za-z0-9_-]{16,}|-----BEGIN.*PRIVATE KEY/.test(t)})()"
    if(-not $safe){throw 'Screenshot privacy check failed'}
    $expanded=$name.EndsWith('-expanded.png')
    if($expanded) {
        $position=@'
(() => {
 const task=window.__expectedDevelopmentSteps[0];
 const list=document.getElementById('task-subtasks-'+task.code);
 const first=list?.querySelector('li'), title=first?.querySelector('.task-subtask-title');
 if(!title)throw Error('Expanded R3 step missing');
 const wrap=list.closest('.table-wrap');
 title.scrollIntoView({block:'center',inline:'start',behavior:'instant'});
 const range=document.createRange();range.selectNodeContents(title);
 const bounds=range.getBoundingClientRect(), container=wrap.getBoundingClientRect();
 wrap.scrollLeft+=bounds.left-Math.max(0,container.left)-12;
 window.__developmentScreenshotStep=first;
 return true;
})()
'@
        Js $position|Out-Null
        $region=@'
(() => {
 const first=window.__developmentScreenshotStep, list=first.closest('ol');
 const row=list.closest('tr').getBoundingClientRect(), wrap=list.closest('.table-wrap').getBoundingClientRect();
 const x=Math.max(0,row.left,wrap.left),y=Math.max(0,row.top);
 const right=Math.min(innerWidth,row.right,wrap.right),bottom=Math.min(innerHeight,row.bottom);
 const range=document.createRange();range.selectNodeContents(first.querySelector('.task-subtask-title'));
 const text=range.getBoundingClientRect();
 if(text.width<=0||text.left<x||text.right>right||text.top<y||text.bottom>bottom)
   throw Error('First R3 step title is not completely visible');
 return {x:x+scrollX,y:y+scrollY,width:right-x,height:bottom-y,scale:1};
})()
'@
        $clip=Js $region
    } else {
        Js "(()=>{const p=document.querySelector('.task-panel');p.querySelector('.table-wrap').scrollLeft=0;p.scrollIntoView({block:'start',behavior:'instant'});return true})()"|Out-Null
        $clip=Js "(()=>{const r=document.querySelector('.task-panel').getBoundingClientRect();const x=Math.max(0,r.left),y=Math.max(0,r.top),w=Math.min(innerWidth,r.right)-x,h=Math.min(innerHeight,r.bottom)-y;return {x:x+scrollX,y:y+scrollY,width:w,height:h,scale:1}})()"
    }
    if($clip.width -lt 20 -or $clip.height -lt 20){throw 'Panel not visible'}
    Assert-NoMutations
    $shot=Cdp 'Page.captureScreenshot' @{format='png';clip=$clip;captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $script:artifactDir $name),[Convert]::FromBase64String($shot.data))
    $result.screenshots+=,$name
}

try {
    $uri=[Uri]$BaseUrl
    if($uri.Scheme -cnotin @('http','https') -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or
       $uri.AbsolutePath -cne '/'){throw 'BaseUrl must be an origin'}
    $BaseUrl=$uri.GetLeftPart([UriPartial]::Authority)
    $expected=@(Read-ExpectedSteps $PlanFile)
    $token=Read-PrivateToken $TokenFile
    $tokenJson=ConvertTo-Json -InputObject $token -Compress
    # Load only transport declarations, never execute the purchase workflow.
    $parseErrors=$null; $parseTokens=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'password_first_purchase_browser_cdp.ps1'),[ref]$parseTokens,[ref]$parseErrors)
    if($parseErrors.Count){throw 'Transport parse failed'}
    foreach($name in @('Send-Cdp','Cdp','Js','WaitJs')) {
        $nodes=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$false))
        if($nodes.Count -ne 1){throw 'Transport declaration changed'}
        if($name -ceq 'Cdp' -and (-not $nodes[0].Extent.Text.Contains('Allowed-Request $request.method $request.url $request.postData') -or
            -not $nodes[0].Extent.Text.Contains("'Fetch.failRequest'"))){throw 'Transport interception guard changed'}
        . ([ScriptBlock]::Create($nodes[0].Extent.Text))
    }
    $script:artifactDir=Join-Path $OutputDir ([Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $script:artifactDir|Out-Null
    $script:stage='owned browser startup'; Connect-ReadOnlyChrome
    $originJson=ConvertTo-Json -InputObject $BaseUrl -Compress
    $expectedJson=ConvertTo-Json -InputObject $expected -Depth 15 -Compress
    $injection="if(location.origin===$originJson){localStorage.setItem('market_token',$tokenJson);window.__expectedDevelopmentSteps=$expectedJson;}"
    Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source=$injection}|Out-Null
    $script:stage='development navigation'
    # Query is narrowly permitted for this document only; API queries stay denied.
    Cdp 'Page.navigate' @{url="$BaseUrl/console?view=development"}|Out-Null
    WaitJs '!!document.querySelector(".task-panel .task-subtask-toggle")' 30
    $script:stage='live API content'
    $apiCheck=@'
(async()=>{
 const h={Authorization:'Bearer '+localStorage.getItem('market_token')};
 const a=await fetch('/api/auth/me',{headers:h}); if(!a.ok)return false;
 const me=await a.json(); if(me.user?.platform_role!=='super_admin')return false;
 const r=await fetch('/api/development/tasks',{headers:h}); if(!r.ok)return false;
 const d=await r.json(), rows=d.items?.filter(t=>t.code.startsWith('TRD-R3-'));
 const expected=window.__expectedDevelopmentSteps;
 return rows?.length===32 && expected.every(e=>{
   const t=rows.find(t=>t.code===e.code);
   return t&&Array.isArray(t.subtasks)&&t.subtasks.length===e.subtasks.length&&e.subtasks.every((s,i)=>
     t.subtasks[i].id===s.id&&t.subtasks[i].title===s.title&&t.subtasks[i].status==='untracked');
 })&&rows.reduce((n,t)=>n+t.subtasks.length,0)===124;
})()
'@
    if(-not (Js $apiCheck)){throw 'Live admin API differs from expected 32 tasks/124 steps'}
    $result.checks+='live super_admin API matches 32 R3 tasks and 124 ordered step IDs/titles/statuses'
    $domHelpers=@'
window.__devSteps={
 buttons:()=>window.__expectedDevelopmentSteps.map(t=>document.querySelector(`[aria-controls="task-subtasks-${t.code}"]`)),
 collapsed:()=>window.__devSteps.buttons().every(b=>b&&!b.disabled&&b.getAttribute('aria-expanded')==='false'),
 expanded:()=>window.__expectedDevelopmentSteps.every(t=>{
   const b=document.querySelector(`[aria-controls="task-subtasks-${t.code}"]`),l=document.getElementById('task-subtasks-'+t.code);
   return b?.getAttribute('aria-expanded')==='true'&&l&&l.children.length===t.subtasks.length&&t.subtasks.every((s,i)=>{
     const li=l.children[i];return li.querySelector('.task-subtask-id')?.textContent.trim()===s.id&&
     li.querySelector('.task-subtask-title')?.textContent.trim()===s.title.trim()&&
     li.querySelector('.task-subtask-status')?.textContent.trim()==='\u672a\u5355\u72ec\u6807\u8bb0';
   });
 }),
 toggleAll:()=>document.querySelector('.task-register-tools button[aria-expanded]').click()
};true
'@
    foreach($viewport in @(@{name='desktop';width=1440;height=1000},@{name='mobile';width=390;height=844})) {
        $script:stage=$viewport.name+' expand/collapse'
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$viewport.width;height=$viewport.height;deviceScaleFactor=1;mobile=($viewport.name -ceq 'mobile')}|Out-Null
        Js $domHelpers|Out-Null
        WaitJs 'window.__devSteps.collapsed()' 10
        Js 'window.__devSteps.buttons()[0].click();true'|Out-Null
        WaitJs 'window.__devSteps.buttons()[0].getAttribute("aria-expanded")==="true" && !!document.getElementById(window.__devSteps.buttons()[0].getAttribute("aria-controls"))' 10
        Js 'window.__devSteps.buttons()[0].click();true'|Out-Null
        WaitJs 'window.__devSteps.collapsed()' 10
        Save-PanelScreenshot ($viewport.name+'-collapsed.png')
        Js 'window.__devSteps.toggleAll();true'|Out-Null
        WaitJs 'window.__devSteps.expanded()' 10
        Save-PanelScreenshot ($viewport.name+'-expanded.png')
        Js 'window.__devSteps.toggleAll();true'|Out-Null
        WaitJs 'window.__devSteps.collapsed() && !document.querySelector(".task-subtask-list")' 10
        Assert-NoMutations
        $result.checks+=($viewport.name+' single/all expand-collapse and exact ordered 124 DOM steps')
    }
    $script:stage='refresh'
    Cdp 'Page.reload' @{}|Out-Null
    WaitJs '!!document.querySelector(".task-panel .task-subtask-toggle")' 30
    Js $domHelpers|Out-Null
    WaitJs 'window.__devSteps.collapsed()' 10
    if(-not (Js $apiCheck)){throw 'Refreshed API content mismatch'}
    Assert-NoMutations
    $result.checks+='authenticated refresh and unchanged step content'
    $result.task_count=32; $result.step_count=124; $result.status='passed'
} catch {
    # Never serialize raw exceptions, tokens, request headers, HTML or DTOs.
    $result.status='failed'; $result.failure='Verification failed; private diagnostics suppressed'
} finally {
    $result.stage=$script:stage; $result.blocked_requests=$script:blocked
    $result.native_dialogs=$script:nativeDialogs; $result.captured_at=[DateTime]::UtcNow.ToString('o')
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome -and -not $script:chrome.HasExited){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path -LiteralPath $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    $token=$null; $tokenJson=$null; $injection=$null
    if($script:artifactDir){$result|ConvertTo-Json -Depth 8|Set-Content -LiteralPath (Join-Path $script:artifactDir 'result.json') -Encoding UTF8}
}
if($result.status -cne 'passed'){throw 'Development steps browser verification failed; see sanitized result if created'}
Write-Output 'Development steps browser verification passed: 32 R3 tasks, 124 steps; GET/HEAD only'
