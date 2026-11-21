# v4 rag


## 這個模組做什麼

輸入 ptt 日本旅遊板的一篇發問文，或一句問題，用 v4 資料庫裡的旅客經驗回答。一篇發問文可能有好幾題，每一題分開回答，最後照原本的順序合在一起。設計和理由在 `docs/rag-main-system/spec.md`（以下的「規格 N」指它的第 N 節），流程圖在同資料夾的 `flow.drawio`。

| | 路徑 | 內容 |
|---|---|---|
| 輸入 | 問題文字，或 `--article` 給的 PTT 文章 JSON | 文章格式同 v4 抽取的輸入；推文不用 |
| 輸入 | `config.DB_PATH`（現在是 `data/test.db`；規格寫 `data/v4.db`，建好後改這一行） | v4 抽取建的資料庫 |
| 輸入 | `data/geo/japan.duckdb` | 本機地圖（地點解析用），`scripts/geo/build_japan.py` 建 |
| 輸出 | stdout | 答案（Markdown）；`--json` 時是完整紀錄 |
| 輸出 | `--log` 給的路徑 | 看得懂的流程紀錄（文字，見「流程紀錄」）；出錯時也寫 |
| 輸出 | 資料庫 | 新解析到的地點（`place`、`place_source`、別名表）、`embedding`、`jev_cache` |

LLM 和國土地理院的回應不快取；重現靠紀錄、`jev_cache` 和 `embedding` 表。

## 流程

| 步驟 | 做什麼 | 檔案 |
|---|---|---|
| 0 | 解析參數、讀 `.env`、開本機地圖（不存在就停）、讀發問文；呼叫 `pipeline.run_article`；印答案或紀錄、決定結束代碼 | `__main__.py` |
| ① | 拆題（LLM，檢查不過附上問題重問最多 2 次）；有點、段、行程題才解析使用者的行程（v4 抽取的 `parse_line.parse`）和行程裡的站（`Geocoder.resolve_article`）。整篇只跑一次 | `parse.py`、`prompts/split.md` |
| ② | 依題型分流：無法處理回固定文字；其他題和點、段、行程題都跑 ③–⑦ | `route.py` |
| ③ | 初始計畫：LLM 建第一輪的節點（點、段、天三種） | `plan.py`、`prompts/plan.md` |
| ④ | 驗證：格式（不合法附上問題重問一次，還不合法的拿掉）→ 地點解析 → 改了什麼 → 去重複 → 數量上限 | `validate.py` |
| ⑤ | 執行：同一輪的節點同時跑；點、段節點：候選 → 時間比對 →（不限時 embedding 取前 50）→ JEV 相關判斷 → 整理重點；天節點：有排法挑有相同站的天、沒有排法從評語找；每輪最後重算所有天節點的排法的計算 | `run.py`、`nodes.py`、`days.py`、`retrieve.py`、`timematch.py`、`scope.py`、`gate.py`、`embedding.py`、`summarize.py`、`prompts/summarize.md` |
| ⑥ | 查詢狀態表、還沒查的事、查無資料清單；補規劃沒滿 2 輪、節點沒到 40 個就到 ⑦ | `status.py` |
| ⑦ | 補規劃：LLM 決定加哪些節點（可以從沒有證據的節點放寬），回到 ④；輸出空的就停 | `replan.py`、`prompts/replan.md` |
| ⑧ | 合成：點、段、行程題用 `answer.md`，其他題用 `general.md`；各題照順序合併，每題前面加「### 第 N 題：{問句}」 | `answer.py` |

## 檔案

