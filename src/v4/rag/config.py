"""路徑、模型、effort 和所有參數（規格 17）。不依賴其他模組。

解析使用者行程、地點解析的模型、重問次數、輪數、本機地圖路徑照 `v4.extract.config`，這裡不重複寫。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"
# 規格寫 data/v4.db；v4.db 還沒建好，先用抽取測試資料建的 test.db，之後改這一行就好
DB_PATH = DATA / "test.db"
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

# 拆題、初始計畫、補規劃、合成、其他題
MODEL = "claude-opus-5-5"
EFFORT = "medium"
# 整理重點
SUMMARIZE_MODEL = "claude-sonnet-5-5"
SUMMARIZE_EFFORT = "medium"

RETRIES = 2              # 拆題、整理重點檢查不過時重問幾次
PLAN_RETRIES = 1         # ③、⑦ 的節點格式不合法時請 LLM 重新輸出幾次

JEV_MODEL = "jev-1.14.0"
JEV_THRESHOLD = 0.5      # 機率 ≥ 這個值算相關
JEV_CONCURRENCY = 4      # 同時幾個 JEV 請求（整題共用）
LLM_CONCURRENCY = 4      # 同時幾個 LLM 請求（整題共用）

EMBEDDING_MODEL = "text-embedding-3-large"
EMBED_TOP = 50           # 點節點不限、天節點沒有排法時，超過這麼多筆就先用 embedding 取前幾筆（12.3）
DAY_TOP = 20             # 天節點有排法時最多挑幾天

REPLAN_ROUNDS = 2        # 補規劃最多幾輪
REPLAN_NODES = 5         # 補規劃每輪最多加幾個節點
MAX_NODES = 40           # 一題最多幾個節點

UNANSWERABLE_TEXT = ("這題要看即時或官方資訊（例如票價、訂房開放時間、活動日期），"
                     "這個系統只根據旅客經驗回答，沒辦法回答。")
NO_EVIDENCE_OPENING = "資料庫沒有相關的旅客經驗，以下是一般知識"
