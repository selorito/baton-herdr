# Installs baton on Windows (native, without WSL), for the current user only.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1          # asks before each step
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Yes     # no questions
#   ... install.ps1 -Yes -NoSetup                                   # the same, without step 5
#
# Without a terminal (run by a program), every question is answered "no", so nothing is
# installed without -Yes, and step 5, which asks for your bot token, is never run.
#
# What it does, in order. Each step is skipped when its tool is already there.
#   1. Checks for Git for Windows (Claude Code needs its Git Bash; baton needs git). If it
#      is missing, offers winget's per-user install, or says where to get it.
#   2. uv (Python's package manager), with its official installer, into ~\.local\bin.
#   3. herdr $HerdrVersion, checked against its known SHA-256, into
#      %LOCALAPPDATA%\Programs\herdr, which is added to your user PATH.
#   4. baton itself, with uv, on Python 3.12, its wheel checked against its known SHA-256.
#   5. `baton setup`, which asks for your Telegram bot and writes ~\.config\baton.
#
# What it never does: ask for administrator rights, change system settings or files
# outside your profile, delete anything, or install the coding agents. Native Windows is
# new in baton 1.4 (beta); WSL is the proven way.
param(
    [switch] $Yes,
    [switch] $NoSetup,
    # Leave the user PATH in the registry alone (for tests in a throwaway profile).
    [switch] $NoUserPath,
    [string] $BatonVersion = $(if ($env:BATON_VERSION) { $env:BATON_VERSION } else { "1.5.0" }),
    [string] $BatonSource = $env:BATON_SOURCE
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$HerdrVersion = "0.9.5"
$HerdrSha256 = "d2c6c9c971c43a0bfdde8c692a9a1a7a45c3e4807cd01ae45662b4734fac1cbf"
$BatonRelease = "https://github.com/selorito/baton-herdr/releases/download/v$BatonVersion"
$BatonWheel = "baton_herdr-$BatonVersion-py3-none-any.whl"
# The wheel's SHA-256 for the versions this script knows; another version is checked
# against its release's SHA256SUMS. A BatonSource given by hand (tests) is not checked.
$BatonSha256 = @{
    "1.4.0" = "1599b7139daf44845157d30f62bdab374a518bf0e17543af32ecf6cf10d63481"
    "1.5.0" = "019172cb3c22690825a12492517adb88e5e13b4e5164795b8a913a4d9597cd41"
}
$CheckBaton = -not $BatonSource
if (-not $BatonSource) { $BatonSource = "$BatonRelease/$BatonWheel" }
# A terminal to ask in. Run by a program (a coding agent, CI) there is none: questions are
# answered "no", -Yes accepts every step, and baton setup, which asks for the bot token,
# is left to you.
$Interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
$Bin = Join-Path $HOME ".local\bin"
$HerdrHome = Join-Path $env:LOCALAPPDATA "Programs\herdr"

function Say([string] $text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Note([string] $text) { Write-Host "    $text" }
function Fail([string] $text) { Write-Host ""; Write-Host "error: $text" -ForegroundColor Red; exit 1 }
function Ask([string] $question) {
    if ($Yes) { return $true }
    if (-not $Interactive) {
        Note "$question -> no (no terminal to ask in; run with -Yes to accept every step)"
        return $false
    }
    $answer = Read-Host "    $question [Y/n]"
    return ($answer -eq "" -or $answer -match "^(y|yes|e|evet)$")
}
function Have([string] $name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }
function Add-UserPath([string] $dir) {
    if (($env:Path -split ";") -notcontains $dir) { $env:Path = "$dir;$env:Path" }
    if ($NoUserPath) { return }
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($current -split ";") -notcontains $dir) {
        [Environment]::SetEnvironmentVariable("Path", "$dir;$current", "User")
        Note "Added $dir to your user PATH (new terminals see it)."
    }
}

# --- What this machine is -------------------------------------------------------------
$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -ne "AMD64" -and $arch -ne "ARM64") { Fail "baton needs a 64-bit Windows (found $arch)" }
Say "baton $BatonVersion for Windows ($arch)"
if ($arch -eq "ARM64") { Note "ARM64: herdr and baton-detect run as x64 under emulation (not tested)." }
Note "Everything goes under $HOME; nothing needs administrator rights."
New-Item -ItemType Directory -Force $Bin | Out-Null
if (($env:Path -split ";") -notcontains $Bin) { $env:Path = "$Bin;$env:Path" }

# --- 1. Git for Windows ---------------------------------------------------------------
if (Have git) {
    Say "git: found $((Get-Command git).Source)"
} else {
    Say "Git for Windows is missing"
    Note "baton makes projects as git repositories, and Claude Code needs Git Bash."
    if ((Have winget) -and (Ask "Install it now with winget (per user)?")) {
        winget install --id Git.Git -e --scope user --accept-source-agreements --accept-package-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    } else {
        Note "Install it from https://git-scm.com/download/win, then run this again."
    }
}