| 檔案 | 做什麼 | 對外提供 | 用到 |
|---|---|---|---|
| `__main__.py` | 命令列：`ask`、`index` | `main(argv)` 回傳結束代碼、`index_texts(conn)` | `pipeline`、`parse`、`trace`、`flowlog`、`embedding`、`config` |
| `pipeline.py` | 一篇發問文的主流程、錯誤處理、組紀錄 | `Services`、`Result`、`run_article(article, services, *, db_path, question_text)`、`question`、`ERRORS`、`params()` | 其他所有模組 |
| `config.py` | 路徑、模型、所有參數（規格 17） | `DB_PATH`、`PROMPT_DIR`、`MODEL`、`EFFORT`、`SUMMARIZE_MODEL`、`RETRIES`、`PLAN_RETRIES`、`JEV_*`、`LLM_CONCURRENCY`、`EMBEDDING_MODEL`、`EMBED_TOP`、`DAY_TOP`、`REPLAN_ROUNDS`、`REPLAN_NODES`、`MAX_NODES`、固定文字 | — |
| `data_schema.py` | 各步驟之間傳的資料結構、給 LLM 的輸出格式 | 見「資料怎麼傳」 | — |
| `coverage.py` | 資料庫每一欄用在哪（規格 4.4），只給測試核對 | `COVERAGE` | — |
| `parse.py` | ① | `from_question(text)`、`load_article(path)`、`split(article, record)`、`check_split`、`needs_itinerary`、`itinerary(article, geocoder, record)`、`Itinerary` | `v4.extract`（`article`、`feedback`、`parse_line`、`geocoding`） |
| `route.py` | ② | `route(qtype)`、`fixed_answer()` | — |
| `plan.py` | ③；③、⑦、⑧ 共用的輸入區塊 | `initial(article, question, ctx)`、`ask_nodes(...)`、`article_block`、`places_block`、`itinerary_block`、`question_block` | `validate`、`feedback` |
| `validate.py` | ④ | `Resolver`、`Ctx`、`check_format`、`format_drops`、`accept`、`diff`、`node_key`、`time_cond`、`raw_label` | `Geocoder` |
| `timematch.py` | 時間比對（規格 5） | `TimeRow`、`Match`、`match`、`match_row`、`hour_range`、`hours_of`、`day_rows`、`month_rows`、`year_of` | — |
| `scope.py` | 地點與範圍（規格 6） | `scope_places(conn, scope)`、`places_in_area`、`around`、`contains`、`area_size`、`km`、`zh_names`、`display_name`、`coordinates` | `v4.place` |
| `retrieve.py` | 撈候選、時間過濾、embedding 先取、其他條件下的筆數 | `Exp`、`place_notes`、`moves`、`time_filter`、`top_by_embedding`、`other_counts`、`id_key` | `scope`、`timematch`、`embedding` |
| `gate.py` | JEV 相關判斷、`jev_cache` | `Gate(conn, client, model, concurrency)`：`nouls(question, aspect, items)`、`asked`、`cached`；`Item`、`CRITERIA` | `service.jev` |
| `embedding.py` | OpenAI embedding、`embedding` 表快取、餘弦相似度 | `OpenAIEmbedder`、`CachedEmbedder`（`embed`、`aembed`、`sent`）、`cosines`、`EmbeddingError` | `numpy` |
| `nodes.py` | ⑤ 點、段節點和排法的計算 | `Env`、`point(node, env)`、`move(node, env)`、`calc(node, results, conn)` | `retrieve`、`scope`、`gate`、`summarize` |
| `days.py` | ⑤ 天節點 | `day(node, env)`、`view`、`compared_stops` | `nodes`、`retrieve`、`scope`、`timematch`、`summarize` |
| `summarize.py` | 整理重點 | `summarize(question, node, evidence, days, sem)`、`check`、`describe_day`、`describe_evidence` | `feedback` |
| `run.py` | ⑤ 的執行順序 | `run_round(batch, env, state)`、`recalc`、`run_node` | `nodes`、`days` |
| `status.py` | ⑥ | `add_batch`、`table`、`todos`、`no_data`、`should_replan`、`snapshot`、`result_brief` | `validate.diff` |
| `replan.py` | ⑦ | `replan(article, question, ctx, state)`、`build_user`、`budget` | `plan`、`status` |
| `answer.py` | ⑧ | `answer(question, ctx, state, stop_reason, *, general)`、`merge`、`build_user`、`cited` | `plan`、`status`、`summarize`、`llm_log` |
| `trace.py` | 紀錄 | `step(label, model)`、`Calls`、`node_record`、`result_record`、`dropped_record`、`dumps` | `service.llm` |
| `flowlog.py` | 把紀錄寫成看得懂的流程紀錄（`--log`） | `render(record, detail_name)`、`render_detail(record, details)`、`write(path, record, details)`、`detail_path`、`Capture` | `data_schema` |
| `prompts/*.md` | `split.md`、`plan.md`、`summarize.md`、`replan.md`、`answer.md`、`general.md`，都是 system prompt | — | — |

