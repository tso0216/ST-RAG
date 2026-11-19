# v4 extract


## 這個模組做什麼

讀 ptt 文章 JSON（主文和推文），用 llm 抽出點（`place_note`）、線（`day`、`day_place`、`day_note`）、段（`move`），把文章裡的地點解析成 `place`，整篇寫進 SQLite。

| | 路徑 | 內容 |
|---|---|---|
| 輸入 | 資料夾或檔案，例如 `data/ptt_ask/itinerary/` | 一篇一個 JSON：`title`、`author`、`time`、`url`、`text`、`comments[]`（`tag`、`user`、`content`、`time`） |
| 輸入 | `data/geo/japan.duckdb` | 本機地圖（OSM＋Overture），`scripts/geo/build_japan.py` 建 |
| 輸出 | `data/v4.db` | 資料庫 |
| 輸出 | `data/place_unresolved.tsv` | 要使用者決定的地點，使用者在 `answer` 欄填答案後重跑 |
| 輸出 | `data/v4_pending/<文章 id>.json` | 還沒寫入的文章的 llm 解析結果，重跑時直接用，不再呼叫 llm 解析 |
| 輸出 | `data/logs/geocode_<開始時間>.jsonl` | 地點解析的 log：一個地點一行，記它在哪一步找到、錯在哪（見「看 log 找錯」） |

llm 和國土地理院的回應都不快取。

## 流程

| 步驟 | 做什麼 | 檔案 |
|---|---|---|
| 0 | 解析參數，呼叫 `pipeline.run`，最後印結果、決定結束代碼 | `__main__.py` |
| 1 | 資料夾展開成裡面的 `*.json`（照檔名排），`--limit` 只留前幾篇 | `pipeline.collect` |
| 2 | 逐篇整理文章：取文章 id、發文時間、合併推文、標出原 po 的推文。讀不了、取不到 id、沒有 `author`、重複、資料庫已有的略過，記在 `Report.skipped` | `pipeline._queue`、`article.py`、`store.has_article` |
| 3 | `v4_pending/` 有這篇就跳過第 4–6 步 | `pending.load` |
| 4 | 第一次 llm 解析：線（只看主文）→ 檢查 → 有問題附上次輸出和問題清單重問，最多 2 次；還有問題丟 `ParseError`，整批停 | `parse_line.py`、`feedback.py`、`check.check_line`、`prompts/line.md` |
| 5 | 第二次 llm 解析：點、段、day_note。推文 30 則一批，每批都附主文和第 4 步的結果；每批各自檢查和重問，還有問題的那幾筆拿掉；各批結果合併 | `parse_note.py`、`feedback.py`、`check.check_note`、`check.drop`、`prompts/note.md` |
| 6 | 兩次結果合成 `Parsed`，馬上存進 `v4_pending/` | `data_schema.Parsed.from_llm`、`pending.save` |
| 7 | 地點解析：這篇用到的每個地點各做一次，每個地點寫一行 log | `geocoding.py`（用 `v4/place.py`、`local_map.py`、`ask_place.py`、`unresolved.py`、`geocode_log.py`、`service/gsi.py`） |
| 8 | 地點都有結果：整篇寫入（一個 transaction），刪掉 pending。有地點要使用者決定：這篇不寫入，pending 留著 | `store.write_article`、`pending.delete` |
| 9 | 整批跑完：把要決定的地點寫進 tsv、印出清單和地點解析的統計。有要決定的地點或整批停下，結束代碼是 1 | `unresolved.save`、`unresolved.print_list`、`geocode_log.summary_lines`、`__main__.py` |

並行：第 4–6 步每篇一個 task，`--jobs` 是同時做的篇數（`asyncio.Semaphore`）。第 7–8 步照輸入的順序一篇一篇做；本機地圖的搜尋和國土地理院的查詢丟到 thread（搜尋要掃整張表、`service.gsi` 每秒限 1 次，等的時候不卡住其他篇的 llm 解析）。

開始時開本機地圖（唯讀）和這次的 log 檔，結束時關掉。本機地圖檔不存在就什麼都不做，印「請先跑 scripts/geo/build_japan.py」，結束代碼 1。

整批停下：`service.llm.LLMError`（含額度用完）、`service.gsi.GsiError`、`parse_line.ParseError`。`Report.error` 是原因，`Report.unfinished` 是還沒做完的文章。已經寫入的保留；已經解析完的留在 `v4_pending/`，重跑直接用。

