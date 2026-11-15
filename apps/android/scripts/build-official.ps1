param(
    [Parameter(Mandatory=$true)][string]$SigningConfig,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string]$ReleaseTag = 'v0.9.17',
    [string]$VersionName = '0.8.1',
    [int]$VersionCode = 801,
    [string]$Gradle = 'gradle',
    [string]$AndroidSdk = $env:ANDROID_SDK_ROOT
)
$ErrorActionPreference = 'Stop'
if ($VersionName -notmatch '^\d{1,5}\.\d{1,5}\.\d{1,5}$' -or $ReleaseTag -notmatch '^(android-)?v\d{1,5}\.\d{1,5}\.\d{1,5}$') { throw 'Invalid release version/tag.' }
if ($VersionCode -lt 1) { throw 'Invalid release code.' }
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskSecret = Get-Content -LiteralPath $SigningConfig -Raw | ConvertFrom-Json
try {
    $env:KMVMS_ANDROID_KEYSTORE = $taskSecret.keystore
    $env:KMVMS_ANDROID_STORE_PASSWORD = $taskSecret.password
    if (!(Test-Path -LiteralPath $env:KMVMS_ANDROID_KEYSTORE)) { throw 'Official keystore missing.' }
    Push-Location $taskRoot
    try {
        & $Gradle --offline --console=plain "-Pkmvms.versionName=$VersionName" "-Pkmvms.versionCode=$VersionCode" :core:test :app:testReleaseUnitTest :app:assembleRelease
        if ($LASTEXITCODE -ne 0) { throw 'Official Android build failed.' }
    } finally { Pop-Location }
    $taskApk = Join-Path $taskRoot 'app\build\outputs\apk\release\app-release.apk'
    $taskSigner = Join-Path $AndroidSdk 'build-tools\36.0.0\apksigner.bat'
    $taskInspection = & $taskSigner verify --verbose --print-certs $taskApk 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'APK signing verification failed.' }
    $taskCertificate = [regex]::Match(($taskInspection -join "`n"), 'Signer #1 certificate SHA-256 digest:\s*([a-fA-F0-9]{64})').Groups[1].Value.ToLowerInvariant()
    if (!$taskCertificate) { throw 'Signing certificate not found.' }
    $null = New-Item -ItemType Directory -Path $OutputDirectory -Force
    $taskDestination = Join-Path $OutputDirectory "km-vms-android-$VersionName.apk"
    Copy-Item -LiteralPath $taskApk -Destination $taskDestination
    $taskMetadata = [ordered]@{format=1;published=$true;package_name='ru.kmvms.mobile';version_name=$VersionName;version_code=$VersionCode;release_tag=$ReleaseTag;
        size_bytes=(Get-Item -LiteralPath $taskDestination).Length;sha256=(Get-FileHash -LiteralPath $taskDestination -Algorithm SHA256).Hash.ToLowerInvariant();certificate_sha256=$taskCertificate;min_android_sdk=26}
    [IO.File]::WriteAllText((Join-Path $OutputDirectory 'release-candidate.json'), ($taskMetadata|ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    Write-Output "Verified signed APK: $taskDestination"
    Write-Output 'Candidate metadata generated only in output directory. No Git tag, upload, or public metadata changed.'
} finally {
    Remove-Item Env:KMVMS_ANDROID_KEYSTORE -ErrorAction SilentlyContinue
    Remove-Item Env:KMVMS_ANDROID_STORE_PASSWORD -ErrorAction SilentlyContinue
    $taskSecret=$null; $taskInspection=$null
}