沿用 v4 抽取（不改行為）：`article.load`、`parse_line.parse`、`Geocoder`、`LocalMap`、`feedback.ask`（多了 `model`、`effort`、`retries`）、`llm_log.call`（多了 `model`、`effort`）、`check.Problem`。外部服務在專案根目錄的 `service/`：`llm.py`、`jev.py`、`gsi.py`、`env.py`。

方向：`pipeline` 呼叫其他模組，其他模組不呼叫它；`config`、`data_schema` 不依賴其他模組。

## 資料怎麼傳

```
問題文字 ──parse.from_question──▶ Article        JSON ──parse.load_article──▶ Article
Article ──parse.split──▶ list[Question]
Article ──parse.itinerary──▶ Itinerary（places: list[PlaceRef]、days: list[UserDay]、resolved: 地點編號 → place id）
每一題：
  Question + Ctx ──plan.initial──▶ Batch（accepted: list[Node]、dropped: list[Dropped]、order、record）
  Batch ──status.add_batch──▶ QState（nodes、items、dropped）
  Batch.accepted ──run.run_round──▶ QState.results（節點編號 → NodeResult）
  QState ──status.snapshot──▶ 查詢狀態表、還沒查的事、查無資料清單
  QState ──replan.replan──▶ Batch（回到 add_batch、run_round）
  QState ──answer.answer──▶ 答案
list[(Question, 答案)] ──answer.merge──▶ 整篇的答案
```

LLM 的輸出格式只在 `data_schema.py` 定義：`SPLIT_SCHEMA`、`PLAN_SCHEMA`、`REPLAN_SCHEMA`（多 `relax_from`）、`SUMMARY_SCHEMA`。所有欄位都必填，「留空」是空字串、空陣列或 0。

| 結構 | 內容 |
|---|---|
| `Question` | `no`（第幾題）、`text`（問句）、`qtype`（題型） |
| `UserDay` | 使用者行程的一天：`label`（原文寫法）、`month`、`stops`（`UserStop`：`ref`、`type`） |
| `Where` | 一個地點：`ref`（① 地點列表的編號）或新地點（`name_zh`、`name_ja`、`city`、`kind`），`place_id` 是 ④ 解析的結果 |
| `Scope` | 地點條件：`mode`（地點、周圍、不限）、`where`、`km` |
| `TimeCond` | 時間條件：`year`、`months`、`day_type`、`hours`（（H1, H2），H1 > H2 跨夜） |
| `Stop` | 排法展開後的一站：`where`、`day`（從哪一天展開來的）、`type` |
| `Node` | `no`（N1…）、`kind`、`aspect`、`reason`、`time`、`target`／`origin`／`dest`／`both_ways`、`arrangement`（LLM 寫的排法）、`stops`（展開後）、`relax_from`、`changes`、`round`、`raw` |
| `Evidence` | 一筆證據：`table`、`id`（`ref` 是「表名 id」）、`article_id`、`content`、`place`、`group`、`time`、`year`、`score`、`flags`、`places` |
| `Summary` | 整理重點：`points`、`numbers`、`differences`、`todo`、`dropped` |
| `DayView` | 天節點挑出或列出的一天：文章、第幾天、那篇有幾天、月份、站、評語、相同的站、時間不明 |
| `NodeResult` | `status`（有、有，沒有評語、沒有）、各種筆數、`evidence`、`groups`、`summary`、`other_counts`、`days`、`same_days`、`calc` |
| `Dropped` | ④ 拿掉的節點：`index`、`why`（格式、地點解析不到、重複、超過上限）、`reason`、`label`、`raw`、`round` |
| `QState` | 一題的狀態：`nodes`、`results`、`items`（查詢狀態表的項目）、`dropped`、`replans` |

## 規則

細節見規格；這裡列容易搞錯的：