### 地點解析的順序（`geocoding.Geocoder.resolve`）

流程圖：`docs/geocode_mvp.drawio`。只解析被行程、`place_note`、`move` 用到的地點。同一篇裡 `是` 先做、`猜的` 後做，這樣挑分店時同一天的其他地點已經有座標。

`located` 是 `是` 或 `猜的`：

1. 查資料庫正名、別名（`place.find`）：`name_ja`、`name_zh` 其中一個相同且 `city` 相同，只查到一個才用。`猜的` 只用 `name_ja` 查。
2. 查使用者填的答案（`Answers.for_place`），有就照答案處理（見「報 error 之後怎麼做」）。
3. 用這一輪的日文名稱和城市在本機地圖模糊搜尋（`LocalMap.search`，見下面「本機地圖」）。第 1 輪用 llm 抽取時給的 `name_ja`、`city`，之後用 llm 換的。
4. 有候選：問 llm 是哪一筆（`ask_place.pick`，回 0 是都不是）。候選附上來源、類別、縣市地址、離同一天已解析地點的距離。
5. 挑中：`place.add`（同一個來源 id 已在資料庫就回原本的 id），再加別名。`place_source` 寫候選的來源和來源 id；`name_ja`、種類、範圍用候選的；`city` 用 llm 給的。
6. 沒有候選或都不是：還沒滿 `config.GEOCODE_ROUNDS`（3）輪就請 llm 換一個名稱和城市（`ask_place.new_name`），回到 3。llm 換的名稱已經查過，那一輪就不查，算沒有候選。
7. 滿 3 輪還沒有：請 llm 上網查地址（`ask_place.web_address`，開 `WebSearch`、`WebFetch`）。沒查到 → `Unresolved(type=解析不到)`，原因「上網查不到地址」。
8. 用國土地理院查這個地址，取第一筆（`service.gsi`）。沒有結果 → `Unresolved(type=解析不到)`，原因「國土地理院查不到這個地址」。llm 查到的地址和網址寫進 `Unresolved.note`，印清單時看得到。
9. 有結果就用：`source` 是 `gsi`、`source_id` 是 `<國土地理院回傳的地址>｜<name_ja>`；`name_ja`、種類用 llm 抽取時給的，範圍是空的，座標用國土地理院的。

`located` 是 `猜不出來`：不查資料庫也不查地圖。不是行程的一站就不收入；是行程的一站就查使用者替這一篇填的答案（`Answers.for_guess`），沒有就回傳 `Unresolved(type=猜不出來)`。

不管結果如何，每個地點都寫一行 log（`geocode_log`）；llm 或國土地理院出錯時先寫一行 `中斷` 再讓例外往外丟。

### 本機地圖（`local_map.py`）

`data/geo/japan.duckdb` 一張 `place` 表（`local_map.SCHEMA`）：OSM 所有有 `name` 的物件和 Overture places（日本），每筆有來源、來源 id、名稱、比對用名稱（正規化後的所有名稱）、類別、種類（OSM 的 `place=*`、`boundary=administrative` 是區域，其他和 Overture 都是景點）、縣、市區町村、地址、代表點、範圍（區域而且是多邊形才有）。

名稱正規化（`normalize`，查詢和資料都用）：NFKC → 英文轉小寫 → 去掉空白（含全形）、`・`、`-`、`‐`、`－`（`ー` 保留）→ `ヶ`、`ヵ` 轉 `ケ`、`カ`。

一筆地點拿它每個比對用名稱跟查詢比，取最好的：

| 比對結果 | 條件 |
|---|---|
| 完全相同 | 兩個字串一樣 |
| 包含 | 地圖上的名稱包含查詢的名稱（反過來不算），而且查詢至少 2 個字 |
| 模糊 | DuckDB 的 `jaro_winkler_similarity` ≥ `config.FUZZY_THRESHOLD`（0.8） |

範圍是全日本。排序：同城市 → 完全相同、包含、模糊 → 相似度高 → 名稱短。同城市：城市、縣名、市區町村名都去掉結尾的「都府県市区町村」（去掉後只剩一個字就不去，所以「京都」對得上「京都府」「京都市」），城市跟縣名或市區町村名其中一個相同才算；城市是空的都不算。OSM、Overture 各取前 `config.CANDIDATES_PER_SOURCE`（5）筆，合起來照同樣的順序排，最多 10 筆，不合併去重。

