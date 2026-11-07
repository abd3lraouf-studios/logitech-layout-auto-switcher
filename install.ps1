<#
.SYNOPSIS
    Logitech Layout Auto Switcher installer for Windows.

.DESCRIPTION
    One-liner (nothing cloned):
        irm https://raw.githubusercontent.com/abd3lraouf-studios/logitech-layout-auto-switcher/main/install.ps1 | iex

    From a checkout:
        .\install.ps1

    To pass options through the one-liner, set an environment variable first:
        $env:LOGISWITCH_OS = 'macos'; irm https://.../install.ps1 | iex

    Creates a virtualenv, installs the package, verifies the keyboard is
    reachable and registers a Scheduled Task that starts the agent at logon.
    No administrator rights are required.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Uninstall
    .\install.ps1 -TargetOs macos
#>
# No [CmdletBinding()] and no [ValidateSet] here on purpose: when this script is
# piped through `iex`, parameter attributes are applied to the caller's scope and
# an empty default would be rejected before the body ever runs. The target OS is
# validated explicitly below instead.
param(
    [switch]$Uninstall,
    [string]$TargetOs = $env:LOGISWITCH_OS,
    [string]$InstallDir = $(if ($env:LOGISWITCH_HOME) { $env:LOGISWITCH_HOME } else { Join-Path $env:LOCALAPPDATA 'LogiSwitch' }),
    [string]$Ref = $(if ($env:LOGISWITCH_REF) { $env:LOGISWITCH_REF } else { 'main' })
)

$ErrorActionPreference = 'Stop'
$Repo = 'abd3lraouf-studios/logitech-layout-auto-switcher'

$ValidTargets = @('windows', 'macos', 'linux', 'android', 'ios', 'chrome',
                  'win', 'mac', 'pc', 'osx', 'darwin', 'chromeos')
if ($TargetOs) {
    $TargetOs = $TargetOs.Trim().ToLower()
    if ($ValidTargets -notcontains $TargetOs) {
        throw "Unknown target OS '$TargetOs'. Choose one of: $($ValidTargets -join ', ')"
    }
}

function Write-Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "    $m" -ForegroundColor Yellow }

if ($env:LOGISWITCH_UNINSTALL -eq '1') { $Uninstall = $true }

# --- where are we running from? ----------------------------------------------
# Piped through `iex`, there is no script file and no checkout around us.
$ScriptDir = $null
if ($PSCommandPath) { $ScriptDir = Split-Path -Parent $PSCommandPath }

if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir 'pyproject.toml'))) {
    $Root = $ScriptDir
    $FromCheckout = $true
    $Venv = Join-Path $Root '.venv'
} else {
    $Root = Join-Path $InstallDir 'app'
    $FromCheckout = $false
    $Venv = Join-Path $InstallDir 'venv'
}
$Python = Join-Path $Venv 'Scripts\python.exe'

# hidapi, the one compiled dependency, publishes Windows wheels up to this minor
# version. On a newer Python pip has to build it from source, which fails on any
# machine without a C compiler -- so a newer Python is only a last resort.
$MaxWheelMinor = 14

