param(
    [string]$BaseUrl='http://192.168.10.10:30080',
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [string]$TokenFile='',
    [ValidateSet('Public','Admin','Operator','Provider','Member')][string]$Actor='Public',
    [string]$EnterpriseId='',
    [ValidatePattern('^index-[A-Za-z0-9_-]+\.js$')][string]$ExpectedAsset='index-B6tsYtRH.js',
    [string]$DeploymentTag='',
    [switch]$LocalEmptyFix,
    [switch]$FormsOnly,
    [switch]$RequireEmptyJumpDisabled,
    [switch]$CheckSlaReason,
    [switch]$SlaOnly,
    [switch]$CheckStrategicResponse,
    [switch]$MenusOnly,
    [Alias('Viewport')][ValidateSet('Both','Desktop','Mobile')][string]$ViewportMode='Both',
    [switch]$PaginationOnly,
    [switch]$VerifyAuditNavigation,
    [switch]$SelfTest
)
$ErrorActionPreference='Stop'
$ChromePath='C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile=Join-Path $env:TEMP ('market-r3-fe-'+[Guid]::NewGuid().ToString('N'))
$script:socket=$null; $script:chrome=$null; $script:commandId=0
$script:blocked=0; $script:nativeDialogs=0; $script:publicAuth=0
$script:networkStatus=@(); $script:networkFailures=@(); $script:consoleTypes=@(); $script:requestPaths=@{}
$script:stage='startup'; $token=$null
$result=@{actor=$Actor;status='pending';checks=@();gaps=@();findings=@();screenshots=@();deployment=$DeploymentTag;expected_asset=$ExpectedAsset;actual_entry_assets=@();tasks=@{'FE001'='not_run';'FE002'='not_run';'FE003'='not_run';'FE004'='not_run';'FE005'='not_run'}}
if($LocalEmptyFix){$result.deployment='local-current-dist; not deployed'}
function Matches-ExpectedAsset([string]$path) { return $path -ceq ('/assets/'+$ExpectedAsset) }
function Allowed-Request([string]$method,[string]$url,[string]$postData) {
    try {
        $uri=[Uri]$url
        return $method -cin @('GET','HEAD') -and -not $postData -and
            $uri.GetLeftPart([UriPartial]::Authority) -ceq $BaseUrl -and -not $uri.UserInfo -and
            $uri.AbsolutePath -cnotmatch '(?i)(credential|secret|download|/oauth/|/oidc/)' -and
            ($Actor -cne 'Public' -or $uri.AbsolutePath -cmatch '^/(?:$|products/|assets/|favicon.ico$|api/storefront/(?:products|categories))')
    } catch {return $false}
}
if($SelfTest) {
    foreach($bad in @(@('POST',"$BaseUrl/api/orders",'{}'),@('DELETE',"$BaseUrl/api/products/p",''),@('GET','https://example.invalid/',''),@('GET',"$BaseUrl/api/orders/a/api-credentials",''))) {
        if(Allowed-Request $bad[0] $bad[1] $bad[2]){throw 'Request guard failure'}
    }
    if(-not (Allowed-Request GET "$BaseUrl/api/storefront/products?page_size=10" '')){throw 'Public read guard failure'}
    if(-not (Matches-ExpectedAsset ('/assets/'+$ExpectedAsset)) -or (Matches-ExpectedAsset '/assets/index-mismatch.js') -or (Matches-ExpectedAsset ('/other/'+$ExpectedAsset))){throw 'Expected asset guard failure'}
    Write-Output ('Passed: offline request/asset guard tests; expected asset '+$ExpectedAsset+'; no browser or network'); return
}
$helpers=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'development_steps_browser.ps1'),[ref]$null,[ref]$null)
foreach($name in @('Connect-ReadOnlyChrome','Read-PrivateToken')) {
    $node=$helpers.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true)
    if(-not $node){throw 'Browser helper missing'}
    . ([ScriptBlock]::Create($node.Extent.Text))
}
$transport=[Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'password_first_purchase_browser_cdp.ps1'),[ref]$null,[ref]$null)
foreach($name in @('Send-Cdp','Cdp','Js','WaitJs')) {
    $node=$transport.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true)
    if(-not $node){throw 'CDP helper missing'}
    $definition=$node.Extent.Text
    if($name -ceq 'Cdp') {
        $marker="} elseif(`$answer.method -eq 'Page.javascriptDialogOpening') {"
        $observer=@'
} elseif($answer.method -eq 'Network.requestWillBeSent') {
    $script:requestPaths[$answer.params.requestId]=([Uri]$answer.params.request.url).AbsolutePath
} elseif($answer.method -eq 'Network.responseReceived') {
    $script:networkStatus+=@{path=([Uri]$answer.params.response.url).AbsolutePath;status=[int]$answer.params.response.status}
} elseif($answer.method -eq 'Network.loadingFailed') {
    $script:networkFailures+=@{path=$script:requestPaths[$answer.params.requestId];type=[string]$answer.params.type;canceled=[bool]$answer.params.canceled}
} elseif($answer.method -eq 'Runtime.consoleAPICalled') {
    $script:consoleTypes+=([string]$answer.params.type)
} elseif($answer.method -eq 'Runtime.exceptionThrown') {
    $script:consoleTypes+='exceptionThrown'
} elseif($answer.method -eq 'Page.javascriptDialogOpening') {
'@
        if(-not $definition.Contains($marker)){throw 'CDP observation hook missing'}
        $definition=$definition.Replace($marker,$observer)
    }
    . ([ScriptBlock]::Create($definition))
}
function Check([string]$expression,[string]$label) {
    if(-not (Js $expression)){$result.failed_check=$label;throw 'Browser assertion failed'}
    $result.checks+=,$label
    Write-Output ('passed: '+$label)
}
function Check-EntryAsset {
    $actual=Js '(()=>{const n=document.querySelector("script[type=module][src]");return n?new URL(n.src).pathname:""})()'
    if($result.actual_entry_assets -cnotcontains $actual){$result.actual_entry_assets+=,$actual}
    if(-not (Matches-ExpectedAsset $actual)){$result.failed_check='entry asset differs from ExpectedAsset';throw 'Entry asset mismatch'}
    $result.checks+=('entry asset matches '+$ExpectedAsset)
    Write-Output ('passed: entry asset matches '+$ExpectedAsset)
}
function Set-VerifiedPageSize([string]$selector,[int]$size) {
    $selectorJson=ConvertTo-Json -InputObject $selector -Compress
    WaitJs "(()=>{const n=document.querySelector($selectorJson),s=n?.querySelector('select');return !!s&&!s.disabled&&n.getAttribute('aria-busy')!=='true'})()" 20
    Js "(()=>{window.__r3PageSizeStart=performance.now();const s=document.querySelector($selectorJson).querySelector('select');s.value='$size';s.dispatchEvent(new Event('change',{bubbles:true}));return true})()"|Out-Null
    WaitJs "performance.getEntriesByType('resource').some(r=>{const u=new URL(r.name);return r.startTime>=window.__r3PageSizeStart&&u.pathname.startsWith('/api/')&&u.searchParams.get('page_size')==='$size'&&r.responseStatus===200})" 20
    WaitJs "(()=>{const n=document.querySelector($selectorJson),s=n?.querySelector('select');return !!s&&!s.disabled&&s.value==='$size'&&n.getAttribute('aria-busy')!=='true'})()" 20
    $result.checks+=('FE004 '+$selector+' page_size='+$size+' HTTP 200 and idle')
    Write-Output ('passed: page_size='+$size+' HTTP 200 and idle '+$selector)
}
function Shot([string]$name) {
    Check '!document.querySelector("input[type=password],.api-credential-reveal") && !/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|mk_[A-Za-z0-9_-]{24,}/.test(document.body.innerText)' 'screenshot privacy gate'
    $shot=Cdp 'Page.captureScreenshot' @{format='png';captureBeyondViewport=$false}
    [IO.File]::WriteAllBytes((Join-Path $OutputDir $name),[Convert]::FromBase64String($shot.data)); $result.screenshots+=,$name
}
function Navigate([string]$path) {
    Cdp 'Page.navigate' @{url=($BaseUrl+$path)}|Out-Null
    WaitJs 'document.readyState==="complete"' 15
}
try {
    if([Uri]$BaseUrl -and $BaseUrl -ne ([Uri]$BaseUrl).GetLeftPart([UriPartial]::Authority)){throw 'Origin required'}
    if(Test-Path $OutputDir){throw 'Fresh output directory required'}
    New-Item -ItemType Directory -Path $OutputDir|Out-Null
    if($Actor -cne 'Public'){$token=Read-PrivateToken $TokenFile}
    Connect-ReadOnlyChrome
    Cdp 'Runtime.enable' @{}|Out-Null
    if($token) {
        $quoted=ConvertTo-Json -InputObject $token -Compress
        $origin=ConvertTo-Json -InputObject $BaseUrl -Compress
        Cdp 'Page.addScriptToEvaluateOnNewDocument' @{source="if(location.origin===$origin){localStorage.setItem('market_token',$quoted);window.__r3QaTokenInstalled=true;}"}|Out-Null
    }
    $viewports=@(@{name='desktop';width=1440;height=1000},@{name='mobile';width=390;height=844})
    foreach($viewportSpec in $viewports) {
        if($ViewportMode -cne 'Both' -and $viewportSpec.name -ine $ViewportMode){continue}
        $viewport=$viewportSpec
        Cdp 'Emulation.setDeviceMetricsOverride' @{width=$viewport.width;height=$viewport.height;deviceScaleFactor=1;mobile=($viewport.name -ceq 'mobile')}|Out-Null
        if($LocalEmptyFix) {
            if($Actor -cne 'Public'){throw 'Local fix probe must be anonymous'}
            $script:stage=$viewport.name+' local empty pagination fix'
            Navigate '/?q=r3-fe-acceptance-no-match-20261014'
            WaitJs '!!document.querySelector(".pagination-controls input") && !document.querySelector(".store-state[role=status]")' 20
            Check '!document.querySelector(".store-card") && document.querySelector(".pagination-total").textContent.includes("0 条")' 'local fix: real public empty response'
            Check 'document.querySelector(".pagination-controls input").disabled && document.querySelector(".pagination-controls button[aria-label=跳转到指定页]").disabled' 'local fix: empty page jump input and action disabled'
            Check '!document.querySelector(".pagination-controls select").disabled' 'local fix: empty page-size selection stays available'
            Js 'document.querySelector(".pagination-controls").scrollIntoView({block:"center"});true'|Out-Null
            Check 'document.documentElement.scrollWidth<=innerWidth+1' 'local fix: no page overflow'
            Shot ($viewport.name+'-empty-fixed.png')
            continue
        }
        if($Actor -ceq 'Public') {
            $script:stage=$viewport.name+' public rendering'; Navigate '/'
            WaitJs '!!document.querySelector(".store-card") && !document.querySelector(".store-state[role=status]")' 20
            Check-EntryAsset
            Check 'document.querySelectorAll(".store-category-row").length===2 && !document.querySelector("select[aria-label=交付方式]")' 'FE001 two category rows; no delivery filter'
            Check '(()=>{const b=document.querySelectorAll(".store-category-row")[1].querySelectorAll("button");return b.length===1&&b[0].disabled})()' 'FE001 secondary all disabled without parent'
            Check '(()=>{const s=document.querySelector("select[aria-label=商品类型]");return ![...s.options].some(o=>["report","application"].includes(o.value))})()' 'FE001 retired filter types absent'
            Check 'document.documentElement.scrollWidth<=innerWidth+1 && [...document.querySelectorAll(".store-filters input,.store-filters select,.store-filters button,.pagination-controls input,.pagination-controls select,.pagination-controls button")].every(n=>{const r=n.getBoundingClientRect();return r.left>=-1&&r.right<=innerWidth+1})' ('FE001/FE004 '+$viewport.name+' page and control bounds')
            Shot ($viewport.name+'-storefront.png')
            $script:stage=$viewport.name+' filters'
            Js 'document.querySelector(".store-category-row button:nth-of-type(2)")?.click();true'|Out-Null
            WaitJs '!document.querySelector(".store-state[role=status]")' 15
            Check 'new URLSearchParams(location.search).has("parent_id") && !new URLSearchParams(location.search).has("category_id")' 'FE001 selecting parent clears child'
            Check 'document.querySelectorAll(".store-category-row")[1].querySelector("button").disabled===false' 'FE001 children enabled after parent'
            Js 'document.querySelectorAll(".store-category-row")[1].querySelector("button:nth-of-type(2)")?.click();true'|Out-Null
            WaitJs '!document.querySelector(".store-state[role=status]")' 15
            Check 'new URLSearchParams(location.search).has("category_id")' 'FE001 selected secondary category is sent'
            Js 'document.querySelector(".store-category-row button").click();true'|Out-Null
            WaitJs '!new URLSearchParams(location.search).has("parent_id") && !new URLSearchParams(location.search).has("category_id")' 15
            Js '(()=>{const n=document.querySelector(".store-search input");n.value="r3-fe-acceptance-no-match-20261014";n.dispatchEvent(new Event("input",{bubbles:true}));n.closest("form").requestSubmit();return true})()'|Out-Null
            WaitJs 'location.search.includes("r3-fe-acceptance-no-match") && !document.querySelector(".store-state[role=status]")' 15
            Check '!document.querySelector(".store-card") && !new URLSearchParams(location.search).has("page")' 'FE001 name filter empty result and page reset'
            if(-not (Js 'document.querySelector(".pagination-controls input").disabled && document.querySelector(".pagination-controls button[aria-label=跳转到指定页]").disabled')) {
                $result.findings+='FE004 empty total=0 still allows editing/jump controls; requirement says no jump on empty list'
            }
            if($RequireEmptyJumpDisabled) {
                Check 'document.querySelector(".pagination-controls input").disabled && document.querySelector(".pagination-controls button[aria-label=跳转到指定页]").disabled' 'FE004 deployed empty jump input and action disabled'
                Check '!document.querySelector(".pagination-controls select").disabled' 'FE004 empty page-size selector remains available'
            }
            Shot ($viewport.name+'-empty-result.png')
            Navigate '/'; WaitJs '!!document.querySelector(".store-card")' 15
            Js '(()=>{const name=document.querySelector(".store-card h2").textContent.trim(),n=document.querySelector(".store-search input");n.value=name;n.dispatchEvent(new Event("input",{bubbles:true}));n.closest("form").requestSubmit();return true})()'|Out-Null
            WaitJs 'new URLSearchParams(location.search).has("q") && !!document.querySelector(".store-card") && !document.querySelector(".store-state[role=status]")' 15
            Check '[...document.querySelectorAll(".store-card h2")].every(n=>n.textContent.includes(new URLSearchParams(location.search).get("q")))' 'FE001 positive name search matches rendered names'
            Navigate '/'; WaitJs '!!document.querySelector(".store-card")' 15
            Js '(()=>{const s=document.querySelector("select[aria-label=商品类型]");s.value="dataset";s.dispatchEvent(new Event("change",{bubbles:true}));return true})()'|Out-Null
            WaitJs 'new URLSearchParams(location.search).get("product_type")==="dataset" && !document.querySelector(".store-state[role=status]")' 15
            Check 'document.querySelector("select[aria-label=商品类型]").value==="dataset" && !new URLSearchParams(location.search).has("delivery_method")' 'FE001 type filter uses product_type'
            Check '[...document.querySelectorAll(".store-tags .catalog-tag")].every(n=>n.textContent.trim()==="数据集") && !!document.querySelector(".store-card")' 'FE001 type filter matches real dataset cards'
            $script:stage=$viewport.name+' pagination'
            Navigate '/'; WaitJs '!!document.querySelector(".store-card")' 15
            foreach($size in @(10,20,50,100)) {
                Js "(()=>{const s=document.querySelector('.pagination-controls select');s.value='$size';s.dispatchEvent(new Event('change',{bubbles:true}));return true})()"|Out-Null
                WaitJs "new URLSearchParams(location.search).get('page_size')==='$size' && !document.querySelector('.store-state[role=status]')" 15
            }
            $result.checks+='FE004 storefront real size requests 10/20/50/100'
            Js '(()=>{const n=document.querySelector(".pagination-controls input");n.value="0";n.dispatchEvent(new Event("input",{bubbles:true}));n.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true}));return true})()'|Out-Null
            WaitJs 'document.querySelector(".pagination-error").textContent.trim().length>0' 5
            Check '!new URLSearchParams(location.search).has("page")' 'FE004 invalid zero jump rejected'
            Navigate '/?page=999&page_size=10'
            WaitJs '!!document.querySelector(".store-card") && !new URLSearchParams(location.search).has("page")' 20
            Cdp 'Page.reload' @{}|Out-Null; WaitJs '!!document.querySelector(".store-card")' 15
            $result.checks+='FE004 storefront out-of-range recovery and F5'
            Js 'document.querySelector(".pagination-controls").scrollIntoView({block:"center"});true'|Out-Null
            Shot ($viewport.name+'-pagination.png')
        } else {
          if(-not $SlaOnly) {
          if(-not $PaginationOnly) {
            $script:stage=$viewport.name+' actor session'
            $suffix=if($EnterpriseId){'&enterprise_id='+[Uri]::EscapeDataString($EnterpriseId)}else{''}
            Navigate ('/console?view=development'+$suffix)
            WaitJs '!!document.querySelector(".sidebar") && !document.querySelector(".loading-line")' 25
            Check-EntryAsset
            if($Actor -ceq 'Admin'){
                WaitJs '!!document.querySelector(".task-panel")' 25
                Check '!!document.querySelector(".task-panel")' 'FE002 system administrator development view'
            }
            else {
                Check '!document.querySelector(".task-panel,.progress-panel") && ![...document.querySelectorAll(".sidebar button")].some(b=>b.textContent.includes("开发进度"))' 'FE002 non-system-admin development view denied'
                Check '!document.body.innerText.includes("开发任务监控台会显示")' 'FE002 non-system-admin fallback development text absent'
            }
            Shot ($viewport.name+'-'+$Actor.ToLower()+'.png')
            if($MenusOnly -and $Actor -cne 'Provider'){continue}
          }
            if($Actor -cin @('Admin','Operator') -and -not $FormsOnly) {
                $script:stage=$viewport.name+' message pagination'; Navigate '/console?view=messages'
                WaitJs '!!document.querySelector(".message-center .pagination-controls") && !document.querySelector(".message-center .pagination-controls[aria-busy=true]")' 20
                Check-EntryAsset
                Check 'JSON.stringify([...document.querySelector(".message-center .pagination-controls select").options].map(n=>n.value))===JSON.stringify(["10","20","50","100"])' 'FE004 message size options'
                foreach($size in @(10,20,50,100)) {
                    Set-VerifiedPageSize '.message-center .pagination-controls' $size
                }
                Shot ($viewport.name+'-messages.png')
                $script:stage=$viewport.name+' audit pagination'; Navigate '/console?view=audit'
                WaitJs '!!document.querySelector(".audit-panel .pagination-controls") && !document.querySelector(".audit-panel .pagination-controls[aria-busy=true]")' 20
                foreach($size in @(10,20,50,100)) {
                    Set-VerifiedPageSize '.audit-panel .pagination-controls' $size
                }
                if($VerifyAuditNavigation) {
                    Set-VerifiedPageSize '.audit-panel .pagination-controls' 10
                    Js '(()=>{window.__r3AuditJumpStart=performance.now();const n=document.querySelector(".audit-panel .pagination-controls input");n.value="2";n.dispatchEvent(new Event("input",{bubbles:true}));return true})()'|Out-Null
                    Js 'document.querySelector(".audit-panel .pagination-controls button[aria-label=跳转到指定页]").click();true'|Out-Null
                    WaitJs 'performance.getEntriesByType("resource").some(r=>{const u=new URL(r.name);return r.startTime>=window.__r3AuditJumpStart&&u.pathname==="/api/audit-logs"&&u.searchParams.get("page")==="2"&&u.searchParams.get("page_size")==="10"&&r.responseStatus===200})' 20
                    WaitJs 'document.querySelector(".audit-panel .pagination-position").textContent.trim().startsWith("2 /")&&!document.querySelector(".audit-panel .pagination-controls[aria-busy=true]")' 20
                    Check '!!document.querySelector(".audit-table tbody tr")' 'FE004 audit valid second page has rows and HTTP 200'
                    Js 'document.querySelector(".audit-panel .pagination-controls").scrollIntoView({block:"center"});true'|Out-Null
                    Shot ($viewport.name+'-audit-page2.png')
                    Js '(()=>{window.__r3AuditFilterStart=performance.now();const n=document.querySelector(".audit-filter-grid input");n.value="qa-r3-fe-readonly-no-match-filter";n.dispatchEvent(new Event("input",{bubbles:true}));return true})()'|Out-Null
                    Js 'document.querySelector(".audit-panel form").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));true'|Out-Null
                    WaitJs 'performance.getEntriesByType("resource").some(r=>{const u=new URL(r.name);return r.startTime>=window.__r3AuditFilterStart&&u.pathname==="/api/audit-logs"&&u.searchParams.get("page")==="1"&&u.searchParams.get("q")==="qa-r3-fe-readonly-no-match-filter"&&r.responseStatus===200})' 20
                    WaitJs '!document.querySelector(".audit-panel .pagination-controls[aria-busy=true]")' 20
                    Check 'document.querySelector(".audit-panel .pagination-position").textContent.trim().startsWith("1 /")' 'FE004 audit filter resets page to one with HTTP 200'
                    Js '[...document.querySelectorAll(".audit-panel button")].find(n=>n.textContent.trim()==="清空条件").click();true'|Out-Null
                    WaitJs '!!document.querySelector(".audit-table tbody tr")&&!document.querySelector(".audit-panel .pagination-controls[aria-busy=true]")' 20
                }
                if(Js '!!document.querySelector(".audit-table tbody tr")') {
                    Js 'document.querySelector(".audit-table tbody tr").click();true'|Out-Null
                    WaitJs '!!document.querySelector(".audit-drawer .pagination-controls") && !document.querySelector(".audit-drawer .pagination-controls[aria-busy=true]")' 15
                    foreach($size in @(10,20,50,100)) {
                        Set-VerifiedPageSize '.audit-drawer .pagination-controls' $size
                    }
                    Shot ($viewport.name+'-timeline.png')
                    $result.checks+='FE004 audit list and timeline size selections'
                } else {$result.gaps+='FE004 timeline: no audit event to open'}
                $result.gaps+='FE004 ledger report and valid page jumps require an isolated populated report'
            }
            if($PaginationOnly){continue}
            if($Actor -cin @('Admin','Provider')) {
              if($Actor -ceq 'Provider') {
                $script:stage=$viewport.name+' provider menu'; Navigate ('/console?view=products'+$suffix)
                WaitJs '!!document.querySelector("select[aria-label=业务身份]") && !document.querySelector(".loading-line")' 20
                Js '(()=>{const n=document.querySelector("select[aria-label=业务身份]");n.value="provider";n.dispatchEvent(new Event("change",{bubbles:true}));return true})()'|Out-Null
                WaitJs 'document.querySelector("select[aria-label=业务身份]").value==="provider"' 10
                Check '![...document.querySelectorAll(".sidebar button")].some(b=>b.textContent.includes("我的应用")) && parseFloat(getComputedStyle(document.querySelector("select[aria-label=业务身份]")).fontSize)<=13' 'FE002 provider excludes MyApps and compact identity'
                $result.identity_typography=Js 'JSON.stringify([document.querySelector("select[aria-label=业务身份]"),document.querySelector(".sidebar button")].map(n=>{const s=getComputedStyle(n);return {fontFamily:s.fontFamily,fontSize:s.fontSize,fontWeight:s.fontWeight}}))'
                Check '(()=>{const n=document.querySelector("select[aria-label=业务身份]"),a=getComputedStyle(n),b=getComputedStyle(document.querySelector(".sidebar button"));return a.fontFamily===b.fontFamily&&parseFloat(a.fontSize)<=parseFloat(b.fontSize)&&n.getBoundingClientRect().width>=78})()' 'FE002 identity uses coordinated compact font and full-label minimum width'
                Check '(()=>{const n=document.querySelector("select[aria-label=业务身份]").parentElement.querySelector(".sr-only"),s=getComputedStyle(n),r=n.getBoundingClientRect();return s.position==="absolute"&&s.overflow==="hidden"&&s.clipPath!=="none"&&r.width<=1&&r.height<=1})()' 'FE002 identity auxiliary label visually hidden'
                Check '(()=>{const n=document.querySelector("select[aria-label=业务身份]"),s=getComputedStyle(n),r=n.getBoundingClientRect(),p=n.parentElement.getBoundingClientRect();return s.fontSize==="12px"&&r.width>=78&&r.left>=0&&r.right<=innerWidth&&p.left>=0&&p.right<=innerWidth})()' 'FE002 identity 12px minimum 78px and control bounds'
                Check '(()=>{const n=document.querySelector("select[aria-label=业务身份]"),s=getComputedStyle(n),c=document.createElement("canvas").getContext("2d");c.font=s.fontWeight+" "+s.fontSize+" "+s.fontFamily;const room=n.clientWidth-parseFloat(s.paddingLeft)-parseFloat(s.paddingRight)-22;return ["提供方","购买方"].every(t=>c.measureText(t).width<=room)})()' 'FE002 both identity labels fit with native arrow allowance'
                Shot ($viewport.name+'-provider-identity.png')
                if($MenusOnly) {
                    Js '(()=>{const n=document.querySelector("select[aria-label=业务身份]");n.value="buyer";n.dispatchEvent(new Event("change",{bubbles:true}));return true})()'|Out-Null
                    WaitJs 'document.querySelector("select[aria-label=业务身份]")?.value==="buyer"&&!document.querySelector(".loading-line")' 15
                    Check 'document.querySelector("select[aria-label=业务身份]").selectedOptions[0].textContent==="购买方"' 'FE002 buyer identity label selected'
                    Shot ($viewport.name+'-buyer-identity.png')
                }
                if($MenusOnly){continue}
              }
                $script:stage=$viewport.name+' product registration entry'
                Navigate ('/console?view=products'+$suffix)
                WaitJs '[...document.querySelectorAll("button")].some(b=>b.textContent.trim()==="新建产品"&&!b.disabled) && !document.querySelector(".loading-line")' 25
                Js '[...document.querySelectorAll("button")].find(b=>b.textContent.trim()==="新建产品").click();true'|Out-Null
                WaitJs '!!document.querySelector(".product-modal")' 10
                $script:stage=$viewport.name+' registration'
                Check '(()=>{const labels=[...document.querySelectorAll(".product-modal label")],t=labels.find(n=>n.textContent.trim().startsWith("产品类型")).querySelector("select");return ![...t.options].some(o=>["report","application"].includes(o.value))})()' 'FE003 retired registration options absent'
                foreach($mapping in @(@('dataset','file'),@('model','file'),@('api','api'),@('saas','tenant_access'),@('training','training'),@('consulting','consulting'),@('custom','custom'))) {
                    $script:stage=$viewport.name+' delivery mapping '+$mapping[0]
                    Js "(()=>{const n=[...document.querySelectorAll('.product-modal label')].find(n=>n.textContent.trim().startsWith('产品类型')).querySelector('select');n.value='$($mapping[0])';n.dispatchEvent(new Event('change',{bubbles:true}));return true})()"|Out-Null
                    WaitJs "(()=>{const n=[...document.querySelectorAll('.product-modal label')].find(n=>n.textContent.trim().startsWith('交付方式')).querySelector('select');return n.disabled&&n.value==='$($mapping[1])'})()" 5
                    $result.checks+='FE003 fixed readonly delivery '+$mapping[0]+' -> '+$mapping[1]
                    Write-Output ('passed: FE003 delivery '+$mapping[0])
                }
                $script:stage=$viewport.name+' unsaved editor dismissal'
                Js '(()=>{const n=document.querySelector(".product-modal input");n.value="qa-r3-fe-unsaved-local-only";n.dispatchEvent(new Event("input",{bubbles:true}));document.querySelector(".product-modal").parentElement.click();return true})()'|Out-Null
                Js 'document.querySelector(".product-modal input").focus();true'|Out-Null
                Cdp 'Input.dispatchKeyEvent' @{type='keyDown';key='Escape';code='Escape';windowsVirtualKeyCode=27}|Out-Null
                Check '!!document.querySelector(".product-modal") && document.querySelector(".product-modal input").value==="qa-r3-fe-unsaved-local-only"' 'FE003 backdrop and Escape retain unsaved editor'
                Js 'document.querySelector(".product-modal button[aria-label=关闭产品登记]").click();true'|Out-Null
                WaitJs '!!document.querySelector(".action-dialog")' 5
                Js 'document.querySelector(".action-dialog .secondary-btn").click();true'|Out-Null
                WaitJs '!document.querySelector(".action-dialog") && !!document.querySelector(".product-modal")' 5
                Check 'document.querySelector(".product-modal input").value==="qa-r3-fe-unsaved-local-only"' 'FE003 cancel discard preserves editor'
                Shot ($viewport.name+'-registration.png')
                Js 'document.querySelector(".product-modal button[aria-label=关闭产品登记]").click();true'|Out-Null
                WaitJs '!!document.querySelector(".action-dialog")' 5
                Js 'document.querySelector(".action-dialog button[type=submit]").click();true'|Out-Null
                WaitJs '!document.querySelector(".product-modal")' 5
                $result.gaps+='FE003 save failure/single submission not tested: GET-only driver does not save products'
            }
          }
            if(($CheckSlaReason -or $SlaOnly) -and $Actor -cin @('Admin','Operator')) {
                $script:stage=$viewport.name+' SLA assignment reason'
                Navigate '/console?view=sla'
                WaitJs '!!document.querySelector(".service-level-assignment textarea") && !document.querySelector(".loading-line")' 25
                Check-EntryAsset
                if($CheckStrategicResponse) {
                    WaitJs '[...document.querySelectorAll(".service-level-card")].some(n=>n.textContent.includes("战略"))' 15
                    Check '(()=>{const n=[...document.querySelectorAll(".service-level-card")].find(n=>n.textContent.includes("战略"));return !!n&&/普通首响\s*720\s*分钟（支持时间）/.test(n.textContent)&&/紧急首响\s*360\s*分钟（自然时间）/.test(n.textContent)})()' 'SLA strategic normal 720 support minutes and urgent 360 natural minutes'
                    Js '[...document.querySelectorAll(".service-level-card")].find(n=>n.textContent.includes("战略")).scrollIntoView({block:"center"});true'|Out-Null
                    Shot ($viewport.name+'-strategic-response.png')
                }
                Check '(()=>{const n=document.querySelector(".service-level-assignment textarea");return n.required&&n.maxLength===2000&&!n.disabled})()' 'SLA reason required and maxlength 2000'
                Check 'document.querySelector(".service-level-assignment button").disabled' 'SLA empty reason blocks assignment'
                Js '(()=>{const n=document.querySelector(".service-level-assignment textarea");n.value="   ";n.dispatchEvent(new Event("input",{bubbles:true}));return true})()'|Out-Null
                WaitJs 'document.querySelector(".service-level-assignment button").disabled' 5
                Check 'document.querySelector(".service-level-assignment button").disabled' 'SLA whitespace-only reason blocks assignment'
                Js '(()=>{const n=document.querySelector(".service-level-assignment textarea");n.value="qa-r3-fe-readonly-reason-not-submitted";n.dispatchEvent(new Event("input",{bubbles:true}));return true})()'|Out-Null
                WaitJs '!document.querySelector(".service-level-assignment button").disabled' 5
                Check '!document.querySelector(".service-level-assignment button").disabled' 'SLA nonblank reason enables action without submitting'
                Js 'document.querySelector(".service-level-assignment").scrollIntoView({block:"center"});new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))'|Out-Null
                Check '(()=>{const n=document.querySelector(".service-level-assignment textarea"),r=n.getBoundingClientRect();return r.width>0&&r.left>=-1&&r.right<=innerWidth+1})()' 'SLA reason field fits viewport'
                Shot ($viewport.name+'-sla-reason.png')
                Js '(()=>{const n=document.querySelector(".service-level-assignment textarea");n.value="";n.dispatchEvent(new Event("input",{bubbles:true}));return true})()'|Out-Null
            }
            if($Actor -ceq 'Member') {
                $script:stage=$viewport.name+' granted member applications'; Navigate ('/console?view=applications'+$suffix)
                WaitJs '!!document.querySelector(".applications-center") && !document.querySelector(".applications-center [role=status]")' 20
                Check '!document.querySelector(".application-members-modal") && ![...document.querySelectorAll(".application-actions button")].some(b=>b.textContent.includes("成员授权"))' 'FE005 ordinary member has no grant management'
                $enterpriseJson=ConvertTo-Json -InputObject $EnterpriseId -Compress
                Check "performance.getEntriesByType('resource').some(r=>{const u=new URL(r.name);return u.pathname==='/api/applications'&&u.searchParams.get('enterprise_id')===$enterpriseJson})" 'FE005 application list uses current enterprise query'
                Check '!!document.querySelector(".application-actions .primary-btn:not(:disabled)")' 'FE005 granted member has enabled SSO entry'
                Shot ($viewport.name+'-applications.png')
                $result.gaps+='FE005 actual OIDC/RP login and revocation/expiry/disabled negatives require owned SaaS fixture; launch is blocked in read-only mode'
            }
        }
    }
    if($script:blocked -or $script:publicAuth -or $script:nativeDialogs){throw 'Request or native-dialog guard failure'}
    $result.status='passed'
    if($Actor -ceq 'Public'){
        if(-not $LocalEmptyFix){$result.tasks.FE001='passed'}
        $result.tasks.FE004='partial';$result.gaps+=@('FE002/FE003/FE005 require an authenticated isolated actor','FE004 other four surfaces and valid second page not covered')
    }
    if($Actor -cne 'Public'){$result.tasks.FE002='partial'}
    if($Actor -cin @('Admin','Operator') -and -not $FormsOnly){$result.tasks.FE004='partial'}
    if($Actor -cin @('Admin','Provider')){$result.tasks.FE003='partial'}
    if($Actor -ceq 'Member'){$result.tasks.FE005='partial'}
} catch {
    $result.status='failed'; $result.failure='Browser check failed; raw private diagnostics suppressed'
    try {$result.session_wait_state=Js 'JSON.stringify({path:location.pathname,view:new URLSearchParams(location.search).get("view"),token_present:!!localStorage.getItem("market_token"),injected:window.__r3QaTokenInstalled===true,sidebar:!!document.querySelector(".sidebar"),login:!!document.querySelector("input[type=password]")})'} catch {}
    try {$result.pagination_wait_state=Js 'JSON.stringify([...document.querySelectorAll(".pagination-controls")].map(n=>({busy:n.getAttribute("aria-busy"),size:n.querySelector("select")?.value,disabled:n.querySelector("select")?.disabled})))'} catch {}
    if($script:stage -like '*SLA assignment reason') {
        try {
            $result.sla_wait_state=Js 'JSON.stringify({view:new URLSearchParams(location.search).get("view"),ready:document.readyState,loading:!!document.querySelector(".loading-line"),assignment:!!document.querySelector(".service-level-assignment"),textarea:!!document.querySelector(".service-level-assignment textarea"),sidebar:!!document.querySelector(".sidebar"),login:!!document.querySelector("input[type=password]")})'
            Shot 'sla-wait-failure.png'
        } catch {}
    }
} finally {
    $result.network_status=$script:networkStatus; $result.network_failures=$script:networkFailures; $result.console_types=$script:consoleTypes
    $result.stage=$script:stage; $result.blocked=$script:blocked; $result.public_auth=$script:publicAuth; $result.native_dialogs=$script:nativeDialogs
    $result.captured_at=[DateTime]::UtcNow.ToString('o')
    if($script:socket){$script:socket.Dispose()}
    if($script:chrome -and -not $script:chrome.HasExited){Stop-Process -Id $script:chrome.Id -Force -ErrorAction SilentlyContinue}
    if(Test-Path $profile){Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue}
    $token=$null; $quoted=$null
    if(Test-Path $OutputDir){$result|ConvertTo-Json -Depth 8|Set-Content -LiteralPath (Join-Path $OutputDir 'result.json') -Encoding UTF8}
}
if($result.status -cne 'passed'){throw 'Browser acceptance failed; see sanitized result.json'}
Write-Output ('Passed bounded '+$Actor+' checks; see result.json for per-task gaps')