`LocalMap.lookup(source, source_id)` 用來源 id 查一筆，給使用者填 OSM 網址的答案用。

別名：

| 情況 | `place_alias_zh` | `place_alias_ja` |
|---|---|---|
| `是` | `name_zh` | `name_ja`、實際查到它的那個名稱 |
| `猜的` | 不加，`place.name_zh` 也不填 | 同上 |
| `猜不出來`（照答案處理） | 不加 | 只加答案裡的日文名稱（答案是日文名稱時） |

## 檔案

### `src/v4/`（抽取和查詢共用，不依賴 `extract/`）

| 檔案 | 做什麼 | 對外提供 |
|---|---|---|
| `schema.sql` | 資料庫 schema | — |
| `db.py` | 開連線，開 `foreign_keys`；檔案不存在才照 `schema.sql` 建表，已存在的照原樣開 | `connect(path)`、`MEMORY` |
| `place.py` | `place`、`place_source` 和兩張別名表的讀寫 | `Place`、`get`、`by_source`、`source_of`、`matches`、`find`、`add`、`add_manual`、`add_alias` |
| `time_slots.md` | 時段對應的鐘點範圍 | `note.md` 的 `{{時段表}}` 換成它；`check.time_slots()` 讀它來提示 llm |

### `src/v4/extract/`

| 檔案 | 做什麼 | 對外提供 | 用到 |
|---|---|---|---|
| `__main__.py` | 命令列 | `main(argv)` 回傳結束代碼 | `pipeline`、`unresolved`、`geocode_log`、`config` |
| `pipeline.py` | 主流程 | `collect`、`run(sources, db_path, jobs, limit, *, local_map, gsi, unresolved_path, pending_dir, log_dir)` 回傳 `Report` | 其他所有模組、`v4.db` |
| `config.py` | 路徑和設定 | `DB_PATH`、`UNRESOLVED_PATH`、`PENDING_DIR`、`LOCAL_MAP_PATH`、`LOG_DIR`、`PROMPT_DIR`、`TIME_SLOTS_PATH`、`MODEL`、`EFFORT`、`RETRIES`、`GEOCODE_ROUNDS`、`CANDIDATES_PER_SOURCE`、`FUZZY_THRESHOLD`、`COMMENTS_PER_BATCH` | — |
| `data_schema.py` | 各步驟之間傳的資料結構、給 llm 的輸出格式 | 見「資料怎麼傳」 | — |
| `article.py` | 整理文章 | `load(path)`、`Skipped`、`article_id_of`、`posted_at`、`merge_comments` | `data_schema` |
| `parse_line.py` | 第一次 llm 解析 | `parse(article)`、`build_messages`、`ParseError` | `feedback`、`check`、`prompts/line.md` |
| `parse_note.py` | 第二次 llm 解析：分批、合併 | `parse(article, line)` 回傳（合併後的輸出，不收入的筆數）、`batches`、`build_messages` | `feedback`、`check`、`prompts/note.md`、`time_slots.md` |
| `check.py` | 兩次解析的檢查 | `check_line(out)`、`check_note(out, line)`、`drop(out, problems)`、`Problem`、`time_slots()` | `data_schema`、`config` |
| `feedback.py` | 呼叫 llm、檢查、有問題重問 | `ask(system, user, schema, check)` 回傳（最後的輸出，還有的問題） | `service.llm`、`config` |
| `geocoding.py` | 地點解析 | `Geocoder(conn, local_map, answers, gsi, log_file)`：`resolve_article(article_id, parsed)`、`resolve(place, ctx)` | `v4.place`、`local_map`、`ask_place`、`unresolved`、`geocode_log`、`service.gsi` |
| `local_map.py` | 本機地圖：名稱正規化、模糊搜尋、用來源 id 查一筆 | `LocalMap(path)`：`search(name_ja, city)`、`lookup(source, source_id)`、`close()`；`normalize`、`city_key`、`SCHEMA`、`MissingLocalMap` | `duckdb` |
| `geocode_log.py` | 地點解析的 log 檔和統計 | `GeocodeLog.start(folder)`：`write`、`close`；`Trace`、`read`、`summarize`、`summary_lines`；`result`、`outcome` 的值 | `data_schema` |
| `ask_place.py` | 問 llm 是哪一個、請 llm 換名稱、請 llm 上網查地址 | `pick`（回傳 `Pick`）、`new_name`、`web_address`（回傳 `WebAddress`）、`describe_candidate`、`nearest`、`km`、三個 `*_SCHEMA` | `service.llm`、`prompts/pick.md`、`prompts/new_name.md`、`prompts/web_address.md` |
| `unresolved.py` | `place_unresolved.tsv` 的讀寫 | `Answers.load/for_place/for_guess`、`parse_answer`、`Answer`、`save(path, items, handled)`、`print_list`、`COLUMNS` | `data_schema` |
| `pending.py` | `v4_pending/` 的存、讀、刪 | `save`、`load`、`delete`、`path_of` | `data_schema` |
| `store.py` | 寫入 `place` 和別名表以外的表 | `has_article`、`write_article(conn, article, parsed, place_ids)` 回傳不收入的筆數 | `data_schema` |
| `prompts/*.md` | `line.md`、`note.md`、`pick.md`、`new_name.md`、`web_address.md`，都是 system prompt | — | — |

