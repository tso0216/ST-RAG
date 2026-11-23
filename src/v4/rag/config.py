"""路徑和設定。不依賴其他模組。行程解析、地點解析的模型和重問次數照 `v4.extract.config`。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DB_PATH = ROOT / "data" / "main.db"
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

MODEL = "claude-opus-5-5"
EFFORT = "medium"

RETRIES = 2              # 拆題檢查不過時重問幾次
TOOL_CALLS = 40          # 每題最多呼叫工具幾次（回錯誤的也算）
MAX_TURNS = 43           # 回合上限

LIST_LIMIT, LIST_MAX = 20, 50   # 每次預設回幾筆、最多回幾筆
DAY_LIMIT, DAY_MAX = 10, 20     # search_days 預設回幾天、最多回幾天
PLACE_CANDIDATES = 5            # find_place 最多回幾個候選
PLACE_SPREAD = 10               # 地點分布列前幾個

EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_BATCH = 128           # 一次最多送幾段給 OpenAI

OVER_LIMIT_TEXT = "這一題查了太多次，沒有答完。"
NO_EVIDENCE_OPENING = "資料庫沒有相關的旅客經驗，以下是一般知識"
NO_QUESTION_TEXT = "可以問我日本旅遊的行程、交通或景點，我會用旅客的經驗回答。"
