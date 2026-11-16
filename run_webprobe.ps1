$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "没有找到项目虚拟环境。请先按 README 的‘本地运行’步骤安装依赖。"
}

Set-Location -LiteralPath $projectRoot
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $projectRoot ".browsers"

& $pythonPath -m streamlit run (Join-Path $projectRoot "streamlit_app.py") --server.address 127.0.0.1
exit $LASTEXITCODE