外部服務在專案根目錄的 `service/`：`llm.py`（`call(system, user, schema, model, effort, tools)`、`LLMError`；`tools` 給了就開那些工具、直接執行不用確認）、`gsi.py`（國土地理院地址查詢，`GsiClient.search(address)` 回傳 `GsiHit` 或 None、`GsiError`）。

本機地圖由 `scripts/geo/build_japan.py` 建（手動跑一次，見「怎麼執行」）。

方向：`pipeline` 呼叫其他模組，其他模組不呼叫 `pipeline`；`data_schema`、`config` 不依賴其他模組。

## 資料怎麼傳

```
JSON ──article.load──▶ Article
Article ──parse_line.parse──▶ line（dict，照 LINE_SCHEMA）
Article + line ──parse_note.parse──▶ note（dict，照 NOTE_SCHEMA）、dropped
line + note ──Parsed.from_llm──▶ Parsed ◀──▶ v4_pending/<id>.json（Parsed.to_dict / from_dict）
Parsed ──Geocoder.resolve_article──▶ place_ids（地點編號 → place id 或 None）、[Unresolved]
Article + Parsed + place_ids ──store.write_article──▶ 資料庫
[Unresolved] ──unresolved.save──▶ place_unresolved.tsv
```

`Article`：`id`、`title`、`author`、`posted_at`（ISO 格式）、`text`、`comments`（`Comment`：`no` 從 1 編、`user`、`content`、`by_author`）。

llm 的輸出格式只在 `data_schema.py` 定義。所有欄位都是必填的字串，「留空」是空字串。

`LINE_SCHEMA`：

| 欄位 | 內容 |
|---|---|
| `places[]` | 主文裡的地點，見下表 |
| `days[].label` | 原文對這一天的寫法，只用來對推文，不寫進資料庫 |
| `days[].month` | 1–12，或留空 |
| `days[].stops[]` | 地點編號，照原文順序 |

`places[]` 的一筆（兩個格式共用）：

| 欄位 | 內容 |
|---|---|
| `ref` | 這篇文章裡的編號（`p1`、`p2`） |
| `name_zh`、`name_ja`、`city` | 繁中名稱、日文名稱、城市的日文名稱 |
| `kind` | `景點`／`區域`（llm 的看法；用本機地圖的候選時，寫進資料庫的種類由候選決定，上網查地址找到的才用它） |
| `located` | `是`／`猜的`／`猜不出來` |
| `reason` | `猜的` 寫依據，`猜不出來` 寫原因 |
| `sentence` | 原文提到它的那一句，照抄；問 llm 是哪一個和 tsv 都用它 |

`NOTE_SCHEMA`：

| 欄位 | 內容 |
|---|---|
| `places[]` | 推文新提到的地點 |
| `place_notes[]` | `place`（地點編號）、`content`、`times[]` |
| `moves[]` | `from`、`to`（地點編號）、`content`、`times[]` |
| `day_notes[]` | `day`（`days[].label`，講整趟的留空）、`content` |
| `times[]` 的一筆 | `year`、`month`、`day_type`、`time` |

`Parsed`：`places`（`PlaceRef`，兩次的合起來）、`days`（`Day`）、`place_notes`（`PlaceNote`）、`moves`（`Move`，`from`/`to` 改叫 `origin`/`dest`）、`day_notes`（`DayNote`）、`dropped`。