# --- 2. uv ----------------------------------------------------------------------------
if (Have uv) {
    Say "uv: found $((Get-Command uv).Source)"
} else {
    Say "uv is missing"
    Note "Its official installer (https://docs.astral.sh/uv/) puts uv in ~\.local\bin"
    Note "and adds that directory to your user PATH."
    if (-not (Ask "Install uv now?")) { Fail "uv is needed; install it and run this again" }
    $installer = Join-Path $env:TEMP "uv-install.ps1"
    Invoke-WebRequest -UseBasicParsing "https://astral.sh/uv/install.ps1" -OutFile $installer
    $env:UV_INSTALL_DIR = $Bin
    if ($NoUserPath) { $env:UV_NO_MODIFY_PATH = "1" }
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installer
    Remove-Item $installer -ErrorAction SilentlyContinue
    if (-not (Have uv)) { Fail "uv did not install; see the output above" }
}

# --- 3. herdr -------------------------------------------------------------------------
if (Have herdr) {
    Say "herdr: found $((Get-Command herdr).Source) ($(herdr --version))"
} else {
    $url = "https://github.com/herdrdev/herdr/releases/download/v$HerdrVersion/herdr-windows-x86_64.zip"
    Say "herdr is missing"
    Note "Download: $url"
    Note "Into: $HerdrHome (SHA-256 checked), added to your user PATH"
    if (-not (Ask "Install herdr $HerdrVersion now?")) { Fail "herdr is needed; install it and run this again" }
    $zip = Join-Path $env:TEMP "herdr-$HerdrVersion.zip"
    Invoke-WebRequest -UseBasicParsing $url -OutFile $zip
    $sha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($sha -ne $HerdrSha256) {
        Remove-Item $zip
        Fail "the herdr download does not match its known checksum; nothing was installed"
    }
    New-Item -ItemType Directory -Force $HerdrHome | Out-Null
    Expand-Archive -Force $zip $HerdrHome
    Remove-Item $zip
    Add-UserPath $HerdrHome
    Note "Installed $HerdrHome\herdr.exe"
}

# --- 4. baton -------------------------------------------------------------------------
$installed = if (Have baton) { (baton version) 2>$null } else { "" }
if ($installed -eq "baton $BatonVersion") {
    Say "baton: found $((Get-Command baton).Source) ($BatonVersion)"
} else {
    Say "baton $BatonVersion"
    Note "From: $BatonSource"
    if ($CheckBaton) { Note "SHA-256 checked before it is installed" }
    Note "With: uv tool install --python 3.12 (uv downloads Python 3.12 if it is missing)"
    if (-not (Ask "Install baton now?")) { Fail "nothing more to do without baton" }
    if ($CheckBaton) {
        $wheelDir = Join-Path $env:TEMP "baton-$BatonVersion"
        New-Item -ItemType Directory -Force $wheelDir | Out-Null
        $wheel = Join-Path $wheelDir $BatonWheel
        Invoke-WebRequest -UseBasicParsing $BatonSource -OutFile $wheel
        $expected = $BatonSha256[$BatonVersion]
        if (-not $expected) {
            $sums = (Invoke-WebRequest -UseBasicParsing "$BatonRelease/SHA256SUMS").Content
            if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
            foreach ($line in ($sums -split "`n")) {
                $parts = $line.Trim() -split "\s+"
                if ($parts.Count -eq 2 -and $parts[1].TrimStart("*") -eq $BatonWheel) { $expected = $parts[0] }
            }
        }
        if (-not $expected -or (Get-FileHash $wheel -Algorithm SHA256).Hash.ToLower() -ne $expected) {
            Remove-Item -Recurse -Force $wheelDir
            Fail "the baton download does not match its checksum; nothing was installed"
        }
        $BatonSource = $wheel
    }
    uv tool install --force --python 3.12 $BatonSource
    if ($LASTEXITCODE -ne 0) { Fail "uv could not install baton; see the output above" }
    Add-UserPath $Bin
}

# --- What is left ---------------------------------------------------------------------
Say "Coding agents"
Note "baton drives the agents you install and sign in to yourself; at least one is needed."
foreach ($tool in "claude", "codex", "opencode") {
    if (Have $tool) { Note "${tool}: $((Get-Command $tool).Source)" }
}
if (-not (Have claude)) { Note "Claude Code: irm https://claude.ai/install.ps1 | iex   then: claude (sign in)" }
if (-not (Have codex)) { Note "Codex CLI:   npm install -g @openai/codex   then: codex (sign in)" }

if (-not $NoSetup -and -not $Interactive) {
    Say "baton setup"
    Note "It asks for your Telegram bot token, so it needs you at a terminal; it was not"
    Note "run. Next, in a terminal: baton setup"
} elseif (-not $NoSetup) {
    Say "baton setup"
    Note "It finds your agents, sets up Telegram (you need a bot token from @BotFather),"
    Note "writes ~\.config\baton, and prints the command that starts baton at boot."
    if (Ask "Run baton setup now?") { baton setup } else { Note "Later: baton setup" }
}
Say "Done. Check everything with: baton doctor"
