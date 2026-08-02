[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SelfTest,
    [switch]$SkipWinget,
    [switch]$NoPause,
    [string]$PythonPath
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RequirementsDir = Join-Path $ProjectDir "requirements"
$DevelopmentRequirements = Join-Path $RequirementsDir "development.txt"
$NativeRequirements = Join-Path $RequirementsDir "native-wheels.txt"
$VerifyScript = Join-Path $PSScriptRoot "verify_environment.py"
$VenvDir = Join-Path $ProjectDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$SupportedVersions = @("3.12", "3.11")
$script:StepNumber = 0
$script:TotalSteps = 8
$script:TranscriptStarted = $false

function Write-Step([string]$Message) {
    $script:StepNumber += 1
    Write-Host ""
    Write-Host "[$($script:StepNumber)/$($script:TotalSteps)] $Message" -ForegroundColor Cyan
}

function Write-Info([string]$Message) {
    Write-Host "      [INFO] $Message"
}

function Write-Pass([string]$Message) {
    Write-Host "      [PASS] $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Host "      [WARN] $Message" -ForegroundColor Yellow
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Description) {
    Write-Info $Description
    Write-Host "      Command: $Executable $($Arguments -join ' ')" -ForegroundColor DarkGray
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Get-PythonInfo([string]$Executable, [string]$Source) {
    try {
        $probe = @"
import json, platform, struct, sys
print(json.dumps({
    'executable': sys.executable,
    'major_minor': f'{sys.version_info.major}.{sys.version_info.minor}',
    'full_version': platform.python_version(),
    'bits': struct.calcsize('P') * 8
}))
"@
        $output = & $Executable -c $probe 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $output) {
            return $null
        }
        $data = ($output | Select-Object -Last 1) | ConvertFrom-Json
        return [PSCustomObject]@{
            Executable = [string]$data.executable
            MajorMinor = [string]$data.major_minor
            FullVersion = [string]$data.full_version
            Bits = [int]$data.bits
            Source = $Source
        }
    }
    catch {
        return $null
    }
}

function Add-PythonCandidate([System.Collections.ArrayList]$Candidates, [hashtable]$Seen, $Candidate) {
    if ($null -eq $Candidate) {
        return
    }
    $key = $Candidate.Executable.ToLowerInvariant()
    if (-not $Seen.ContainsKey($key)) {
        $Seen[$key] = $true
        [void]$Candidates.Add($Candidate)
    }
}

function Find-PythonCandidates {
    $candidates = New-Object System.Collections.ArrayList
    $seen = @{}

    if ($PythonPath) {
        Add-PythonCandidate $candidates $seen (Get-PythonInfo $PythonPath "explicit -PythonPath")
    }
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        Add-PythonCandidate $candidates $seen (Get-PythonInfo $VenvPython "existing project .venv")
    }

    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($version in $SupportedVersions) {
            try {
                $resolved = & $pyLauncher.Source "-$version" -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $resolved) {
                    Add-PythonCandidate $candidates $seen (Get-PythonInfo ($resolved | Select-Object -Last 1) "Python launcher $version")
                }
            }
            catch {}
        }
    }

    foreach ($commandName in @("python.exe", "python3.exe")) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($command) {
            Add-PythonCandidate $candidates $seen (Get-PythonInfo $command.Source "PATH command $commandName")
        }
    }

    foreach ($version in $SupportedVersions) {
        $compact = $version.Replace(".", "")
        $standardPath = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$compact\python.exe"
        if (Test-Path -LiteralPath $standardPath -PathType Leaf) {
            Add-PythonCandidate $candidates $seen (Get-PythonInfo $standardPath "standard per-user install")
        }
    }
    return @($candidates)
}

function Select-CompatiblePython([object[]]$Candidates) {
    $compatible = @($Candidates | Where-Object {
        $_.Bits -eq 64 -and $SupportedVersions -contains $_.MajorMinor
    })
    if (-not $compatible) {
        return $null
    }

    $projectEnvironment = @($compatible | Where-Object { $_.Source -eq "existing project .venv" })
    if ($projectEnvironment) {
        return $projectEnvironment[0]
    }
    return $compatible | Sort-Object { [version]$_.FullVersion } -Descending | Select-Object -First 1
}

function Install-CompatiblePython {
    if ($SkipWinget) {
        throw "No compatible 64-bit Python was found and automatic installation was disabled with -SkipWinget."
    }
    $winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "No compatible Python was found and Windows Package Manager (winget) is unavailable. Install 64-bit Python 3.12, then run this setup again."
    }
    if ($DryRun) {
        Write-Info "DRY RUN: Would install 64-bit Python 3.12 for the current Windows user with winget."
        return
    }

    Invoke-Checked $winget.Source @(
        "install", "--exact", "--id", "Python.Python.3.12",
        "--scope", "user", "--silent",
        "--accept-package-agreements", "--accept-source-agreements"
    ) "Installing Python 3.12 with Windows Package Manager"
}