地點解析：`Candidate`（本機地圖的一筆：`source`、`source_id`、`name`、`category`、`kind`、`pref`、`municipality`、`address`、`lat`、`lon`、`boundary`、`match`、`similarity`）、`Context`（`article_id`、`in_itinerary`、`where`、`same_day`）、`Resolution`（`place_id`／`excluded`／`unresolved` 三選一）、`Unresolved`（tsv 的一列加上 `where`、`same_day`、`note`）。

`Report`：`written`、`skipped`、`waiting`、`dropped`、`unresolved`、`unfinished`、`error`、`geocode_log`（這次的 log 檔）。

## 規則

收入標準（寫在 `prompts/line.md`、`prompts/note.md`）：

- 講得出地點就收，不分這件事會不會隨時間改變。只抽日本的地點。
- 只有地名、沒有內容的不收。
- 原 po 的行程寫成 `day`、`day_place`，不另外寫成 `place_note`。
- 區域只能是行政區；車站、機場、飯店、商場、商店街是景點。
- 原 po 的推文當成主文的補充：可以產生 `place_note`、`move`，不產生 `day_note`。
- `move` 只有一端明確時，寫成 `place_note` 掛在明確的那一端。
- 欄位只放時空條件（地點、`times`、`days[].month`）。適合誰、交通方式這類其他條件沒有欄位，原文有講就寫在 `content` 裡。

`located`：

| 值 | 意思 | 處理 |
|---|---|---|
| `是` | 原文有名字，指得出是哪一個 | 走地點解析；本機地圖三輪無結果、上網也查不到是 `解析不到` |
| `猜的` | 連鎖店沒寫分店，`name_ja` 填猜的那一家 | 同上；`name_zh` 不拿來查也不加別名；挑分店時看離同一天其他地點的距離 |
| `猜不出來` | 沒有名字也沒有線索 | 是行程的一站：等使用者決定。不是：不收入。不能被 `place_note`、`move` 引用 |

時間（`check.py` 檢查）：

- `time`：0–23 的一個鐘點或 `H-H`（跨夜寫 `18-4`）。只講時段照 `time_slots.md`：早上 `5-10`、中午 `11-13`、下午 `14-17`、晚上 `18-4`。
- `month`：1–12 的一個月份。季節要一個月給一筆（冬天是 `12`、`1`、`2` 三筆）。`days[].month` 只有一個值，只寫季節時留空。
- `year` 四位數，`day_type` 是 `平日`／`假日`。都可以留空。

檢查：

| 哪一次 | 檢查什麼 | 重問後還有問題 |
|---|---|---|
| 第一次（`check_line`） | `ref` 不重複、不是空的；`name_zh`、`name_ja` 不是空的；`stops` 的編號都在 `places` 裡；`month` 格式 | 丟 `ParseError`，整批停 |
| 第二次（`check_note`） | 新地點的 `ref` 不重複、也不跟第一次的重複，名稱不是空的；引用的地點編號存在、不是 `猜不出來` 的；`move` 的兩端不同；`day` 留空或對得到某一天；`content` 不是空的；`times` 各欄格式 | 那幾筆拿掉，算進不收入的筆數 |

第二次解析的分批（`parse_note.py`）：第一批的 `<scope>` 是「主文和這批推文」，之後是「只有這批推文」，主文只抽一次。各批的新地點編號會撞，合併時程式重新編號，五個欄位都相同的新地點共用一個編號。沒有推文也做一批。

什麼情況不寫入：

| 情況 | 結果 |
|---|---|
| 沒有 `author`、讀不了、取不到文章 id、重複、資料庫已有 | 整篇略過 |
| 有地點 `解析不到`，或行程裡有 `猜不出來` 的地點還沒有答案 | 整篇先不寫入，等答案 |
| 地點不收入（`猜不出來` 且不是行程的一站，或答案填 `不收`） | 這個地點的 `place_note` 不寫；`day_place` 不放這一站，`seq` 跳號 |
| 一天裡沒有任何一站可以放 | 那一天的 `day` 不寫，掛在那一天的 `day_note` 也不寫 |
| `move` 一端不收入 | 改寫成 `place_note` 掛在另一端；兩端都不收入就不寫 |
| `move` 兩端解析成同一個 place | 改寫成 `place_note` |
| `times` 的一筆四欄都留空 | 那一筆不寫 |

## 資料庫

schema 在 `src/v4/schema.sql`。留空的欄位存 NULL。

