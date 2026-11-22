param(
    [Parameter(Mandatory=$true)][string]$MguIp,
    [Parameter(Mandatory=$true)][string]$Key,
    [Parameter(Mandatory=$true)][string]$RedirectUri,
    [Parameter(Mandatory=$true)][string]$ClientId,
    [string]$KnownHostsFile='',
    [string]$SshPath='ssh.exe',
    [switch]$ManualReturn
)
$ErrorActionPreference='Stop'
$address=$null
if (-not [Net.IPAddress]::TryParse($MguIp,[ref]$address) -or $address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'Use the IPv4 address of your MGU.' }
if (-not (Test-Path -LiteralPath $Key -PathType Leaf)) { throw 'SSH private key not found.' }
if ($ClientId -notmatch '^[1-9][0-9]{0,14}$') { throw 'Invalid VK client_id.' }
$uri=$null
if (-not [Uri]::TryCreate($RedirectUri,[UriKind]::Absolute,[ref]$uri) -or $uri.UserInfo -or $uri.Fragment -or $uri.Query) { throw 'Use the exact registered Redirect URI.' }
if ($uri.Scheme -ne 'https' -and -not ($uri.Scheme -eq 'http' -and $uri.Host -in @('localhost','127.0.0.1'))) { throw 'Redirect URI must use HTTPS (HTTP is permitted only for a local test).' }
$ssh=(Get-Command $SshPath -ErrorAction Stop).Source
$scp=Join-Path (Split-Path -Parent $ssh) 'scp.exe'
if (-not (Test-Path -LiteralPath $scp -PathType Leaf)) { throw 'scp.exe is required next to the selected ssh.exe.' }
$options=@('-i',$Key,'-o','StrictHostKeyChecking=yes','-o','UpdateHostKeys=no','-o','ConnectTimeout=10')
if ($KnownHostsFile) {
    if (-not (Test-Path -LiteralPath $KnownHostsFile -PathType Leaf)) { throw 'Known-hosts file not found.' }
    $options+=@('-o',('UserKnownHostsFile='+$KnownHostsFile))
}
$remote='/tmp/go-video-oauth-'+[Guid]::NewGuid().ToString('N')+'.json'
$local=[IO.Path]::GetTempFileName()
try {
    $config=@{client_id=$ClientId;redirect_uri=$RedirectUri;scope='video';callback_page=(-not $ManualReturn.IsPresent)} | ConvertTo-Json
    [IO.File]::WriteAllText($local,$config,[Text.UTF8Encoding]::new($false))
    Write-Host 'Checking the existing trusted SSH connection...'
    & $ssh @options "root@$MguIp" 'id'
    if ($LASTEXITCODE -ne 0) { throw 'SSH verification failed. Confirm the host key in MGU Manager first, and pass that profile known-hosts file if needed.' }
    & $scp -O @options $local "root@${MguIp}:$remote"
    if ($LASTEXITCODE -ne 0) { throw 'Configuration upload failed.' }
    & $ssh @options "root@$MguIp" "set -e; mkdir -p /var/opt/st-govideo-d/state; chmod 700 /var/opt/st-govideo-d/state; cp $remote /var/opt/st-govideo-d/state/oauth-config.json; chmod 600 /var/opt/st-govideo-d/state/oauth-config.json; rm -f $remote"
    if ($LASTEXITCODE -ne 0) { throw 'Configuration was not confirmed.' }
    Write-Host 'Public VK ID configuration saved. Open Go Video and choose Sign in. No firmware, coding, AWD or lights were changed.'
} finally {
    if (Test-Path -LiteralPath $local -PathType Leaf) { Remove-Item -LiteralPath $local -Force }
}