- 時間比對（`timematch.py`，規格 5）：一筆經驗只要有一列時間符合就算；「時間不明」只看條件有限制的維度；年是空的看發文年份；天用 `day.month`，`day_note` 用它講到的天的月份，講整趟的用那篇所有天的月份。
- 地點條件（`scope.py`，規格 6）：景點只取它自己（不取包含它的區域）；區域是區域本身、範圍裡的景點、範圍裡比較小的區域（沒有 `boundary` 的區域只算它本身）；周圍只取景點和中心；解析不到的經驗用繁中名稱、繁中別名完全相同比對，標「名稱比對」。
- 天節點有排法時只比是不是同一個地點，不算距離、不比順序；相同的站多的在前，一樣多時發文時間新的在前、再照第幾天；取前 20 天。
- 證據編號是「表名 id」：`place_note`、`move`、`day_note` 的 id 都是「時間戳-第幾筆」，會重複，所以 `jev_cache` 的鍵含表名。照 id 排用 `retrieve.id_key`（`-10` 排在 `-2` 後面）。
- 補規劃：⑦ 每輪最多 5 個、整題最多 40 個、最多 2 輪；程式不判斷值不值得再查，⑦ 輸出空的就停。
- 錯誤（規格 16.2）：LLM、JEV、OpenAI embedding、國土地理院、行程解析檢查不過時，這篇停下；`run_article` 不丟出去，記在 `Result.error` 和紀錄裡。

## 資料庫

讀 `article`、`place`、別名表、`place_note`、`place_note_time`、`day`、`day_place`、`day_note`、`day_note_day`、`move`、`move_time`。寫：地點解析新增的 `place`、`place_source`、別名；第一次用到才建的 `embedding`（模型、文字、float32 向量）和 `jev_cache`（模型、判準的雜湊、問題、面向、經驗所在的表、經驗的 id、機率、實際回答的模型、建立時間）。這兩張不在 `schema.sql` 裡。

## 怎麼執行、怎麼測試

需要 `.env` 裡的金鑰：JEV（TypeSafe）、`OPENAI_API_KEY`（embedding）；LLM 走 Claude Code 的訂閱額度。本機地圖要先建好（見 `src/v4/extract/README.md`）。

```bash
# 問一句問題
PYTHONPATH=.:src .venv/bin/python -m v4.rag ask "10 月帶長輩，Day3 明治神宮 → 淺草寺 → 東京晴空塔，會不會太趕？"
# 回答一篇發問文，流程紀錄寫到檔案（/tmp/ask.log 白話寫每一輪查了什麼；/tmp/ask.detail.log 是每次 llm 的完整輸入和輸出）
PYTHONPATH=.:src .venv/bin/python -m v4.rag ask --article data/ptt_ask/itinerary/ptt_M.1767020760.A.E80.json --log /tmp/ask.log
# 先算好每一筆 place_note 和 day_note 的 embedding（可以不跑，ask 用到沒算過的會自己算）
PYTHONPATH=.:src .venv/bin/python -m v4.rag index
```

測試不打網路（LLM、JEV、OpenAI、國土地理院都換成假的，本機地圖用小型的）：

```bash
.venv/bin/python -m pytest tests/v4 -q
```

`tests/v4/rag/conftest.py` 建記憶體資料庫和固定的地點、經驗、行程；`fakes.py` 是假的 LLM（依輸出格式分辨是哪一種呼叫）、JEV（依內容裡的關鍵字給機率）、embedding（依內容裡的關鍵字給向量）。

評估（規格 19）在 `src/v4/eval/`：

```bash
PYTHONPATH=.:src .venv/bin/python -m v4.eval route --limit 100
PYTHONPATH=.:src .venv/bin/python -m v4.eval run data/ptt_ask/itinerary
```

## 紀錄

`--json`、`--log`、評估用的都是 `pipeline.run_article` 回傳的 `Result.record`：