| 表 | 存什麼 | 哪裡寫 |
|---|---|---|
| `article` | 一篇一列：`id`、`title`、`posted_at` | `store.write_article` |
| `place` | 地點：`kind`、`name_ja`（地圖上的名稱；上網查到的用 llm 給的）、`name_zh`（第一次抽到的繁中名稱）、`city`（llm 給的）、座標、`boundary`（區域才有） | `geocoding` 透過 `place.add`、`place.add_manual` |
| `place_source` | 地點的來源，`(source, source_id)` 不重複：`osm`（`node/123`、`way/456`、`relation/789`）、`overture`（GERS id）、`gsi`（`<國土地理院回傳的地址>｜<name_ja>`，同一個地址可能是同一棟樓的不同店，所以加上名稱）、`manual`（使用者填的座標） | 跟 `place` 同一個 transaction 寫入 |
| `place_alias_ja`、`place_alias_zh` | 別名，同一個 place 不重複 | `geocoding` 透過 `place.add_alias` |
| `day` | 行程的一天；同一篇照 `id` 排就是第幾天 | `store.write_article` |
| `day_place` | 一天裡的站，`seq` 是原文的第幾站 | 同上 |
| `day_note` | 推文對某一天（`day_id`）或整趟（`day_id` 是 NULL）的意見 | 同上 |
| `place_note`、`place_note_time` | 一個地點的經驗；`poi_name` 是這篇文章的寫法 | 同上 |
| `move`、`move_time` | 兩地之間的移動經驗；`from_name`、`to_name` 是這篇文章的寫法 | 同上 |

`place` 和別名在地點解析時就寫進去（各自 commit），所以等答案的文章解析出來的地點會先留在資料庫，重跑時資料庫直接命中。其他表一篇一個 transaction。

## 報 error 之後怎麼做

要使用者決定的地點兩種：

| `type` | 什麼情況 | tsv 怎麼列 | 答案對誰有效 |
|---|---|---|---|
| `解析不到` | `是`、`猜的`，本機地圖三輪都沒有，上網查地址也沒有 | `name_zh` 和 `city` 相同的一列（`猜的` 另外要 `name_ja` 相同），`articles` 列出所有文章 | 之後任何一篇同樣寫法的地點 |
| `猜不出來` | `猜不出來` 而且是行程的一站 | 一篇文章的一個地點一列（文章 id 和 `name_zh` 相同） | 只有那一篇 |

`place_unresolved.tsv` 的欄位：`type`、`name_zh`、`name_ja`、`city`、`articles`（逗號分隔）、`sentence`、`tried`（試過的日文名稱，頓號分隔）、`reason`（llm 寫的依據或原因）、`answer`。

`answer` 填下面其中一種（`unresolved.parse_answer` 分辨），然後重跑同一個指令：

| 填的內容 | 處理 |
|---|---|
| OSM 網址（含 `openstreetmap.org/node|way|relation/<id>`） | 在本機地圖用來源 id 查（`LocalMap.lookup`）；查不到再報一次 |
| 座標（`35.7148,139.7967`，全形逗號也可以） | `place.add_manual`：`source` 是 `manual`、`source_id` 是座標、種類是景點、`name_ja` 用 llm 給的；同一個座標重用同一個 place |
| `不收` | 這個地點不收入 |
| 其他文字 | 當成日文名稱在本機地圖模糊搜尋，取第一筆（不問 llm）；查不到再報一次 |

重跑時：

- 已經寫入的文章略過。等答案的文章從 `v4_pending/` 讀解析結果，不再呼叫 llm 解析，直接做地點解析。
- 地點都有結果就寫入、刪掉 pending。還有沒填的就繼續等。
- tsv：填過答案的列不動。沒填的列，這次處理過的文章先從 `articles` 拿掉，還要決定的再加回去；沒有文章的列刪掉。列的順序不變，新的加在最後。
- 填的答案在本機地圖查不到時，清單裡會印「填的答案…在本機地圖查不到，請改填座標」，那一列的答案留著，要自己改。

## 怎麼執行、怎麼測試

先建本機地圖（只要一次；換 OSM 或 Overture 版本時重跑）：

```bash
brew install osmium-tool
# 從 https://download.geofabrik.de/asia/japan.html 下載 japan-latest.osm.pbf
PYTHONPATH=.:src .venv/bin/python scripts/geo/build_japan.py ~/Downloads/japan-latest.osm.pbf
```

