# Joytag 本地开发模式：Docker 只跑 qdrant，backend 本地 uvicorn --reload（改代码即时生效）
# 首次使用：先跑 backend/setup_dev.ps1 搭建本地 Python 3.11 环境
# 管理单页地址：http://localhost:8000/admin（9 个功能区 hash 路由，如 /admin#pending）

docker compose up -d qdrant

# .env 中的 QDRANT_URL=http://joytag-qdrant:6333 是 Docker 内部主机名，本地必须覆盖为 localhost
$env:QDRANT_URL = "http://localhost:6333"

# uvicorn 内置 .env 读取用系统默认 gbk 编码，根 .env 含中文注释时从项目根启动会崩，强制 UTF-8
$env:PYTHONUTF8 = "1"

.\.venv\Scripts\Activate.ps1
# app.py 用绝对导入 from services.xxx，必须 cd 到 backend（与 Docker WORKDIR /app 一致）
Set-Location (Join-Path $PSScriptRoot "backend")
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