| 鍵 | 內容 |
|---|---|
| `輸入` | 文章 id、標題、發文時間、主文（或問題文字）、資料庫路徑 |
| `參數` | `pipeline.params()`：模型、所有參數 |
| `拆題與解析` | `拆題`（每次的輸出、重問次數、每次的問題、修正、題目）、`行程解析`（做了嗎、地點列表、使用者的行程、行程裡的站的解析）、`節點裡的地點解析` |
| `每一題[]` | 題型、走哪一條路；`每一輪[]`：`計畫`（③ 或 ⑦ 的輸入、每次的輸出、每次檢查出的問題、地點解析、通過和拿掉的節點）、`節點`（條件和結果：筆數、證據的編號和分數和標記、整理重點、其他條件下的筆數、天、排法的計算）、這一輪結束時的查詢狀態表、還沒查的事、查無資料清單、排法的計算；`為什麼停`、`合成`（輸入）、`答案` |
| `用量` | LLM 每次呼叫的步驟、模型、token、估計金額、時間和合計；JEV 問了幾次、讀快取幾次；embedding 送了幾段文字；總時間 |
| `答案`、`錯誤` | 合併好的答案；出錯時只有 `錯誤` |

## 流程紀錄

`ask --log 路徑` 寫兩個檔案，`flowlog.py` 從上面的紀錄組出來：

| 檔案 | 內容 |
|---|---|
| 主檔（`--log` 給的路徑） | 白話：問題、題目、提到的地點、你的行程；每一輪地點對到哪、要查什麼（想知道什麼、為什麼）、拿掉的、結果（✓ 找到幾筆和前 3 點重點，✗ 是資料庫沒有、時間對不上還是不相關）、還缺什麼；停下來的原因、回答用到哪些節點、答案；出錯；花費 |
| 細節檔（檔名加 `.detail`，`ask.log` → `ask.detail.log`） | 參數、每一步的 LLM 用量；跑的時候 logger `v4` 的訊息：每次 llm 的完整輸入和輸出（同一段 system prompt 只寫一次全文）、重問、地點解析每一輪怎麼查、怎麼挑 |

## 要改某件事時去哪個檔案

| 要改什麼 | 動哪裡 |
|---|---|
| 提示詞（題型的定義、怎麼建節點、怎麼整理重點、怎麼放寬、答案的規則） | `prompts/*.md` |
| 模型、effort、重問次數、JEV 門檻、embedding 取幾筆、天節點挑幾天、補規劃的上限、資料庫路徑 | `config.py` |
| 節點的欄位（LLM 輸出加欄位） | `data_schema.py` 的 `NODE_FIELDS` 和 `Node` → `validate.py` 的 `check_format`、`_build`、`diff`、`node_key` → `prompts/plan.md`、`replan.md` |
| 節點格式怎麼檢查 | `validate.check_format`；`tests/v4/rag/test_validate.py` 加例子 |
| 時間怎麼比對、時間不明怎麼算 | `timematch.py` |
| 區域範圍、周圍、名稱比對 | `scope.py` |
| 撈候選的 SQL、其他條件下的筆數 | `retrieve.py`；天節點的在 `days.py` |
| 證據怎麼排、怎麼分組、排法的計算 | `nodes.py`（`order`、`group`、`calc`） |
| 天節點怎麼挑天、帶出什麼 | `days.py`（`_arranged`、`_free`、`view`） |
| 整理重點的檢查 | `summarize.check` |
| 查詢狀態表、什麼時候補規劃 | `status.py` |
| ⑦ 看到什麼 | `replan.build_user`、`status.result_brief` |
| ⑧ 看到什麼、合併的格式 | `answer.build_user`、`answer.merge` |
| 紀錄的內容 | `pipeline.py`（結構）、`trace.py`（節點、結果的寫法） |
| 流程紀錄（`--log`）的寫法 | `flowlog.py` |
| 流程紀錄細節檔的訊息 | 各模組的 `log.info(...)`；`__main__.ask` 用 `flowlog.Capture` 收 logger `v4` 的訊息：`v4.rag.<模組>` 印每一步，`v4.extract.llm_log` 印每次 llm 的完整輸入和輸出（同一段 system prompt 只印一次全文），`v4.extract.feedback` 印重問，`v4.extract.geocoding`、`ask_place` 印地點解析的過程 |
| 命令列參數、結束代碼 | `__main__.py` |
