# 一次性搭建本地开发环境（Python 3.11 + venv + 依赖，走清华镜像）
# 之后日常开发直接用根目录 dev.ps1 启动
# 注意：本机默认 Python 3.14 无法装 torch（无 cp314 wheel），必须用 3.11（与 Docker 基准一致）

uv python install 3.11

Set-Location (Join-Path $PSScriptRoot "..")
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -r backend/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# Embedding 模型：hf-mirror 大文件走 AWS CDN 国内不通，用 ModelScope 下载到本地目录
if (-not (Test-Path "backend\models\bge-small-zh-v1.5\model.safetensors")) {
    Write-Host "下载 embedding 模型（ModelScope，约 96MB）..."
    uv pip install --python .venv/Scripts/python.exe modelscope -i https://pypi.tuna.tsinghua.edu.cn/simple
    Set-Location backend
    ..\.venv\Scripts\python.exe -c "from modelscope.hub.snapshot_download import snapshot_download; snapshot_download('AI-ModelScope/bge-small-zh-v1.5', local_dir='models/bge-small-zh-v1.5')"
    Set-Location ..
}

Write-Host ""
Write-Host "环境就绪。启动开发：.\dev.ps1"
