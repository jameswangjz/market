$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'enterprise_trade_browser_cdp.ps1') -FixtureFile 'unused-offline' -GuardOnly
$BaseUrl='http://fixture.invalid'
$script:fixture=[pscustomobject]@{product_id='owned-p';product_version_id='owned-v';enterprise_id='owned-e';actors=[pscustomobject]@{super=[pscustomobject]@{user_id='owned-u';email='owned@example.invalid';password='owned-password'};admin=[pscustomobject]@{user_id='owned-admin'}}}
$script:actorRole='super';$script:orderId='';$script:orderPosts=0;$script:subscriptionId=''
function Assert-Guard($expected,$method,$url,$body='') {
    $actual=Allowed-Request $method $url $body
    if($actual -ne $expected){throw "Offline request guard mismatch: $method $url"}
}
$purchase='{"product_id":"owned-p","product_version_id":"owned-v","buyer_enterprise_id":"owned-e","subscription_months":1}'
$script:quoteId='b'*64
$orderPurchase=$purchase.Replace('}',(',"quote_id":"'+$script:quoteId+'"}'))
Assert-Guard $false 'POST' 'http://external.invalid/api/orders' $purchase
Assert-Guard $false 'GET' "$BaseUrl/api/orders/ORD-261012005144E1B3"
Assert-Guard $false 'GET' "$BaseUrl/api/storefront/products/qa-vcq-a044ea9ea181-p2"
Assert-Guard $false 'POST' "$BaseUrl/api/orders" ($purchase.Replace('owned-v','foreign-v'))
Assert-Guard $false 'POST' "$BaseUrl/api/orders" ($purchase.Replace('}',',"extra":true}'))
Assert-Guard $false 'POST' "$BaseUrl/api/orders" $purchase
Assert-Guard $false 'POST' "$BaseUrl/api/orders" ($orderPurchase.Replace('owned-p','foreign-p'))
Assert-Guard $false 'POST' "$BaseUrl/api/orders" ($orderPurchase.Replace('owned-v','foreign-v'))
Assert-Guard $false 'POST' "$BaseUrl/api/orders" ($orderPurchase.Replace('owned-e','foreign-e'))
Assert-Guard $false 'POST' "$BaseUrl/api/orders" ($orderPurchase.Replace(('b'*64),('c'*64)))
Assert-Guard $false 'POST' "$BaseUrl/api/orders" ($orderPurchase.Replace('}',',"idempotency_key":"unknown"}'))
Assert-Guard $true 'POST' "$BaseUrl/api/orders" $orderPurchase
Assert-Guard $false 'POST' "$BaseUrl/api/orders" $orderPurchase
$script:orderId='a'*32
Assert-Guard $false 'GET' "$BaseUrl/api/orders/$($script:orderId)/api-credentials/anything/download"
Assert-Guard $false 'POST' "$BaseUrl/api/orders/$($script:orderId)/transition" '{"action":"cancel"}'
$script:actorRole='admin'
$script:orderId=''
Assert-Guard $true 'POST' "$BaseUrl/api/orders/quote" $purchase
Assert-Guard $false 'POST' "$BaseUrl/api/orders" $purchase
$script:orderId='a'*32
Assert-Guard $false 'POST' "$BaseUrl/api/orders/$($script:orderId)/transition" '{"action":"start_payment"}'
$script:actorRole='super'
Assert-Guard $true 'POST' "$BaseUrl/api/orders/$($script:orderId)/transition" '{"action":"start_payment"}'
Assert-Guard $true 'POST' "$BaseUrl/api/orders/$($script:orderId)/transition" '{"action":"confirm_payment"}'
Assert-Guard $false 'GET' "$BaseUrl/api/orders?enterprise_id=foreign"
Assert-Guard $false 'GET' "$BaseUrl/api/admin/settings"
Assert-Guard $false 'PUT' "$BaseUrl/api/products/owned-p"
Start-BuyerNavigation
foreach($path in @('summary','stream','sender-context')) {
    Assert-Guard $true 'GET' "$BaseUrl/api/notifications/${path}?enterprise_id=owned-e&business_view=provider"
    Assert-Guard $false 'GET' "$BaseUrl/api/notifications/${path}?enterprise_id=foreign&business_view=buyer"
    Assert-Guard $false 'GET' "$BaseUrl/api/notifications/${path}?business_view=buyer"
    Assert-Guard $false 'GET' "$BaseUrl/api/notifications/${path}?enterprise_id=owned-e&enterprise_id=foreign"
}
$script:startupNotificationDeadline=[DateTime]::UtcNow.AddSeconds(-1)
Assert-Guard $false 'GET' "$BaseUrl/api/notifications/stream?enterprise_id=owned-e&business_view=provider"
Start-BuyerNavigation
Assert-Guard $true 'GET' "$BaseUrl/api/notifications/stream?enterprise_id=owned-e&business_view=provider"
$script:startupNotifications=$false
Assert-Guard $false 'GET' "$BaseUrl/api/notifications/stream?enterprise_id=owned-e&business_view=provider"
Assert-Guard $true 'GET' "$BaseUrl/api/notifications/stream?enterprise_id=owned-e&business_view=buyer"
Assert-Guard $true 'GET' "$BaseUrl/api/orders/$($script:orderId)/version-changes"
Assert-Guard $false 'GET' "$BaseUrl/api/orders/foreign-order/version-changes"
Assert-Guard $false 'GET' "$BaseUrl/api/orders/$($script:orderId)/version-changes?extra=1"
$ownedPayload=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('{"sub":"owned-u"}')).TrimEnd('=').Replace('+','-').Replace('/','_')
$foreignPayload=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('{"sub":"foreign"}')).TrimEnd('=').Replace('+','-').Replace('/','_')
foreach($path in @('stream','sender-context')) {
    if(-not (Allowed-Request 'GET' "$BaseUrl/api/notifications/$path" '' ([pscustomobject]@{Authorization="Bearer e30.$ownedPayload.signature"}))){throw 'Owned personal notification read denied'}
    if(Allowed-Request 'GET' "$BaseUrl/api/notifications/$path" '' ([pscustomobject]@{Authorization="Bearer e30.$foreignPayload.signature"})){throw 'Foreign personal notification read allowed'}
    Assert-Guard $false 'GET' "$BaseUrl/api/notifications/$path"
}
$script:deniedRequests=@()
Record-DeniedRequest 'GET' 'http://private-user:private-password@fixture.invalid/api/notifications/stream?enterprise_id=PRIVATE_VALUE&business_view=OTHER_PRIVATE#PRIVATE_FRAGMENT'
$diagnostic=$script:deniedRequests[0]
if($diagnostic.method -cne 'GET' -or $diagnostic.path -cne '/api/notifications/stream' -or
   ($diagnostic.queryKeys -join ',') -cne 'enterprise_id,business_view'){throw 'Denied diagnostic fields differ'}
