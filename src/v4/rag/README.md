# v4 rag

規格：`docs/rag-main-system/spec_2.md`。

## 這個套件做什麼

用資料庫裡的旅客經驗（`v4.extract` 抽出來的 `place_note`、`move`、`day`、`day_note`），回答 ptt 日本旅遊板的發問文。

```
① 拆題、解析使用者的行程
→ 每一題一個迴圈：LLM 呼叫工具（一次可以好幾個）→ 程式執行、馬上回傳 → LLM 看完決定下一步……
→ LLM 不再呼叫工具，直接寫答案
→ 各題的答案照順序合併
```

LLM 走 Agent SDK（訂閱額度），8 個工具建成同一個行程裡的 MCP server（`mcp__db__*`），不開 Claude Code 內建的工具。

## 怎麼執行

```bash
PYTHONPATH=.:src .venv/bin/python -m v4.rag ask "2 月去京都，清水寺和伏見稻荷大社排同一天會不會太趕？"
PYTHONPATH=.:src .venv/bin/python -m v4.rag ask --article data/ptt_ask/ptt_M.1773015916.A.F40.json --verbose --log run.log
```

| 參數 | 內容 |
|---|---|
| 一句問題 | 文章 id 是 `q-` 加問題 SHA-256 的前 12 碼，標題空的，發文時間是現在（台北時間） |
| `--article` | PTT 文章 JSON；只用標題、發文時間、主文，不用推文 |
| `--db` | 資料庫，預設 `data/main.db` |
| `--log <路徑>` | 寫純文字 log（輸入、拆題、行程、每題每回合的判斷和工具呼叫、答案、用量）；沒給就不記 |
| `--verbose` | 邊跑邊印步驟（`⏺` 判斷或工具呼叫、`⎿` 回傳的第一行） |

需要：`data/geo/japan.duckdb`（本機地圖，地點解析用；沒有就停下）、`.env` 的 `OPENAI_API_KEY`（工具用 `query` 排序時算 embedding）。

會寫進資料庫：地點解析新增的 `place`、`place_source`、別名；算過的 embedding（`embedding` 表）。

出錯：工具的參數錯、外部服務出錯，當成工具錯誤回給 LLM，迴圈繼續。本機地圖不存在、拆題或行程解析出錯，這篇停下，錯誤印在 stderr，有 `--log` 時寫進 `# 錯誤`。

## 工具

| 類 | 工具 | 查什麼 |
|---|---|---|
| 找地點 | `find_place` | 一個名稱 → 地點的 id 和有幾筆經驗（必要時呼叫地點解析） |
| | `list_places` | 一個範圍（區域、周圍、城市）裡有哪些地點、各有幾筆經驗 |
| | `measure` | 一個排法的直線距離，以及每站、每段有幾筆經驗 |
| 搜經驗 | `search_notes` | 地點的經驗 |
| | `search_moves` | 移動的經驗 |
| | `search_days` | 別人有去這些地點的那幾天，附評語 |
| | `search_day_notes` | 對行程的評語，用語意找 |
| 讀全文 | `read_article` | 一篇文章在資料庫裡的全部內容 |

地點條件（`地點`、`周圍`、`不限`）、時間條件的比對、排序翻頁、筆數和分布，見規格第 3 章。

## 檔案

| 檔案 | 做什麼 |
|---|---|
| `__main__.py` | 命令列；開始前讀 `.env` |
| `config.py` | 路徑、模型、上限、筆數這些參數 |
| `pipeline.py` | 一篇的流程：讀輸入 → 拆題和解析行程（同時跑）→ 每題跑迴圈 → 合併；組第一則訊息 |
| `split.py`、`prompts/split.md` | 拆題，檢查不過時附上次輸出和問題重問 |
| `tools.py` | 8 個工具：「`Env`、參數 → 文字」的函式，不依賴 SDK。資料庫整個讀進記憶體（`Index`），`find_place` 新增地點後重讀。也有 embedding（先查表，沒有的才呼叫 OpenAI） |
| `loop.py` | 把工具包成 MCP server、計呼叫次數、用 Agent SDK 跑一題，把訊息交給 `log.py` |
| `log.py` | `--verbose` 的終端機輸出、`--log` 的檔案 |
| `prompts/agent.md` | 迴圈的 system prompt（`{{NO_EVIDENCE_OPENING}}` 換成 `config` 的值） |

用到的現有東西（不改）：`v4.extract`（`article.load`、`parse_line.parse`、`Geocoder`、`LocalMap`）、`service/llm.py`、`service/env.py`、`v4.db`。
