# Bootstrap only: do not execute a Python candidate until its publisher is checked.
$ErrorActionPreference = 'Stop'
try {
    # A cmd.exe child can inherit PS7 module paths while launching Windows PS5.
    # Load this host's built-in security module, never an inherited user module.
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
    if ($env:BT_BUILD_PYTHON) {
        if (-not [IO.Path]::IsPathRooted($env:BT_BUILD_PYTHON)) { throw 'BT_BUILD_PYTHON must be an absolute path.' }
        $candidate = $env:BT_BUILD_PYTHON
    } else {
        $candidate = (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source
    }
    $file = Get-Item -LiteralPath $candidate -Force -ErrorAction Stop
    if ($file -isnot [IO.FileInfo] -or $file.Name -ine 'python.exe' -or
        ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Expected a regular python.exe, not an alias or link.' }
    $parentDirectory = $file.Directory
    while ($null -ne $parentDirectory) {
        if ($parentDirectory.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Python must not be reached through a directory link or junction.' }
        $parentDirectory = $parentDirectory.Parent
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $file.FullName
    if ($signature.Status -ne 'Valid' -or
        $signature.SignerCertificate.Subject -notmatch '(?:^|, )O=Python Software Foundation(?:,|$)') {
        throw 'Python must have a valid Python Software Foundation signature. Install a trusted Python runtime.'
    }
    # stdout is consumed as one quoted path by build.bat. Diagnostics go to stderr.
    Write-Output $file.FullName
    exit 0
} catch {
    [Console]::Error.WriteLine('[BUILD BLOCKED] ' + $_.Exception.Message)
    exit 1
}