function Test-SelectionPolicy {
    Write-Host "Beyond Tournament setup policy self-test"

    function Assert-Equal([string]$Name, $Expected, $Actual) {
        $script:__testCount += 1
        if ($Expected -eq $Actual) {
            Write-Host "[PASS] $Name"
        }
        else {
            $script:__testFailures += 1
            Write-Host "[FAIL] $Name - expected '$Expected', received '$Actual'" -ForegroundColor Red
        }
    }

    $script:__testCount = 0
    $script:__testFailures = 0
    $newest = Select-CompatiblePython @(
        [PSCustomObject]@{Executable="C:\Python313\python.exe"; MajorMinor="3.13"; FullVersion="3.13.4"; Bits=64; Source="test"},
        [PSCustomObject]@{Executable="C:\Python312\python.exe"; MajorMinor="3.12"; FullVersion="3.12.10"; Bits=64; Source="test"},
        [PSCustomObject]@{Executable="C:\Python311\python.exe"; MajorMinor="3.11"; FullVersion="3.11.9"; Bits=64; Source="test"}
    )
    Assert-Equal "Newest compatible Python wins" "3.12" $newest.MajorMinor

    $fallback = Select-CompatiblePython @(
        [PSCustomObject]@{Executable="C:\Python313\python.exe"; MajorMinor="3.13"; FullVersion="3.13.4"; Bits=64; Source="test"},
        [PSCustomObject]@{Executable="C:\Python311\python.exe"; MajorMinor="3.11"; FullVersion="3.11.9"; Bits=64; Source="test"}
    )
    Assert-Equal "Unsupported newest version falls back safely" "3.11" $fallback.MajorMinor

    $none = Select-CompatiblePython @(
        [PSCustomObject]@{Executable="C:\Python313\python.exe"; MajorMinor="3.13"; FullVersion="3.13.4"; Bits=64; Source="test"},
        [PSCustomObject]@{Executable="C:\Python312-32\python.exe"; MajorMinor="3.12"; FullVersion="3.12.10"; Bits=32; Source="test"}
    )
    Assert-Equal "No compatible runtime requests automatic install" $null $none

    $existing = Select-CompatiblePython @(
        [PSCustomObject]@{Executable="C:\Python312\python.exe"; MajorMinor="3.12"; FullVersion="3.12.10"; Bits=64; Source="test"},
        [PSCustomObject]@{Executable="$VenvPython"; MajorMinor="3.11"; FullVersion="3.11.9"; Bits=64; Source="existing project .venv"}
    )
    Assert-Equal "Existing project environment is preserved" "existing project .venv" $existing.Source
    Assert-Equal "Development requirements exist" $true (Test-Path -LiteralPath $DevelopmentRequirements -PathType Leaf)
    Assert-Equal "Native wheel policy exists" $true (Test-Path -LiteralPath $NativeRequirements -PathType Leaf)
    Assert-Equal "Health check exists" $true (Test-Path -LiteralPath $VerifyScript -PathType Leaf)
    $setupSource = Get-Content -LiteralPath $PSCommandPath -Raw
    $buildSource = Get-Content -LiteralPath (Join-Path $ProjectDir "build.bat") -Raw
    Assert-Equal "Missing native wheels cannot fall back to C++ compilation" $true $setupSource.Contains('"--only-binary=:all:"')
    Assert-Equal "Nuitka build requests automatic MinGW64" $true $buildSource.Contains("--mingw64")

    Write-Host ""
    Write-Host "Self-test result: $($script:__testCount - $script:__testFailures)/$($script:__testCount) passed."
    if ($script:__testFailures -gt 0) {
        return 1
    }
    return 0
}

if ($SelfTest) {
    exit (Test-SelectionPolicy)
}