$serialized=$script:deniedRequests|ConvertTo-Json -Depth 6 -Compress
if($serialized -match 'PRIVATE|private-user|private-password|fixture.invalid'){throw 'Denied diagnostic leaked URI details'}
Record-DeniedRequest 'GET' 'not-an-absolute-url'
if($script:deniedRequests[1].path -cne 'invalid-url'){throw 'Invalid URL diagnostic differs'}
Record-DeniedRequest 'GET' ('http://fixture.invalid/api/orders?'+('x'*80)+'=PRIVATE_VALUE')
if($script:deniedRequests[2].queryKeys[0] -cne '[redacted-key]'){throw 'Unbounded diagnostic query key'}
for($i=0;$i -lt 40;$i++){Record-DeniedRequest 'GET' 'http://fixture.invalid/api/denied'}
if($script:deniedRequests.Count -ne 32){throw 'Denied diagnostic limit differs'}
$cdpFunction=(Get-Command Cdp).ScriptBlock.ToString()
if(-not $cdpFunction.Contains('Record-DeniedRequest $request.method $request.url')){throw 'CDP interception denial diagnostic missing'}
# Execute installer with offline CDP stubs and verify its real registration call.
$script:installedMethod='';$script:installedSource=''
function Cdp($method,$parameters) {$script:installedMethod=$method;$script:installedSource=$parameters.source}
function Js($source) {if($source -cne $script:installedSource){throw 'Observer current-document source differs'};return $true}
Install-OwnedOrderObserver
if($script:installedMethod -cne 'Page.addScriptToEvaluateOnNewDocument' -or -not $script:orderObserverInstalled){throw 'Observer registration missing'}
$driver=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'enterprise_trade_browser_cdp.ps1'))
$install=$driver.IndexOf('    Install-OwnedOrderObserver')
$purchaseClick=$driver.IndexOf("    Js 'document.querySelector(`".store-version-tool button.primary-btn.full-btn`").click();true'")
if($install -lt 0 -or $purchaseClick -lt $install){throw 'Observer must be installed before purchase'}
if($driver.Contains('market_pfirst_order')){throw 'Driver still relies on unfiltered legacy storage'}
function Cdp($method,$parameters) {
    if(-not $script:startupNotifications -or $script:startupNotificationDeadline -le [DateTime]::UtcNow){throw 'Console navigation did not open bounded startup window'}
}
function WaitJs($expression,$seconds) {
    if(-not $expression.Contains('.console-buyer-context #buyer-enterprise')){throw 'Buyer context proof missing'}
}
$script:startupNotifications=$false
Navigate-BuyerConsole
Finish-BuyerNavigation
Assert-Guard $false 'GET' "$BaseUrl/api/notifications/stream?enterprise_id=owned-e&business_view=provider"
Write-Output 'passed: offline request guards; no Chrome or network execution'
