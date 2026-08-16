"""海外采集目标国家集（唯一权威源，2026-08）。

与 Amazon 欧洲站点对齐的六国：DE/FR/NL/UK/IT/ES（去 BE/LU，加 IT/ES）。
同步位置：
- services/rule_manager.py VALID_COUNTRIES（小写派生，import 本模块勿再硬编码）
- services/transparency.py / app.py 披露正文（f-string 引用本模块）
- backend/static/js/admin.js EU_COUNTRIES（跨语言副本，前端国家下拉，与后端保持一致）
"""

EU_COUNTRIES = ["DE", "FR", "NL", "UK", "IT", "ES"]

# 本地化语言名（LLM 翻译目标语言描述用）
LANGUAGE_NAMES = {
    "DE": "德语",
    "FR": "法语",
    "NL": "荷兰语",
    "UK": "英语",
    "IT": "意大利语",
    "ES": "西班牙语",
}