Set-Location $ProjectDir
$logDir = Join-Path $ProjectDir "setup_logs"
try {
    if (-not $DryRun) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        $logPath = Join-Path $logDir ("setup-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
        Start-Transcript -LiteralPath $logPath -Force | Out-Null
        $script:TranscriptStarted = $true
        Write-Info "Detailed log: $logPath"
    }

    Write-Step "Checking the project and operating system"
    Write-Info "Project directory: $ProjectDir"
    Write-Info "Operating system: $([Environment]::OSVersion.VersionString)"
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "Beyond Tournament development setup requires 64-bit Windows."
    }
    foreach ($requiredFile in @($DevelopmentRequirements, $NativeRequirements, $VerifyScript)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "Required setup file is missing: $requiredFile"
        }
    }
    Write-Pass "Required setup files and 64-bit Windows are available."

    Write-Step "Discovering installed Python runtimes"
    $candidates = @(Find-PythonCandidates)
    if ($candidates.Count -eq 0) {
        Write-Warn "No working Python executable was detected."
    }
    else {
        foreach ($candidate in $candidates) {
            $support = if ($candidate.Bits -eq 64 -and $SupportedVersions -contains $candidate.MajorMinor) { "compatible" } else { "not compatible" }
            Write-Info "Python $($candidate.FullVersion), $($candidate.Bits)-bit, $support - $($candidate.Executable)"
        }
    }

    Write-Step "Selecting the newest tested Python"
    $selected = Select-CompatiblePython $candidates
    if ($null -eq $selected) {
        Write-Warn "No installed Python has all required binary wheels."
        Install-CompatiblePython
        if ($DryRun) {
            Write-Pass "Dry run reached the automatic Python 3.12 fallback successfully."
            Write-Host ""
            Write-Host "[DRY RUN COMPLETE] No Python environment or package was changed." -ForegroundColor Green
            exit 0
        }
        $candidates = @(Find-PythonCandidates)
        $selected = Select-CompatiblePython $candidates
        if ($null -eq $selected) {
            throw "Python 3.12 installation completed but the executable could not be found. Close this window and run install_libs.bat again."
        }
    }
    Write-Pass "Selected Python $($selected.FullVersion) from $($selected.Source)."
    Write-Info "Python 3.13+ is intentionally skipped until cyal and pyenet publish compatible Windows wheels."

    Write-Step "Preparing the isolated .venv environment"
    $existingVenv = $null
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        $existingVenv = Get-PythonInfo $VenvPython "existing project .venv"
    }
    $canReuseVenv = (
        $null -ne $existingVenv -and
        $existingVenv.Bits -eq 64 -and
        $SupportedVersions -contains $existingVenv.MajorMinor
    )
    if ($DryRun) {
        if ($canReuseVenv) {
            Write-Info "DRY RUN: Would reuse the existing project .venv."
        }
        else {
            if (Test-Path -LiteralPath $VenvDir) {
                Write-Info "DRY RUN: Would preserve the incompatible or incomplete .venv as a timestamped backup."
            }
            Write-Info "DRY RUN: Would create $VenvDir with Python $($selected.FullVersion)."
        }
    }
    elseif (-not $canReuseVenv) {
        if (Test-Path -LiteralPath $VenvDir) {
            $backupDir = "$VenvDir.backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
            Write-Warn "The existing .venv is incompatible or incomplete. Preserving it as $backupDir"
            Move-Item -LiteralPath $VenvDir -Destination $backupDir
        }
        Invoke-Checked $selected.Executable @("-m", "venv", $VenvDir) "Creating the project virtual environment"
        Write-Pass "Created isolated environment at $VenvDir."
    }
    else {
        Write-Pass "Reusing the existing isolated environment."
    }

    Write-Step "Updating pip and wheel installation tools"
    if ($DryRun) {
        Write-Info "DRY RUN: Would update pip, setuptools, and wheel inside .venv only."
    }
    else {
        Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel") "Updating packaging tools inside .venv"
        Write-Pass "Packaging tools are ready."
    }

    Write-Step "Installing native libraries from prebuilt wheels"
    Write-Info "Visual Studio and Desktop development with C++ will not be used."
    if ($DryRun) {
        Write-Info "DRY RUN: Would require binary wheels from $NativeRequirements."
    }
    else {
        Invoke-Checked $VenvPython @(
            "-m", "pip", "install", "--upgrade", "--only-binary=:all:",
            "--requirement", $NativeRequirements
        ) "Installing native dependencies without compiling C or C++"
        Write-Pass "All native dependencies came from compatible wheels."
    }

    Write-Step "Installing game and compiler dependencies"
    if ($DryRun) {
        Write-Info "DRY RUN: Would install the tested dependency set from $DevelopmentRequirements."
    }
    else {
        $oldOnlyBinary = $env:PIP_ONLY_BINARY
        $env:PIP_ONLY_BINARY = "pygame,cyal,pyenet,cryptography,cffi,psutil,pywin32,zstandard"
        try {
            Invoke-Checked $VenvPython @(
                "-m", "pip", "install", "--upgrade", "--prefer-binary",
                "--requirement", $DevelopmentRequirements
            ) "Installing Beyond Tournament development dependencies"
        }
        finally {
            $env:PIP_ONLY_BINARY = $oldOnlyBinary
        }
        Write-Pass "Game and Nuitka dependencies are installed."
    }

    Write-Step "Running the full environment health check"
    if ($DryRun) {
        Write-Info "DRY RUN: Would import every critical module and verify required project files."
        Write-Host ""
        Write-Host "[DRY RUN COMPLETE] No Python environment or package was changed." -ForegroundColor Green
    }
    else {
        Invoke-Checked $VenvPython @($VerifyScript) "Testing Python, audio, networking, screen-reader, and build modules"
        Write-Host ""
        Write-Host "========================================================" -ForegroundColor Green
        Write-Host " Beyond Tournament development environment is ready." -ForegroundColor Green
        Write-Host " Build:  build.bat" -ForegroundColor Green
        Write-Host " Verify: build.bat --check" -ForegroundColor Green
        Write-Host " Run:    launch.bat" -ForegroundColor Green
        Write-Host "========================================================" -ForegroundColor Green
    }
}
catch {
    Write-Host ""
    Write-Host "[SETUP FAILED] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "The system Python installation was not removed or replaced." -ForegroundColor Yellow
    Write-Host "Run install_libs.bat again after correcting the reported problem." -ForegroundColor Yellow
    exit 1
}
finally {
    if ($script:TranscriptStarted) {
        Stop-Transcript | Out-Null
    }
}
