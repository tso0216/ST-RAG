"""路徑和設定。不依賴其他模組。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"
DB_PATH = DATA / "v4.db"
UNRESOLVED_PATH = DATA / "place_unresolved.tsv"   # 要使用者決定的地點，使用者在這裡填答案
PENDING_DIR = DATA / "v4_pending"                 # 還沒寫入的文章的 llm 解析結果
LOCAL_MAP_PATH = DATA / "geo" / "japan.duckdb"   # 本機地圖（OSM＋Overture），scripts/geo/build_japan.py 建
LOG_DIR = DATA / "logs"                           # 地點解析的 log 檔（geocode_<開始時間>.jsonl）
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
# 時段對應的鐘點範圍：note.md 寫 `{{時段表}}` 的地方換成它，check.py 也讀它
TIME_SLOTS_PATH = Path(__file__).resolve().parents[1] / "time_slots.md"
TIME_SLOTS_MARK = "{{時段表}}"

MODEL = "claude-sonnet-5-5"
EFFORT = "medium"

RETRIES = 2              # 檢查有問題時，回報 llm 請它修正的次數
GEOCODE_ROUNDS = 3       # 一個地點查本機地圖幾輪都沒有結果，就改上網查地址
CANDIDATES_PER_SOURCE = 5  # 本機地圖每個來源（OSM、Overture）取幾筆候選
FUZZY_THRESHOLD = 0.8    # 模糊比對的門檻（jaro_winkler_similarity）
COMMENTS_PER_BATCH = 30  # 第二次解析一批看幾則（合併後的）推文