建置腳本會印各 `admin_level` 的行政界筆數（確認 4 是都道府縣、7 是市區町村，不對就改腳本開頭的常數重跑）、OSM 和 Overture 各幾筆，再用幾個地名試搜尋；有一個查不到結束代碼就是 1。Overture 的版本寫死在腳本的 `OVERTURE_RELEASE`。

然後跑抽取：

```bash
PYTHONPATH=.:src .venv/bin/python -m v4.extract data/ptt_ask/itinerary --db data/v4.db
```

| 參數 | 內容 |
|---|---|
| 資料夾或檔案 | 可以給多個；資料夾只取 `*.json` |
| `--db` | 資料庫路徑，預設 `data/v4.db`；不存在就建 |
| `--jobs` | 同時做 llm 解析的篇數，預設 1 |
| `--limit` | 只做前幾篇 |
| `--verbose`、`-v` | 在 stderr 印出每一步的過程：檢查出的問題和第幾次重試、被拿掉的資料、每個地點查本機地圖的候選、llm 挑選的理由、上網查到的地址 |

結束時印：寫入幾篇、略過幾篇（分原因）、等地點答案幾篇、不收入幾筆、地點解析的統計；要決定的地點清單和錯誤印在 stderr。結束代碼：都寫入是 0，有地點要決定、整批停下或沒有本機地圖是 1。

地點解析的統計（數字從這次的 log 檔算）：

```
地點解析：共 N 個（log：data/logs/geocode_xxx.jsonl）
  資料庫 n、答案 n、地圖-第1輪 n、地圖-第2輪 n、地圖-第3輪 n、上網 n、等你決定 n、不收 n
  等你決定的原因：上網查不到地址 n、國土地理院查不到這個地址 n
  本機地圖每一輪的失敗：沒有候選 n、AI 都不選 n
```

固定的項目沒有也印 0；其他有出現的（例如 `中斷`、`猜不出來，是行程的一站`、`名稱重複`）接在那一行後面。

`place_unresolved.tsv`、`v4_pending/`、本機地圖、log 資料夾的位置在 `config.py`，命令列不能改。

```bash
.venv/bin/python -m pytest tests/v4 -q
```

測試不打網路。`tests/v4/extract/fakes.py` 有小型本機地圖（`build_map`：表結構跟真的一樣，十幾筆手寫的資料）、假的國土地理院（`FakeGsi`：固定幾個地址）和假的 llm（看輸出格式分辨是哪一種呼叫：line、note、pick、new_name、web）；`tests/v4/extract/conftest.py` 把 `service.llm.call` 換成假的（`use_llm`）、提供 `local_map` fixture，並把 `config` 的資料庫、tsv、pending、本機地圖、log 路徑換到暫存資料夾，測試不會碰到真的 `data/`。`place`、`db` 的測試在 `tests/v4/`。

## 看 log 找錯

每次執行寫一個 `data/logs/geocode_<開始時間 YYYYmmdd-HHMMSS>.jsonl`（同一秒有兩次就加 `-2`），一行一個地點，每寫一行就 flush，整批停下前寫的都在。用 `jq` 看很方便，例如列出所有等你決定的地點和原因：

```bash
jq -c 'select(.result == "等你決定") | [.article_id, .name_zh, .fail_reason]' data/logs/geocode_xxx.jsonl
```

一行的欄位：`article_id`、`ref`、llm 抽取時給的 `name_zh`、`name_ja`、`city`、`kind`、`located`、`sentence`、`same_day`（同一天的地點和座標，還沒解析的是 null）、`result`、`fail_reason`、`rounds`、`web`、`place`（最後用的 place id、來源、來源 id、名稱、座標、Google 地圖連結）、`llm_calls`、`seconds`。

先看 `result`：

| `result` | 意思 | 接著看 |
|---|---|---|
| `資料庫` | 資料庫已經有（正名或別名） | `place`：是不是對的地點。錯的話是之前加錯了別名 |
| `答案` | 照使用者填的答案 | `place` |
| `地圖-第1輪`～`地圖-第3輪` | 本機地圖在第幾輪找到 | `rounds` 最後一輪的 `candidates`、`choice`、`pick_reason`；`place` 的 Google 地圖連結點開看位置對不對 |
| `上網` | 上網查地址找到 | `web`：`address` 和 `source_url` 對不對、`gsi_title` 是不是同一個地方（國土地理院可能只對到町） |
| `等你決定` | 放進「待人工補」清單 | `fail_reason`，再看 `rounds`、`web` |
| `不收` | 不收入 | — |
| `中斷` | llm 或國土地理院出錯，整批停下 | `fail_reason` 是錯誤訊息 |

