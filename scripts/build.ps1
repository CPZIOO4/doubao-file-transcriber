[CmdletBinding()]
param(
    [string]$InnoCompiler,
    [switch]$SkipInstaller
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$projectRoot = Split-Path -Parent $PSScriptRoot
$cacheRoot = Join-Path $projectRoot '.deps'
$buildRoot = Join-Path $projectRoot '.build'
$distRoot = Join-Path $projectRoot 'dist'
foreach ($directory in @($cacheRoot, $buildRoot, $distRoot)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'dependencies.json') -Raw | ConvertFrom-Json
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-VerifiedDependency($dependency) {
    $destination = Join-Path $cacheRoot $dependency.filename
    if (-not (Test-Path -LiteralPath $destination)) {
        Write-Host ('Downloading ' + $dependency.filename)
        Invoke-WebRequest -UseBasicParsing -Uri $dependency.url -OutFile $destination
    }
    $actual = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
    if ($actual -ne $dependency.sha256) {
        throw ('SHA-256 mismatch: ' + $dependency.filename + '. Remove that cache file and retry.')
    }
    return $destination
}

$pythonZip = Get-VerifiedDependency $manifest.python
$naudioZip = Get-VerifiedDependency $manifest.naudio
$stageRoot = Join-Path $buildRoot ('package-' + [Guid]::NewGuid().ToString('N'))
foreach ($subdirectory in @('app','runtime','licenses')) {
    New-Item -ItemType Directory -Path (Join-Path $stageRoot $subdirectory) -Force | Out-Null
}
[IO.Compression.ZipFile]::ExtractToDirectory($pythonZip, (Join-Path $stageRoot 'runtime'))
$naudioRoot = Join-Path $buildRoot ('naudio-' + [Guid]::NewGuid().ToString('N'))
[IO.Compression.ZipFile]::ExtractToDirectory($naudioZip, $naudioRoot)
Copy-Item -LiteralPath (Join-Path $naudioRoot 'lib\net35\NAudio.dll') -Destination (Join-Path $stageRoot 'app')
$utf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $stageRoot 'runtime\python314._pth'), "python314.zip`n.`n../app`n", $utf8)
foreach ($name in @('compatibility.py','native_worker.py','doubao_transcribe.py')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot ('src\' + $name)) -Destination (Join-Path $stageRoot 'app')
}
Get-ChildItem -LiteralPath (Join-Path $projectRoot 'licenses') -File | Copy-Item -Destination (Join-Path $stageRoot 'licenses')
Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination (Join-Path $stageRoot 'licenses\project-MIT.txt')
Get-ChildItem -LiteralPath (Join-Path $projectRoot 'packaging') -Filter '*.txt' -File | Copy-Item -Destination $stageRoot
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'The Windows .NET Framework x64 C# compiler is required.' }
& $compiler /nologo /target:exe /platform:x64 /codepage:65001 "/out:$stageRoot\app\AudioDecode.exe" "/reference:$stageRoot\app\NAudio.dll" (Join-Path $projectRoot 'src\AudioDecode.cs')
if ($LASTEXITCODE -ne 0) { throw 'Audio decoder build failed.' }
& $compiler /nologo /target:winexe /platform:x64 /codepage:65001 "/out:$stageRoot\豆包录音转写.exe" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll (Join-Path $projectRoot 'src\TranscriberApp.cs')
if ($LASTEXITCODE -ne 0) { throw 'Desktop app build failed.' }

& (Join-Path $stageRoot 'runtime\python.exe') -I -B -m unittest discover -s (Join-Path $projectRoot 'tests') -v
if ($LASTEXITCODE -ne 0) { throw 'Offline regression tests failed.' }

if (-not $SkipInstaller) {
    if (-not $InnoCompiler) {
        $innoRoot = Join-Path $cacheRoot 'inno-6.7.3'
        $InnoCompiler = Join-Path $innoRoot 'ISCC.exe'
        if (-not (Test-Path -LiteralPath $InnoCompiler)) {
            $innoInstaller = Get-VerifiedDependency $manifest.inno
            $installerProcess = Start-Process -FilePath $innoInstaller -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CURRENTUSER','/SP-','/NOICONS',('/DIR="' + $innoRoot + '"')) -WindowStyle Hidden -Wait -PassThru
            if ($installerProcess.ExitCode -ne 0) { throw 'Inno Setup installation failed.' }
        }
    }
    & $InnoCompiler /Qp "/DPackageDir=$stageRoot" "/DReleaseDir=$distRoot" (Join-Path $projectRoot 'src\installer.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
    $installerPath = Join-Path $distRoot 'doubao-file-transcriber-1.2.0-preview.1-windows-x64-setup.exe'
    $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $installerPath).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText((Join-Path $distRoot 'SHA256SUMS.txt'), ($digest + '  ' + [IO.Path]::GetFileName($installerPath) + "`n"), $utf8)
    Write-Host ('Installer: ' + $installerPath)
}
[IO.File]::WriteAllText((Join-Path $buildRoot 'last-package.txt'), $stageRoot, $utf8)
Write-Host ('Portable package: ' + $stageRoot)
