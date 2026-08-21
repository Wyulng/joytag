# 一次性搭建本地开发环境（Python 3.11 + venv + 依赖，走清华镜像）
# 之后日常开发直接用根目录 dev.ps1 启动
# 注意：本机默认 Python 3.14 无法装 torch（无 cp314 wheel），必须用 3.11（与 Docker 基准一致）

uv python install 3.11

Set-Location (Join-Path $PSScriptRoot "..")
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -r backend/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# Embedding 模型：使用 ModelScope 下载 GTE 多语言模型到本地目录
if (-not ((Test-Path "backend\models\gte-multilingual-base\config.json") -and
          (Test-Path "backend\models\gte-multilingual-base\model.safetensors"))) {
    Write-Host "下载 embedding 模型（ModelScope，约 650MB；运行时内存更高）..."
    uv pip install --python .venv/Scripts/python.exe modelscope -i https://pypi.tuna.tsinghua.edu.cn/simple
    Set-Location backend
    ..\.venv\Scripts\python.exe -c "from modelscope.hub.snapshot_download import snapshot_download; snapshot_download('iic/gte_sentence-embedding_multilingual-base', local_dir='models/gte-multilingual-base')"
    Set-Location ..
}

Write-Host ""
Write-Host "环境就绪。启动开发：.\dev.ps1"