`fail_reason`（`等你決定`、`中斷` 才有）：`上網查不到地址`、`國土地理院查不到這個地址`、`猜不出來，是行程的一站`、`填的答案「…」在本機地圖查不到，請改填座標`，或 `中斷` 的錯誤訊息。

`rounds` 一輪一個：`query`、`city` 是這一輪查的；`skipped` 是名稱跟之前重複、沒查；`candidates` 是每筆候選（編號、來源、來源 id、名稱、類別、縣、市區町村、地址、比對結果、相似度、離同一天最近的地點多遠）；`choice` 是 llm 挑的編號（0 是都不是，沒有候選沒問是 null）、`pick_reason` 是理由；`outcome` 是 `挑中`／`沒有候選`／`AI 都不選`／`名稱重複`；`next` 是這輪失敗後 llm 換的名稱和城市。

常見的錯：

- 挑錯了：`rounds[].candidates` 裡有對的那筆，但 `choice` 指到別筆 → 看 `pick_reason`，改 `prompts/pick.md`。
- 對的地點沒出現在候選裡：每一輪都 `沒有候選`，或候選全是 `模糊` 的不相干地點 → 看 `query` 是不是寫法不對（改 `prompts/new_name.md`），或本機地圖沒有這個地點。
- `名稱重複` 很多：llm 換不出新名稱。
- `上網` 的座標偏掉：`gsi_title` 只到町或丁目，表示國土地理院沒對到番地，座標是那一區的代表點。

## 要改某件事時去哪個檔案

| 要改什麼 | 動哪裡 |
|---|---|
| 提示詞（收入標準、怎麼分三種、範例） | `prompts/*.md` |
| 模型、effort、重試次數、查本機地圖幾輪、每個來源取幾筆候選、模糊比對門檻、一批幾則推文 | `config.py` |
| 加一條檢查 | `check.py` 加在 `check_line` 或 `check_note`，回傳 `Problem(kind, index, message)`；`tests/v4/extract/test_check.py` 加一個例子 |
| llm 輸出加欄位 | `data_schema.py` 的 schema 和對應的 dataclass（`Parsed.from_llm`、`from_dict` 會跟著吃）→ 提示詞 → `check.py` → `store.py`；要存進資料庫再改 `src/v4/schema.sql`。舊的 `v4_pending/` 檔案沒有新欄位，要刪掉重抽 |
| 時段對應的鐘點 | `src/v4/time_slots.md`（提示詞和檢查都讀它） |
| 地點解析的順序、別名怎麼加 | `geocoding.py` 的 `resolve`、`_apply`、`_add` |
| 給 llm 挑地點時附什麼資訊 | `ask_place.py` 的 `_describe`、`describe_candidate`，和 `prompts/pick.md` |
| 上網查地址的規則 | `prompts/web_address.md`、`ask_place.web_address` |
| 名稱正規化、比對方式、排序、同城市的判斷 | `local_map.py` 的 `normalize`、`city_key`、`_SEARCH` |
| 本機地圖收哪些資料、哪些算區域、縣市層級、Overture 版本 | `scripts/geo/build_japan.py`（改完重建） |
| 國土地理院的參數、限速 | `service/gsi.py` |
| log 的欄位、統計 | `geocode_log.py`（欄位在 `Trace.record`），哪一步填什麼在 `geocoding.py` |
| tsv 的欄位、新的答案種類、列怎麼合併 | `unresolved.py`（`COLUMNS`、`parse_answer`、`_same`），答案怎麼處理在 `geocoding._apply` |
| 寫進資料庫的規則（跳號、`move` 改寫） | `store.py` |
| 查正名、別名的方式 | `src/v4/place.py` |
| 推文怎麼合併、發文時間怎麼讀 | `article.py` |
| 哪些文章略過、並行、什麼時候整批停 | `pipeline.py` |
| 印出的結果、結束代碼、命令列參數 | `__main__.py` |
| `--verbose` 印的訊息 | 各模組的 `log.info(...)`（`logging`，logger 名稱是 `v4.extract.<模組>`）；`__main__.py` 只負責打開 |