function Get-PythonCandidates {
    # Every interpreter we can find, probed directly by path. The py launcher is
    # only asked for its list: `py -3` runs the *newest* Python installed, which
    # may be one hidapi has no wheel for.
    $paths = New-Object System.Collections.Generic.List[string]
    foreach ($name in @('python', 'python3')) {
        foreach ($command in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($command.Source) { $paths.Add($command.Source) }
        }
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $ErrorActionPreference = 'Continue'
        $listing = & $py.Source -0p 2>$null
        $ErrorActionPreference = 'Stop'
        foreach ($line in @($listing)) {
            if ("$line" -match '([A-Za-z]:\\.*?\\python\.exe)') { $paths.Add($Matches[1]) }
        }
    }
    foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles")) {
        if ($root -and (Test-Path $root)) {
            Get-ChildItem -Path $root -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
                ForEach-Object { $paths.Add((Join-Path $_.FullName 'python.exe')) }
        }
    }
    $seen = @{}
    foreach ($path in $paths) {
        if (-not (Test-Path $path)) { continue }
        # Skip the Microsoft Store stub, which exits without running anything.
        if ($path -like '*WindowsApps*') { continue }
        $key = (Resolve-Path $path).Path.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        # No double quotes in the probe: Windows PowerShell 5.1 passes embedded "
        # to native commands unescaped, which corrupts the -c argument.
        $probe = 'import sys; v = sys.version_info; print(v[0], v[1], sep=chr(46))'
        # A candidate that writes to stderr must be skipped, not abort the script:
        # under 'Stop', Windows PowerShell 5.1 turns redirected native stderr into
        # a terminating error. The assignment is local to this function.
        $ErrorActionPreference = 'Continue'
        $version = & $path -c $probe 2>$null
        $ErrorActionPreference = 'Stop'
        if ($LASTEXITCODE -eq 0 -and "$version" -match '^3\.(\d+)$' -and [int]$Matches[1] -ge 9) {
            [pscustomobject]@{ Path = $path; Minor = [int]$Matches[1]; Version = "$version" }
        }
    }
}

function Resolve-BasePython {
    $found = @(Get-PythonCandidates)
    $best = $found | Where-Object { $_.Minor -le $MaxWheelMinor } |
        Sort-Object Minor -Descending | Select-Object -First 1
    if (-not $best) {
        $best = $found | Sort-Object Minor | Select-Object -First 1
        if ($best) {
            Write-Warn2 "Only Python $($best.Version) was found. hidapi has no prebuilt wheel for it yet,"
            Write-Warn2 "so pip will try to compile it. If that fails, install Python 3.$MaxWheelMinor from"
            Write-Warn2 'https://python.org and re-run.'
        }
    }
    if (-not $best) {
        throw "Python 3.9 or newer was not found. Install it from https://python.org (tick 'Add to PATH') and re-run."
    }
    return @{ Path = $best.Path; Prefix = @(); Version = $best.Version }
}

# --- uninstall ----------------------------------------------------------------
if ($Uninstall) {
    Write-Step 'Removing the logon agent'
    if (Test-Path $Python) {
        & $Python -m logiswitch uninstall
    } else {
        # schtasks reports a missing task on stderr; Windows PowerShell 5.1 would
        # turn that redirected stderr into a terminating error under 'Stop'.
        $ErrorActionPreference = 'Continue'
        foreach ($task in @('LogiSwitch', 'MXSwitch')) {
            schtasks /Query /TN $task *> $null
            if ($LASTEXITCODE -eq 0) {
                schtasks /End /TN $task *> $null
                schtasks /Delete /TN $task /F *> $null
                Write-Ok "removed scheduled task '$task'"
            }
        }
        $ErrorActionPreference = 'Stop'
    }
    if (-not $FromCheckout -and (Test-Path $InstallDir)) {
        # Deleting the task does not wait for the agent it started: that pythonw.exe
        # keeps the venv's compiled hid module open, and Windows refuses to delete a
        # loaded DLL. Stop whatever still runs from the install folder first.
        $prefix = (Resolve-Path $InstallDir).Path.TrimEnd('\') + '\'
        $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -and $_.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
        })
        foreach ($process in $running) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            $process.WaitForExit(10000) | Out-Null
        }
        # The handles close a moment after the process exits; give it a few tries.
        for ($attempt = 1; ; $attempt++) {
            try {
                Remove-Item -Recurse -Force $InstallDir -ErrorAction Stop
                break
            } catch {
                if ($attempt -ge 10) {
                    throw "could not remove ${InstallDir}: $($_.Exception.Message). Sign out and back in, then delete it."
                }
                Start-Sleep -Milliseconds 500
            }
        }
        Write-Ok "removed $InstallDir"
    }
    Write-Ok 'Done.'
    # schtasks leaves a non-zero code behind when it queried a task that was not
    # there; do not let that read as a failed uninstall.
    $global:LASTEXITCODE = 0
    return
}

