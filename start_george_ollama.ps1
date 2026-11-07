# start_george_ollama.ps1 - start Ollama for George, pinned to the RX 5700 XT.
# The GPU settings live ONLY here (this process and the ollama it starts),
# so nothing else on the PC inherits them.
#
# Run before DCS:  powershell -ExecutionPolicy Bypass -File start_george_ollama.ps1
# Note: it stops any Ollama already running (e.g. one started without the pinning).

param(
    [string]$OllamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
    [string]$Model     = "qwen3:8b"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $OllamaExe)) { Write-Host "ollama.exe not found: $OllamaExe"; exit 1 }

# Stop any Ollama that is already running - it may be on the wrong GPU.
$old = Get-Process -Name "ollama app", "ollama" -ErrorAction SilentlyContinue
if ($old) {
    Write-Host "Stopping running Ollama ($($old.Count) process(es))..."
    $old | Stop-Process -Force
    Start-Sleep -Seconds 2
}

# George's settings (same values as before, now process-only).
$env:HIP_VISIBLE_DEVICES     = "-1"      # no ROCm devices
$env:GGML_VK_VISIBLE_DEVICES = "1"       # Vulkan device 1 = RX 5700 XT
$env:OLLAMA_KEEP_ALIVE       = "-1"
$env:OLLAMA_CONTEXT_LENGTH   = "8192"
$env:OLLAMA_HOST             = "127.0.0.1:11434"
Remove-Item Env:HSA_OVERRIDE_GFX_VERSION -ErrorAction SilentlyContinue

$logDir = Join-Path $env:LOCALAPPDATA "Ollama"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logOut = Join-Path $logDir "george_serve.out.log"
$logErr = Join-Path $logDir "george_serve.log"      # Ollama logs to stderr

$proc = Start-Process -FilePath $OllamaExe -ArgumentList "serve" -WindowStyle Hidden `
            -RedirectStandardOutput $logOut -RedirectStandardError $logErr -PassThru
Write-Host "ollama serve started (PID $($proc.Id)). Log: $logErr"

# Wait for the API.
$up = $false
for ($i = 0; $i -lt 30; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:11434/api/version" -TimeoutSec 2 | Out-Null; $up = $true; break }
    catch { Start-Sleep -Seconds 1 }
}
if (-not $up) { Write-Host "Ollama did not answer within 30 s - see $logErr"; exit 1 }

# Load the model now (about 13 s) so George's first answer isn't slow.
Write-Host "Loading $Model..."
$body = @{ model = $Model; prompt = ""; keep_alive = -1; stream = $false } | ConvertTo-Json
try {
    Invoke-RestMethod -Method Post "http://127.0.0.1:11434/api/generate" -Body $body `
        -ContentType "application/json" -TimeoutSec 120 | Out-Null
} catch {
    Write-Host "Model load failed: $($_.Exception.Message)"
    exit 1
}

# Check which GPU Ollama reported.
$log = Get-Content $logErr -Raw -ErrorAction SilentlyContinue
if ($log -match "7900") {
    Write-Host "WARNING: the Ollama log mentions the 7900 XTX. Check $logErr before flying."
} elseif ($log -match "5700") {
    Write-Host "OK: Ollama reports the RX 5700 XT."
} else {
    Write-Host "Couldn't find the GPU name in the log - check Task Manager (5700 XT dedicated memory ~6 GB)."
}

$ps = Invoke-RestMethod "http://127.0.0.1:11434/api/ps" -TimeoutSec 5
foreach ($m in $ps.models) {
    $pct = if ($m.size -gt 0) { [math]::Round(100 * $m.size_vram / $m.size) } else { 0 }
    Write-Host ("{0}: {1:N1} GB, {2}% on GPU" -f $m.name, ($m.size / 1GB), $pct)
}
Write-Host "Ready for George."
