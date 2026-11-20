"""路徑和設定。不依賴其他模組。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"
DB_PATH = DATA / "v4.db"
PENDING_DIR = DATA / "v4_pending"                 # 還沒寫入的文章的 llm 解析結果
LOCAL_MAP_PATH = DATA / "geo" / "japan.duckdb"   # 本機地圖（OSM＋Overture），scripts/geo/build_japan.py 建
LOG_DIR = DATA / "logs"                           # log：每次執行一個資料夾 extract_<開始時間>/，裡面 _run.log 和每篇一個檔
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
# 時段對應的鐘點範圍：note.md 寫 `{{時段表}}` 的地方換成它，check.py 也讀它
TIME_SLOTS_PATH = Path(__file__).resolve().parents[1] / "time_slots.md"
TIME_SLOTS_MARK = "{{時段表}}"

MODEL = "claude-sonnet-5-5"
EFFORT = "medium"

RETRIES = 2              # 檢查有問題時，回報 llm 請它修正的次數
GEOCODE_ROUNDS = 2       # 一個地點查本機地圖幾輪都沒有結果，就改上網查地址
GEOCODE_BATCH = 10       # 地點解析一次問 llm 最多幾個地點（同一篇、同一種 located）
CANDIDATES = 10          # 本機地圖取幾筆候選（OSM、Overture 一起排）
CIRCLE_KM = (0.3, 10.0)  # llm 給的圓形範圍半徑，超出就改成最小／最大值
WIDEN_FACTOR = 3         # 範圍內沒有同名的就自動放寬一次：圓的半徑乘幾倍（行政區是市區町村放寬到都道府縣）
FUZZY_THRESHOLD = 0.8    # 模糊比對的門檻（jaro_winkler_similarity）
COMMENTS_PER_BATCH = 30  # 第二次解析一批看幾則（合併後的）推文