Write-Step "Detected Windows $([Environment]::OSVersion.Version) ($env:PROCESSOR_ARCHITECTURE)"

Write-Step 'Locating Python'
$base = Resolve-BasePython
Write-Ok "$($base.Path) ($($base.Version))"

# --- fetch, if we were piped in ----------------------------------------------
if (-not $FromCheckout) {
    Write-Step "Downloading $Repo@$Ref"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    if (Test-Path $Root) { Remove-Item -Recurse -Force $Root }
    $zip = Join-Path ([IO.Path]::GetTempPath()) "logiswitch-$Ref.zip"
    $extract = Join-Path ([IO.Path]::GetTempPath()) "logiswitch-extract-$([guid]::NewGuid().ToString('N'))"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri "https://codeload.github.com/$Repo/zip/$Ref" -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $extract -Force
        $inner = Get-ChildItem -Path $extract -Directory | Select-Object -First 1
        if (-not $inner) { throw 'the downloaded archive was empty' }
        Move-Item -Path $inner.FullName -Destination $Root
    } finally {
        Remove-Item -Force $zip -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $extract -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path (Join-Path $Root 'pyproject.toml'))) { throw 'download did not contain the project' }
    Write-Ok $Root
}

# --- install ------------------------------------------------------------------
if (Test-Path $Python) {
    # A venv left by an earlier run on a Python hidapi has no wheel for can never
    # install; rebuild it on the interpreter chosen above.
    $ErrorActionPreference = 'Continue'
    $venvVersion = & $Python -c 'import sys; print(sys.version_info[1])' 2>$null
    $ErrorActionPreference = 'Stop'
    if ("$venvVersion" -match '^\d+$' -and [int]$venvVersion -gt $MaxWheelMinor -and
            $base.Version -ne "3.$venvVersion") {
        Write-Step "Replacing the Python 3.$venvVersion virtualenv"
        Remove-Item -Recurse -Force $Venv
    }
}
if (-not (Test-Path $Python)) {
    Write-Step 'Creating the virtualenv'
    [string[]]$venvArgs = @($base.Prefix) + @('-m', 'venv', $Venv)
    & $base.Path @venvArgs
    if ($LASTEXITCODE -ne 0) { throw 'failed to create the virtualenv' }
}

Write-Step 'Installing logiswitch'
& $Python -m pip install --quiet --upgrade pip
& $Python -m pip install --quiet "$Root"
if ($LASTEXITCODE -ne 0) { throw "pip install failed on Python $($base.Version) ($($base.Path)); the error is above" }
Write-Ok (& $Python -m logiswitch --version)

Write-Step 'Checking the keyboard is reachable'
& $Python -m logiswitch status
if ($LASTEXITCODE -ne 0) {
    Write-Warn2 'No supported device answered.'
    Write-Warn2 'If you use a KVM, switch it to this machine and re-run.'
    Write-Warn2 'Installing anyway; the agent retries on every device event.'
}

Write-Step 'Registering the logon agent'
$installArgs = @('-m', 'logiswitch', 'install')
if ($TargetOs) { $installArgs += @('--os', $TargetOs) }
& $Python @installArgs
if ($LASTEXITCODE -ne 0) { throw 'service registration failed' }

Write-Host ''
Write-Host 'Installed.' -ForegroundColor Green
Write-Host "  status:    $Python -m logiswitch status"
Write-Host "  service:   $Python -m logiswitch service-status"
Write-Host "  logs:      $env:LOCALAPPDATA\LogiSwitch\logiswitch.log"
if ($FromCheckout) {
    Write-Host "  uninstall: .\install.ps1 -Uninstall"
} else {
    Write-Host "  uninstall: `$env:LOGISWITCH_UNINSTALL='1'; irm https://raw.githubusercontent.com/$Repo/main/install.ps1 | iex"
}
$global:LASTEXITCODE = 0
