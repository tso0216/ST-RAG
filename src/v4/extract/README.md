# v4 extract


## 這個模組做什麼

讀 ptt 文章 JSON（主文和推文），用 llm 抽出點（`place_note`）、線（`day`、`day_place`、`day_note`、`day_note_day`）、段（`move`），把文章裡的地點解析成 `place`，整篇寫進 SQLite。

| | 路徑 | 內容 |
|---|---|---|
| 輸入 | 資料夾或檔案，例如 `data/ptt_ask/itinerary/` | 一篇一個 JSON：`title`、`author`、`time`、`url`、`text`、`comments[]`（`tag`、`user`、`content`、`time`） |
| 輸入 | `data/geo/japan.duckdb` | 本機地圖（OSM＋Overture），`scripts/geo/build_japan.py` 建 |
| 輸出 | `data/v4.db` | 資料庫 |
| 輸出 | `data/v4_pending/<文章 id>.json` | 還沒寫入的文章的 llm 解析結果，重跑時直接用，不再呼叫 llm 解析 |
| 輸出 | `data/logs/extract_<開始時間>/` | 這次執行的 log（純文字），一次一個資料夾：`_run.log` 是整批的進度和完整的總結，`<文章 id>.log` 一篇一個，有那一篇的進度、一個地點一段（查了什麼、在哪一步找到、錯在哪）；`--verbose` 時另外有每次 llm 的輸入輸出和每一步的過程（見「看 log 找錯」） |

llm 和國土地理院的回應都不快取。

## 流程

| 步驟 | 做什麼 | 檔案 |
|---|---|---|
| 0 | 解析參數，呼叫 `pipeline.run`，最後印結果、解析不到的地點、llm 花的 token、決定結束代碼 | `__main__.py` |
| 1 | 資料夾展開成裡面的 `*.json`（照檔名排），`--limit` 只留前幾篇 | `pipeline.collect` |
| 2 | 逐篇整理文章：取文章 id、發文時間、合併推文、標出原 po 的推文。讀不了、取不到 id、沒有 `author`、重複、資料庫已有的略過，記在 `Report.skipped` | `pipeline._queue`、`article.py`、`store.has_article` |
| 3 | `v4_pending/` 有這篇就跳過第 4–6 步 | `pending.load` |
| 4 | 第一次 llm 解析：線（只看主文）→ 檢查 → 有問題附上次輸出和問題清單重問，最多 2 次；還有問題丟 `ParseError`，整批停 | `parse_line.py`、`feedback.py`、`check.check_line`、`prompts/line.md` |
| 5 | 第二次 llm 解析：點、段、day_note。推文 30 則一批，每批都附主文和第 4 步的結果；每批各自檢查和重問，還有問題的那幾筆拿掉；各批結果合併 | `parse_note.py`、`feedback.py`、`check.check_note`、`check.drop`、`prompts/note.md` |
| 6 | 兩次結果合成 `Parsed`，馬上存進 `v4_pending/` | `data_schema.Parsed.from_llm`、`pending.save` |
| 7 | 地點解析：這篇用到的地點分批（`是` 先、`猜的` 後，一批最多 10 個）一起問 llm，每個地點寫一段 log。解析不到、猜不出來的 place id 是 None | `geocoding.py`（用 `v4/place.py`、`local_map.py`、`ask_place.py`、`geocode_log.py`、`service/gsi.py`） |
| 8 | 整篇寫入（一個 transaction），解析不到的地點照樣寫入、place id 留空（見「寫入規則」），刪掉 pending | `store.write_article`、`pending.delete` |
| 9 | 整批跑完：終端機印幾行總結；完整的總結（地點解析的統計、解析不到的地點清單、llm 用量、各部分的 token 和時間佔比、花的時間）接在 `_run.log` 最後面。整批停下時結束代碼是 1 | `__main__.brief`、`geocode_log.summary_lines`、`geocode_log.dropped_lines`、`usage.lines` |

終端機只印少量資訊：開頭一行和 log 資料夾的位置、每篇寫入時一行、結束時幾行總結（見「命令列」），例如：

```
共 3 篇，略過 1 篇
log：data/logs/extract_20261120-140441/
[1/3] M.1767013758.A.A9D [問題] 東京五天行程：地點 18、天 5，解析不到 1
[2/3] ...
```

詳細進度寫在 `_run.log`，每一步一行，每篇開頭標 `[第幾篇/共幾篇] 文章 id`；同樣的進度行也寫進那一篇自己的檔。例如 `_run.log`：

```
共 3 篇，略過 1 篇
[1/3] M.1767013758.A.A9D llm 解析：[問題] 東京五天行程，推文 45 則
[1/3] M.1767013758.A.A9D llm 解析完成：地點 18、天 5，llm 2 次，2 分 05 秒
[1/3] M.1767013758.A.A9D 地點解析：15 個
[1/3] M.1767013758.A.A9D 寫入：地點解析 llm 9 次，1 分 12 秒，解析不到 1
```

接續上次的寫 `用 pending：標題`。每個地點一段只寫在那一篇的檔（見「看 log 找錯」），是做完的順序：同一批的地點一起問 llm，先找到的先寫，不一定照文章。llm 次數和時間是整批共用的，只在寫入那一行寫整篇的。

下一篇的 llm 解析在背景跑（`--jobs` 大於 1 時還會好幾篇同時），所以 `_run.log` 裡其他篇的「llm 解析／llm 解析完成」會穿插在地點解析之間，看開頭的 `[n/N]` 分辨；每篇自己的檔不會混到別篇。

`--verbose`：logger `v4` 的訊息一律接到 log（`pipeline` 開始時掛上 `GeocodeLog.handler()`，結束時拿掉），平常層級是 WARNING；`--verbose` 時 `__main__.py` 把它開到 INFO，就多了每次 llm 的輸入輸出和每一步的過程。訊息寫進哪個檔看現在在做哪一篇：`pipeline` 把每篇的 llm 解析（它自己的 asyncio task）、地點解析、寫入用 `GeocodeLog.article(文章 id)` 包住（`ContextVar`，每個 task 各自記），不在任何一篇裡的寫進 `_run.log`。終端機印的不管有沒有 `--verbose` 都一樣。

llm 用量：`service.llm` 每次呼叫把用量加進目前 `llm.track()` 開著的 `Tally`。`__main__.py` 包一層算總數，按 Ctrl-C 中斷時也印得出到中斷為止的用量。

各部分的 token 和時間（`usage.py`）：`pipeline.run` 開一份記錄（`usage.collect`），最底層的每段工作用 `usage.part(部分)` 包住，把那段的 llm 用量和時間加進那個部分。部分有：線（第一次解析）、點段 day_note（第二次解析）、搜尋計畫、挑候選、上網查地址（這五個有 llm）、查本機地圖、國土地理院、寫入資料庫。結果放在 `Report.parts`。時間是各部分各自累計的；llm 解析和地點解析會同時進行，所以加起來可能比整批花的時間長，時間佔比的分母是各部分的加總。

並行：第 4–6 步每篇一個 task，`--jobs` 是同時做的篇數（`asyncio.Semaphore`）。第 7–8 步照輸入的順序一篇一篇做；本機地圖的搜尋和國土地理院的查詢丟到 thread（搜尋要掃整張表、`service.gsi` 每秒限 1 次，等的時候不卡住其他篇的 llm 解析）。

開始時開本機地圖（唯讀）和這次的 log 資料夾，結束時關掉地圖。本機地圖檔不存在就什麼都不做，印「請先跑 scripts/geo/build_japan.py」，結束代碼 1。

按 Ctrl-C：已經寫入的保留，解析完的在 `v4_pending/`，終端機印「中斷」和到中斷為止的 llm 用量，結束代碼 130。

整批停下：`service.llm.LLMError`（含額度用完）、`service.gsi.GsiError`、`parse_line.ParseError`。`Report.error` 是原因，`Report.unfinished` 是還沒做完的文章。已經寫入的保留；已經解析完的留在 `v4_pending/`，重跑直接用。

### 地點解析的順序（`geocoding.Geocoder.resolve_article`）

流程圖：`docs/geocode_mvp.drawio`。只解析被行程、`place_note`、`move` 用到的地點。

分批：同一篇裡 `是` 先做、`猜的` 後做（這樣挑分店時同一天的其他地點已經有座標），同一種一批最多 `config.GEOCODE_BATCH`（10）個，一批做完才做下一批。
同一批的地點每一步一起問 llm：一次呼叫裡每個地點包成 `<item ref="地點編號">…</item>`，llm 回 `places` 陣列，一個地點一筆，用 `ref` 對回去（`ask_place._ask`）。
每一輪開始時重拿同一天的地點座標，所以這一輪找到的地點，同一批其他地點下一輪就看得到它的座標。

llm 漏回某個地點：搜尋計畫漏回的當成範圍不能用，跟範圍有問題的一起重問（`<error>` 寫「上一次的回覆裡沒有這個地點」）；挑哪一筆、上網查地址漏回的只拿漏的再問，最多 `config.RETRIES`（2）次，還是沒回就當成都不是／查不到。llm 回了不認得的編號不管。

每個地點（`located` 是 `是` 或 `猜的`）：

1. 查資料庫正名、別名（`place.find`）：`name_ja`、`name_zh` 其中一個相同且 `city` 相同，只查到一個才用。`猜的` 只用 `name_ja` 查。
2. 請 llm 給這一輪的搜尋計畫（`ask_place.plan`，`prompts/plan.md`）：幾個日文名稱（地圖上可能的寫法，例：「新宿駅」和「新宿」）和一個範圍，三種擇一：
   - 行政區：都道府縣，或都道府縣＋市區町村。用 `LocalMap.admin_area` 對到地圖裡的寫法。
   - 圓：同一天已經定位的某個地點為中心、半徑幾公里（`config.CIRCLE_KM`，0.3～10，超出就改成最小／最大值）。
   - 全日本。
   llm 看得到抽取時的 `name_ja`、`city`、句子、同一天的地點（有編號和座標），和之前每一輪的名稱、範圍、結果（AI 不選的理由也在）。
   範圍不能用（縣市對不到地圖、市區町村在好幾個縣都有、中心還沒定位、沒給名稱）就把原因告訴 llm 重問，最多 `config.RETRIES`（2）次；還是不能用，這一輪算 `範圍不能用`。
   名稱和範圍都跟之前某一輪一樣，這一輪就不查，算 `名稱重複`。
3. 在範圍內用這些名稱搜尋本機地圖（`LocalMap.search`，見下面「本機地圖」；傳入地點的種類，跟它一樣的排前面）。範圍內沒有同名的（完全相同或包含）就自動放寬一次：市區町村 → 整個縣、圓的半徑 × `config.WIDEN_FACTOR`（3）；放寬後有同名的才用放寬的結果。只有縣或全日本不放寬。
4. 有候選的地點一起問 llm 各是哪一筆（`ask_place.pick`，回 0 是都不是）。告訴它這次的範圍（放寬過也說）。每筆候選附上來源、類別、地址（跟範圍一樣的縣、市區町村不寫）、離圓心多遠（範圍是圓時）、離同一天已解析地點裡最近的多遠。不給座標。
5. 挑中：`place.add`（同一個來源 id 已在資料庫就回原本的 id），再加別名。`place_source` 寫候選的來源和來源 id；`name_ja`、種類、範圍用候選的；`city` 用 llm 抽取時給的。
6. 沒有候選或都不是：還沒滿 `config.GEOCODE_ROUNDS`（2）輪就回到 2（這一批還沒找到的一起）。
7. 滿 2 輪還沒有的一起請 llm 上網查地址（`ask_place.web_address`，開 `WebSearch`、`WebFetch`）。沒查到 → 解析不到，原因「上網查不到地址」。
8. 用國土地理院查這個地址，取第一筆（`service.gsi`）。沒有結果 → 解析不到，原因「國土地理院查不到這個地址」（llm 查到的地址和網址在 log 的「上網查地址」那行）。
9. 有結果就用：`source` 是 `gsi`、`source_id` 是 `<國土地理院回傳的地址>｜<name_ja>`；`name_ja`、種類用 llm 抽取時給的，範圍是空的，座標用國土地理院的。

`located` 是 `猜不出來`：不查資料庫也不查地圖，直接算解析不到，原因「猜不出來」。

解析不到的地點不用等使用者決定，照樣寫入，place id 留空（見「寫入規則」）。

不管結果如何，每個地點做完都寫一段 log（`geocode_log`）；llm 或國土地理院出錯時，這一批還沒做完的地點各寫一段 `中斷`，再讓例外往外丟。

### 本機地圖（`local_map.py`）

`data/geo/japan.duckdb` 一張 `place` 表（`local_map.SCHEMA`）：OSM 所有有 `name` 的物件（車站底下的月台、停車位置不收）和 Overture places（日本，已歇業的不收），每筆有來源、來源 id、名稱、比對用名稱（正規化後的所有名稱，含英文、中文名稱和機場代碼）、類別、種類（OSM 的 `place=*`、`boundary=administrative` 是區域，其他和 Overture 都是景點）、縣、市區町村、地址、代表點、範圍（區域而且是多邊形才有）、wikidata、維基百科重要度（0–1，沒有維基條目是 0）、面積（多邊形才有，其他是 0）、Overture 的 confidence。

名稱正規化（`normalize`，查詢和資料都用）：NFKC → 英文轉小寫 → 去掉空白（含全形）、`・`、`-`、`‐`、`－`（`ー` 保留）→ `ヶ`、`ヵ` 轉 `ケ`、`カ`。

一筆地點拿它每個比對用名稱跟查詢比，取最好的：

| 比對結果 | 條件 |
|---|---|
| 完全相同 | 兩個字串一樣 |
| 包含 | 地圖上的名稱包含查詢的名稱（反過來不算），而且查詢至少 2 個字 |
| 模糊 | DuckDB 的 `jaro_winkler_similarity` ≥ `config.FUZZY_THRESHOLD`（0.8） |

查詢可以給好幾個名稱，一筆地點取它跟每個名稱比的最好結果。範圍內只要有一筆完全相同或包含，模糊的就都不列（不然名稱只是像的雜訊會佔掉名額）。

範圍（`Area`）：都道府縣、都道府縣＋市區町村（比對地圖的 `pref`、`municipality` 欄位，這兩個欄位是建地圖時用座標對 OSM 行政區邊界算的，反映的是點實際所在的地方）、圓（中心點和半徑，用平面近似算距離），或全日本。政令指定都市在地圖裡只到「市」（「さいたま市」），東京 23 區是區。

`LocalMap.admin_area(pref, municipality)`：把 llm 寫的縣市對到地圖裡的寫法。比對時兩邊都去掉結尾的「都府県市区町村」，市區町村完全相同的優先（「湯沢町」不會對到秋田的「湯沢市」）。對不到、或沒寫縣而市區町村在好幾個縣都有，丟 `AreaError`，訊息列出名稱相近的市區町村，直接回給 llm。

排序照 Nominatim 的分數（`nominatim_api/search/geocoder.py`、`results.py`），越小越前面；OSM、Overture 一起排，取前 `config.CANDIDATES`（10）筆：

    分數 = 名稱扣分 − 維基百科重要度 ＋ 種類扣分

- 名稱扣分（`local_map.CONTAINS_PENALTY`）：完全相同 0，包含 0.5（Nominatim 只靠部分字詞對上時的扣分）。「包含」分不出是同一個地點的全名（查「上野動物園」，地圖上是「恩賜上野動物園」）還是別的地點（查「浅草」，對到「浅草橋」），所以包含的要比完全相同的有名 0.5 以上才排得到前面（查「東京」時的テレビ東京 0.711）。
- 重要度：同名的地點裡，有名的那個通常是旅遊文章講的（成田国際空港 0.595，位置登記錯的 Overture 那筆是 0）。沒有維基條目是 0。
- 種類扣分（`local_map.KIND_PENALTY`）：跟要找的種類（`search` 的 `kind`）不一樣加 0.5。同名的車站常比同名的地區有名（上野站 0.567、上野 0.506），找區域時不這樣會拿到車站。Nominatim 是直接過濾，這裡只扣分：地圖裡沒有那個區域時（嵐山），同名的車站（0.5 − 0.364）還是排在只包含名稱的小地名（0.5）前面。扣到 1.0 就會反過來。
- 同分：面積大的優先（有畫範圍的多邊形通常是地點本身，只有一個點的常是重複的資料或附屬的東西）→ 離圓心近的優先（範圍是圓時；店家、飯店大多沒有維基條目，靠這個排）→ 來源、來源 id 固定順序。

跟 Nominatim 不同的地方：模糊比對是這裡加的（Nominatim 沒有），只在範圍內沒有完全相同或包含時用，照相似度排、再照分數；Nominatim 會丟掉比第一名差 0.5 以上的，這裡不丟（後面還有 llm 挑，llm 種類判錯時正確的那筆會被丟掉）；Nominatim 給中心點時只看距離，這裡照樣看重要度；沒有維基條目時，Nominatim 會依地點層級給預設重要度，這裡是 0。

同一個 wikidata 是同一個地點（車站的好幾個節點、行政界和它的 `place` 節點），只留排最前面的一筆。

別名：

| 情況 | `place_alias_zh` | `place_alias_ja` |
|---|---|---|
| `是` | `name_zh` | `name_ja`（搜尋計畫裡的其他寫法不加，可能很籠統，例：「新宿」） |
| `猜的` | 不加，`place.name_zh` 也不填 | 同上 |

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
| `__main__.py` | 命令列 | `main(argv)` 回傳結束代碼、`brief`（終端機的總結）、`usage_lines` | `pipeline`、`geocode_log`、`store`、`config`、`service.llm` |
| `pipeline.py` | 主流程、印進度 | `collect`、`run(sources, db_path, jobs, limit, *, local_map, gsi, pending_dir, log_dir)` 回傳 `Report`；`PARSE_DROPPED`、`duration`、`count_text`、`shown_path` | 其他所有模組、`v4.db`、`service.llm` |
| `usage.py` | 各部分的 token 和時間 | `collect()`、`part(名稱)`、`lines(board, duration)`、`Part`；部分名稱 `LINE`、`NOTE`、`PLAN`、`PICK`、`WEB`、`MAP`、`GSI`、`WRITE` | `service.llm` |
| `config.py` | 路徑和設定 | `DB_PATH`、`PENDING_DIR`、`LOCAL_MAP_PATH`、`LOG_DIR`、`PROMPT_DIR`、`TIME_SLOTS_PATH`、`MODEL`、`EFFORT`、`RETRIES`、`GEOCODE_ROUNDS`、`GEOCODE_BATCH`、`CANDIDATES`、`CIRCLE_KM`、`WIDEN_FACTOR`、`FUZZY_THRESHOLD`、`COMMENTS_PER_BATCH` | — |
| `data_schema.py` | 各步驟之間傳的資料結構、給 llm 的輸出格式 | 見「資料怎麼傳」 | — |
| `article.py` | 整理文章 | `load(path)`、`Skipped`、`article_id_of`、`posted_at`、`merge_comments` | `data_schema` |
| `parse_line.py` | 第一次 llm 解析 | `parse(article)`、`build_messages`、`ParseError` | `feedback`、`check`、`prompts/line.md` |
| `parse_note.py` | 第二次 llm 解析：分批、合併 | `parse(article, line)` 回傳（合併後的輸出，不收入的筆數）、`batches`、`build_messages` | `feedback`、`check`、`prompts/note.md`、`time_slots.md` |
| `check.py` | 兩次解析的檢查 | `check_line(out)`、`check_note(out, line)`、`drop(out, problems)`、`Problem`、`time_slots()` | `data_schema`、`config` |
| `feedback.py` | 呼叫 llm、檢查、有問題重問 | `ask(system, user, schema, check, label, prompt, *, model, effort, retries)` 回傳（最後的輸出，還有的問題）；`model`、`effort`、`retries` 沒給時用 `config` 的（抽取都不給，`v4.rag` 給自己的）；`report(out, problems)`（附在重問後面的那段） | `llm_log` |
| `llm_log.py` | 呼叫 llm（`v4.extract` 呼叫 llm 都經過這裡），`--verbose` 時把輸入和輸出寫進 log 檔 | `call(system, user, schema, label, prompt, tools, *, model, effort)`；`model`、`effort` 沒給時用 `config` 的 | `service.llm`、`config` |
| `geocoding.py` | 地點解析 | `Geocoder(conn, local_map, gsi, log_file)`：`resolve_article(article_id, parsed, on_place)`、`resolve(place, ctx, on_done)`；`places_to_resolve(parsed)` | `v4.place`、`local_map`、`ask_place`、`geocode_log`、`service.gsi` |
| `local_map.py` | 本機地圖：名稱正規化、在範圍內模糊搜尋、縣市對到地圖的寫法 | `LocalMap(path)`：`search(names, area)`、`admin_area(pref, municipality)`、`close()`；`normalize`、`city_key`、`SCHEMA`、`MissingLocalMap`、`AreaError` | `duckdb` |
| `geocode_log.py` | 這次執行的 log 資料夾、地點解析的統計 | `GeocodeLog.start(folder)`：`folder`、`path`（`_run.log`）、`article_path`、`article`、`line`、`run_line`、`progress`、`handler`、`batch`、`write`、`traces`；`RUN_LOG`；`Trace`、`render`、`summarize`、`summary_lines(traces)`、`dropped_lines(traces)`；`result`、`outcome`、`fail_reason` 的值 | `data_schema` |
| `ask_place.py` | 請 llm 給搜尋計畫、問 llm 是哪一個、請 llm 上網查地址（都是一次好幾個地點） | `plan(list[PlanAsk])`、`pick(list[PickAsk])`、`web_address(list[WebAsk])`（都回傳 地點編號 → `Plan`／`Pick`／`WebAddress`）、`describe_candidate`、`nearest`、`center_km`、`km`、三個 `*_SCHEMA` | `llm_log`、`prompts/plan.md`、`prompts/pick.md`、`prompts/web_address.md` |
| `pending.py` | `v4_pending/` 的存、讀、刪 | `save`、`load`、`delete`、`path_of` | `data_schema` |
| `store.py` | 寫入 `place` 和別名表以外的表 | `has_article`、`row_id(article_id, n)`、`write_article(conn, article, parsed, place_ids)` 回傳拿掉的東西（`list[Dropped]`，一筆一個：種類 `DROPPED_KINDS` 只有 `day_note`，加上是哪一筆、為什麼拿掉） | `data_schema` |
| `prompts/*.md` | `line.md`、`note.md`、`plan.md`、`pick.md`、`web_address.md`，都是 system prompt | — | — |

外部服務在專案根目錄的 `service/`：`llm.py`（`call(system, user, schema, model, effort, tools)`、`LLMError`；`tools` 給了就開那些工具、直接執行不用確認；`track()` 開一個 `Tally` 累計區塊裡每次呼叫的次數、輸入／輸出 token、快取讀取的 token、照 API 定價估的金額）、`gsi.py`（國土地理院地址查詢，`GsiClient.search(address)` 回傳 `GsiHit` 或 None、`GsiError`）。

本機地圖由 `scripts/geo/build_japan.py` 建（手動跑一次，見「怎麼執行」）。

方向：`pipeline` 呼叫其他模組，其他模組不呼叫 `pipeline`；`data_schema`、`config` 不依賴其他模組。

## 資料怎麼傳

```
JSON ──article.load──▶ Article
Article ──parse_line.parse──▶ line（dict，照 LINE_SCHEMA）
Article + line ──parse_note.parse──▶ note（dict，照 NOTE_SCHEMA）、dropped
line + note ──Parsed.from_llm──▶ Parsed ◀──▶ v4_pending/<id>.json（Parsed.to_dict / from_dict）
Parsed ──Geocoder.resolve_article──▶ place_ids（地點編號 → place id；解析不到的是 None）
Article + Parsed + place_ids ──store.write_article──▶ 資料庫、拿掉的東西（list[Dropped]）
```

`Article`：`id`、`title`、`author`、`posted_at`（ISO 格式）、`text`、`comments`（`Comment`：`no` 從 1 編、`user`、`content`、`by_author`）。

llm 的輸出格式只在 `data_schema.py` 定義。所有欄位都是必填的字串，「留空」是空字串。

`LINE_SCHEMA`（為了少輸出比較精簡；`parse_line.expand` 檢查過後轉回 `sentence` 和 `{ref, type}`，第二次解析和後面拿到的都是轉回來的格式）：

| 欄位 | 內容 |
|---|---|
| `places[]` | 主文裡的地點，見下表 |
| `days[].label` | 原文對這一天的寫法，只用來對推文，不寫進資料庫 |
| `days[].month` | 1–12，或留空 |
| `days[].stops[]` | 照原文順序一站一個字串：景點 `p3`、住宿 `h3`（`STOP_PREFIX`）。轉回 `{ref, type}`（`景點`／`住宿`），`Day.stops` 是 `Stop`；`v4_pending/` 裡舊格式只有編號的當成景點 |

`places[]` 的一筆（兩個格式共用，只差在第一次給 `line`、第二次給 `sentence`）：

| 欄位 | 內容 |
|---|---|
| `ref` | 這篇文章裡的編號（`p1`、`p2`） |
| `name_zh`、`name_ja`、`city` | 繁中名稱、日文名稱、城市的日文名稱 |
| `kind` | `景點`／`區域`（llm 的看法；用本機地圖的候選時，寫進資料庫的種類由候選決定，上網查地址找到的才用它） |
| `located` | `是`／`猜的`／`猜不出來` |
| `reason` | `猜的` 寫依據，`猜不出來` 寫原因 |
| `sentence` | 原文提到它的那一句，照抄；地點解析時給 llm 看。第一次解析改給 `line`：主文的行號（輸入的主文每行前面標 `12｜`），由程式換成那一行的原文 |

`NOTE_SCHEMA`：

| 欄位 | 內容 |
|---|---|
| `places[]` | 推文新提到的地點 |
| `place_notes[]` | `place`（地點編號）、`content`、`times[]` |
| `moves[]` | `from`、`to`（地點編號）、`content`、`times[]` |
| `day_notes[]` | `days`（講的是第幾天，1 起算，由小到大；講整趟的是空陣列）、`content` |
| `times[]` 的一筆 | `year`、`month`、`day_type`、`time` |

`Parsed`：`places`（`PlaceRef`，兩次的合起來）、`days`（`Day`）、`place_notes`（`PlaceNote`）、`moves`（`Move`，`from`/`to` 改叫 `origin`/`dest`）、`day_notes`（`DayNote`）、`dropped`。

地點解析：`Candidate`（本機地圖的一筆：`source`、`source_id`、`name`、`category`、`kind`、`pref`、`municipality`、`address`、`lat`、`lon`、`boundary`、`match`、`similarity`）、`Context`（`article_id`、`same_day`）、`Resolution`（`place_id`，解析不到是 None）。

`Report`：`written`、`skipped`、`dropped`（拿掉沒寫入的東西 → 筆數：`store.DROPPED_KINDS` 加上 `pipeline.PARSE_DROPPED`）、`dropped_items`（寫入時拿掉的每一筆，`store.Dropped`）、`unfinished`、`error`、`log_dir`（這次的 log 資料夾）、`geocode_traces`（這次解析過的每個地點，`geocode_log.Trace`）、`parts`（各部分 → `usage.Part`：llm 用量和時間）、`seconds`。

## 規則

收入標準（寫在 `prompts/line.md`、`prompts/note.md`）：

- 講得出地點就收，不分這件事會不會隨時間改變。只抽日本的地點。
- 只有地名、沒有內容的不收。
- 原 po 的行程寫成 `day`、`day_place`，不另外寫成 `place_note`。
- 原文說某天要去的地點都列進那一天，包括還在考慮的、寫在主文其他段落的；明確只會去其中一個的（「A 或 B」）只列一個（看得出比較想去的，不然列先寫的）。
- 區域只能是行政區；車站、機場、飯店、商場、商店街是景點。
- 原 po 的推文當成主文的補充：可以產生 `place_note`、`move`，不產生 `day_note`。
- `move` 只有一端明確時，寫成 `place_note` 掛在明確的那一端。
- 欄位只放時空條件（地點、`times`、`days[].month`）。適合誰、交通方式這類其他條件沒有欄位，原文有講就寫在 `content` 裡。

`located`：

| 值 | 意思 | 處理 |
|---|---|---|
| `是` | 原文有名字，指得出是哪一個 | 走地點解析；本機地圖兩輪無結果、上網也查不到就是解析不到 |
| `猜的` | 連鎖店沒寫分店，`name_ja` 填猜的那一家 | 同上；`name_zh` 不拿來查也不加別名；挑分店時看離同一天其他地點的距離 |
| `猜不出來` | 沒有名字也沒有線索 | 直接算解析不到（行程裡照樣寫入，沒有 place）。不能被 `place_note`、`move` 引用 |

時間（`check.py` 檢查）：

- `time`：0–23 的一個鐘點或 `H-H`（跨夜寫 `18-4`）。只講時段照 `time_slots.md`：早上 `5-10`、中午 `11-13`、下午 `14-17`、晚上 `18-4`。
- `month`：1–12 的一個月份。季節要一個月給一筆（冬天是 `12`、`1`、`2` 三筆）。`days[].month` 只有一個值，只寫季節時留空。
- `year` 四位數，`day_type` 是 `平日`／`假日`。都可以留空。

檢查：

| 哪一次 | 檢查什麼 | 重問後還有問題 |
|---|---|---|
| 第一次（`check_line`） | `ref` 不重複、不是空的；`name_zh`、`name_ja` 不是空的；`ref` 是 `p` 加數字；`line` 是主文裡不是空的一行；`stops` 是 `p`／`h` 加數字、編號都在 `places` 裡；`month` 格式 | 丟 `ParseError`，整批停 |
| 第二次（`check_note`） | 新地點的 `ref` 不重複、也不跟第一次的重複，名稱不是空的；引用的地點編號存在、不是 `猜不出來` 的；`move` 的兩端不同；`day` 留空或對得到某一天；`content` 不是空的；`times` 各欄格式 | 那幾筆拿掉，算進 `解析檢查不過` 的筆數 |

第二次解析的分批（`parse_note.py`）：第一批的 `<scope>` 是「主文和這批推文」，之後是「只有這批推文」，主文只抽一次。各批的新地點編號會撞，合併時程式重新編號，五個欄位都相同的新地點共用一個編號。沒有推文也做一批。

寫入規則（`store.write_article`）：地點解析不到（本機地圖兩輪、上網都沒有）或 `猜不出來` 時照樣寫入：`day_place.poi_id`、`place_note.poi_id`、`move.from_id`／`to_id` 留空，`poi_name`、`from_name`、`to_name` 照原文。

| 情況 | 結果 |
|---|---|
| 沒有 `author`、讀不了、取不到文章 id、重複、資料庫已有 | 整篇略過 |
| 地點解析不到 | 照樣寫入，place id 留空 |
| `move` 兩端解析成同一個 place（兩端都解析不到不算） | 改寫成 `place_note` |
| 一天沒有任何一站 | 那一天不寫，講到那一天的 `day_note` 整筆不寫 |
| `times` 的一筆四欄都留空 | 那一筆不寫 |

拿掉的筆數分種類（`day_note`，加上第二次解析檢查不過的 `解析檢查不過`）記在 `Report.dropped`，每篇寫入時和結束時都印出來；寫入時拿掉的每一筆（`store.Dropped`）記在 `Report.dropped_items`，結束時一筆一行印在筆數下面，例如 `M.xxx day_note 「這天還沒排」：講 D3，D3 沒有站`。

## 資料庫

schema 在 `src/v4/schema.sql`。留空的欄位存 NULL。

`day`、`place_note`、`day_note`、`move` 的 `id` 是文字：文章 id 的時間戳加上第幾筆（`store.row_id`），例如文章 `M.1767020760.A.E80` 的 `1767020760-1`、`1767020760-2`。看到 id 就知道是哪一篇的第幾筆；引用它們的欄位（`day_place.day_id`、`day_note_day.note_id`／`day_id`、`place_note_time.note_id`、`move_time.move_id`）存同樣的字串。`day` 的第幾筆是行程裡的第幾天（跟 llm 輸出 `day_notes.days` 的編號一樣；沒有任何一站的那天不寫，編號會跳過），其他照寫入的順序。`place` 的 `id` 還是整數流水號。

id 是字串，照 `id` 排序時 `-10` 會排在 `-2` 前面；要照第幾天排用 `CAST(substr(id, instr(id, '-') + 1) AS INTEGER)`。同一秒發的兩篇文章時間戳會一樣，id 會撞到，寫入時被 PRIMARY KEY 擋下、整篇不寫（目前的 2000 篇沒有這種情況）。

看資料用的 view（`schema.sql` 最後面），多筆的東西合成 JSON 陣列，地點名稱優先用 `place.name_zh`、解析不到的用原文名稱：

| view | 一列是 | 多筆合成的欄位 |
|---|---|---|
| `place_note_view` | 一筆 place_note（加上文章標題、地點名稱） | `times`：`[{"year", "month", "day_type", "time"}]`，沒有時間是 `[]` |
| `day_note_view` | 一筆 day_note（加上文章標題） | `days`：講到的每一天照順序 `[{"day_id", "month", "stops": [{"seq", "poi_id", "name", "type"}]}]`，講整趟的是 `[]` |

要拆開查用 `json_each`，例如 `SELECT … FROM place_note_view, json_each(times)`。`db.connect` 不會改動已經存在的資料庫，舊的資料庫要自己跑一次 `schema.sql` 裡的 `CREATE VIEW`。

| 表 | 存什麼 | 哪裡寫 |
|---|---|---|
| `article` | 一篇一列：`id`、`title`、`posted_at` | `store.write_article` |
| `place` | 地點：`kind`、`name_ja`（地圖上的名稱；上網查到的用 llm 給的）、`name_zh`（第一次抽到的繁中名稱）、`city`（llm 給的）、座標、`boundary`（區域才有） | `geocoding` 透過 `place.add` |
| `place_source` | 地點的來源，`(source, source_id)` 不重複：`osm`（`node/123`、`way/456`、`relation/789`）、`overture`（GERS id）、`gsi`（`<國土地理院回傳的地址>｜<name_ja>`，同一個地址可能是同一棟樓的不同店，所以加上名稱）、`manual`（以前使用者填的座標，現在抽取不會再產生） | 跟 `place` 同一個 transaction 寫入 |
| `place_alias_ja`、`place_alias_zh` | 別名，同一個 place 不重複 | `geocoding` 透過 `place.add_alias` |
| `day` | 行程的一天；`id` 的 `-` 後面是第幾天 | `store.write_article` |
| `day_place` | 一天裡的站，`seq` 是原文的第幾站；`poi_name` 是這篇文章的寫法；`type` 是 `景點` 或 `住宿`；`poi_id` 留空是解析不到 | 同上 |
| `day_note` | 推文對某幾天或整趟的意見 | 同上 |
| `day_note_day` | `day_note` 講的是哪幾天（`note_id`、`day_id`）；一筆都沒有的是講整趟 | 同上 |
| `place_note`、`place_note_time` | 一個地點的經驗；`poi_name` 是這篇文章的寫法；`poi_id` 留空是解析不到 | 同上 |
| `move`、`move_time` | 兩地之間的移動經驗；`from_name`、`to_name` 是這篇文章的寫法；`from_id`、`to_id` 留空是那一端解析不到 | 同上 |

`place` 和別名在地點解析時就寫進去（各自 commit），所以整批停下時已經解析出來的地點會先留在資料庫，重跑時資料庫直接命中。其他表一篇一個 transaction。

## 怎麼執行、怎麼測試

先建本機地圖（只要一次；換 OSM 或 Overture 版本時重跑）：

```bash
brew install osmium-tool
# 從 https://download.geofabrik.de/asia/japan.html 下載 japan-latest.osm.pbf
# 從 https://nominatim.org/data/wikimedia-importance.csv.gz 下載維基百科重要度，放到 data/geo/src/
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
| `--verbose`、`-v` | 在 log 檔另外寫每次 llm 呼叫的輸入（system prompt 同一段只印第一次，之後寫「跟 llm #N 一樣」；user prompt 全文）和輸出（JSON），用 `llm #N` 對起輸入和輸出；還有每一步的過程：檢查出的問題和第幾次重試、被拿掉的資料、每個地點查本機地圖的候選、llm 挑選的理由、上網查到的地址 |

終端機印的進度見「流程」。結束時在終端機印幾行總結（`__main__.brief`），錯誤印在 stderr：

```
寫入 3 篇、略過 1 篇、拿掉沒寫入 1 筆
地點 15 個，解析不到 2 個
llm 52 次，約 US$3.21
花費時間：12 分 30 秒
log：data/logs/extract_20261120-140441/
```

「拿掉沒寫入」是 0 筆時不印。結束代碼：0 是跑完；整批停下或沒有本機地圖是 1；按 Ctrl-C 是 130。

完整的總結接在 `_run.log` 最後面（`========== 總結 ==========` 之後；錯誤訊息也在裡面），地點解析的統計和解析不到的地點用 `Report.geocode_traces` 算：

```
寫入：3 篇
略過：1 篇
  資料庫已有：1 篇
拿掉沒寫入：1 筆（day_note 1）
  M.1767013758.A.A9D day_note 「這天還沒排」：講 D3，D3 沒有站

地點解析：共 N 個
  資料庫 n、地圖-第1輪 n、地圖-第2輪 n、上網 n、解析不到 n
  解析不到的原因：上網查不到地址 n、國土地理院查不到這個地址 n、猜不出來 n
  本機地圖每輪失敗：沒有候選 n、AI 都不選 n
解析不到的地點（2 個）：
  M.1767013758.A.A9D 築地場外市場（築地場外市場）：上網查不到地址
  M.1767013758.A.A9D 飯店（ホテル）：猜不出來

llm 用量：共 52 次，輸入 1,234,567 token（其中快取讀取 800,000）、輸出 23,456 token
  約 US$3.21
各部分：
  線（第一次解析）：llm 3 次，token 180,000（15%），US$0.45，時間 2 分 10 秒（14%）
  點、段、day_note（第二次解析）：llm 3 次，token 420,000（34%），US$1.10，時間 4 分 20 秒（29%）
  地點解析：搜尋計畫：llm 20 次，…
  地點解析：挑候選：llm 18 次，…
  地點解析：上網查地址：llm 8 次，…
  查本機地圖：時間 40 秒（4%）
  國土地理院：時間 12 秒（1%）
  寫入資料庫：時間 0 秒（0%）
花費時間：12 分 30 秒
```

按 Ctrl-C 中斷時不寫總結，終端機印出到中斷為止的 llm 用量。

統計裡固定的項目沒有也印 0；其他有出現的（例如 `中斷`、`名稱重複`）接在那一行後面。輸入 token 含快取讀寫；金額是 CLI 照 API 定價估的，走訂閱額度時不會真的收。各部分的 token 是輸入＋輸出；時間是各部分各自累計，llm 解析和地點解析會同時進行，所以佔比的分母是各部分的加總。

`v4_pending/`、本機地圖、log 資料夾的位置在 `config.py`，命令列不能改。

```bash
.venv/bin/python -m pytest tests/v4 -q
```

測試不打網路。`tests/v4/extract/fakes.py` 有小型本機地圖（`build_map`：表結構跟真的一樣，十幾筆手寫的資料）、假的國土地理院（`FakeGsi`：固定幾個地址）和假的 llm（看輸出格式分辨是哪一種呼叫：line、note、plan、pick、web；plan、pick、web 每個 `<item>` 各拿一個值回一筆，值是 `fakes.SKIP` 就漏回那個地點；搜尋計畫沒指定時用 `default_plan`：日文名稱一個、城市當都道府縣；每次呼叫記一份固定的用量 `fakes.USAGE`）；`tests/v4/extract/conftest.py` 把 `service.llm.call` 換成假的（`use_llm`）、提供 `local_map` fixture，並把 `config` 的資料庫、pending、本機地圖、log 路徑換到暫存資料夾，測試不會碰到真的 `data/`。`place`、`db` 的測試在 `tests/v4/`。

## 看 log 找錯

每次執行開一個資料夾 `data/logs/extract_<開始時間 YYYYmmdd-HHMMSS>/`（同一秒有兩次就加 `-2`），裡面都是純文字，直接打開看，跑的時候可以 `tail -f`。每寫一次就開檔寫完關掉，整批停下前寫的都在。

- `_run.log`：整批的進度（見「流程」）和最後的總結。
- `<文章 id>.log`：一篇一個。那一篇的進度、每個地點一段（下面）；`--verbose` 時還有這篇的每次 llm 輸入輸出（`━━━━━ llm #N 輸入／輸出` 對起來，編號整批共用）和每一步的過程。

每篇的檔第一行是 `========== 文章 id ==========`；一批一起問 llm 的地點先寫一行 `一批 N 個：…（是）`；每個地點做完寫一段，例如：

```
[p3] 淺草寺｜浅草寺｜東京｜景點｜是　→　地圖-第1輪：浅草寺
  句子：第一天先去淺草寺
  同一天：晴空塔（35.7101, 139.8107）、上野（還沒定位）
  第 1 輪：名稱 浅草寺｜範圍 東京 台東区｜挑中
    理由：淺草寺在台東区
    實際查：東京都 台東区，2 筆候選
    → 1. 浅草寺｜OSM｜amenity=place_of_worship｜台東区 浅草2-3-1｜離「晴空塔」1.4 公里｜35.7148, 139.7967｜way/1｜完全相同｜重要度 0.46
      2. 浅草寺｜Overture｜buddhist_temple｜…｜ovt-sensoji｜完全相同｜重要度 0.00
    AI 挑第 1 筆：名稱和位置都對
  用：place 12 浅草寺（osm way/1）https://www.google.com/maps?q=35.7148,139.7967
  llm：2 次
```

`同一天` 沒有其他地點時不寫這行；`實際查` 後面有 `（放寬）` 是範圍內沒有同名的、自動放寬過。

第一行：抽取時的 `name_zh｜name_ja｜city｜kind｜located`，箭頭後面是結果：

| 結果 | 意思 | 接著看 |
|---|---|---|
| `資料庫` | 資料庫已經有（正名或別名） | `用：` 那行是不是對的地點。錯的話是之前加錯了別名 |
| `地圖-第1輪`、`地圖-第2輪` | 本機地圖在第幾輪找到 | 最後一輪的候選（`→` 是 AI 挑的）和理由；`用：` 那行的 Google 地圖連結點開看位置對不對 |
| `上網` | 上網查地址找到 | `上網查地址`：地址和網址對不對；`國土地理院`：是不是同一個地方（國土地理院可能只對到町） |
| `解析不到（原因）` | 找不到，照樣寫入，place id 留空 | 原因是 `上網查不到地址`、`國土地理院查不到這個地址`、`猜不出來`；再看每一輪 |
| `中斷（錯誤訊息）` | llm 或國土地理院出錯，整批停下 | 錯誤訊息 |

每一輪：`名稱`、`範圍` 是 llm 的搜尋計畫，後面是這一輪的結果（`挑中`／`沒有候選`／`AI 都不選`／`名稱重複`／`範圍不能用`）；`範圍不能用：` 是重問完還不能用的原因；`實際查：` 是對到地圖的範圍（自動放寬過會寫）；候選一筆一行（跟給 llm 的編號一樣，照搜尋的排序）：名稱｜來源｜類別｜地址｜離中心多遠（範圍是圓時）｜離同一天最近的地點多遠｜座標｜來源 id｜比對結果｜維基百科重要度。前面到「離同一天最近的地點」是 llm 看到的。

`llm：N 次` 是這個地點參與的呼叫次數；同一批一起問的地點各算一次，所以全部加起來會比實際呼叫多（實際的看結束時印的 llm 用量）。

常見的錯：

- 挑錯了：候選裡有對的那筆，但 `→` 指到別筆 → 看 `AI 挑第 N 筆：` 的理由，改 `prompts/pick.md`。
- 對的地點沒出現在候選裡：每一輪都 `沒有候選`，或候選全是 `模糊` 的不相干地點 → 看名稱是不是漏了地圖上的寫法、`實際查` 是不是範圍錯了（改 `prompts/plan.md`），或本機地圖沒有這個地點。
- 挑到位置錯的資料：看那筆的「離「X」」、重要度，和其他同名候選的座標。
- `範圍不能用` 很多：看原因，llm 常寫錯的縣市寫法；寫「上一次的回覆裡沒有這個地點」是 llm 漏回。
- `名稱重複` 很多：llm 給不出新的名稱或範圍。
- `上網` 的座標偏掉：`國土地理院` 只到町或丁目，表示沒對到番地，座標是那一區的代表點。

## 要改某件事時去哪個檔案

| 要改什麼 | 動哪裡 |
|---|---|
| 提示詞（收入標準、怎麼分三種、範例） | `prompts/*.md` |
| 模型、effort、重試次數、查本機地圖幾輪、地點解析一次問幾個地點、取幾筆候選、圓的半徑範圍、放寬倍數、模糊比對門檻、一批幾則推文 | `config.py` |
| 加一條檢查 | `check.py` 加在 `check_line` 或 `check_note`，回傳 `Problem(kind, index, message)`；`tests/v4/extract/test_check.py` 加一個例子 |
| llm 輸出加欄位 | `data_schema.py` 的 schema 和對應的 dataclass（`Parsed.from_llm`、`from_dict` 會跟著吃）→ 提示詞 → `check.py` → `store.py`；要存進資料庫再改 `src/v4/schema.sql`。舊的 `v4_pending/` 檔案沒有新欄位，要刪掉重抽 |
| 時段對應的鐘點 | `src/v4/time_slots.md`（提示詞和檢查都讀它） |
| 地點解析的順序、別名怎麼加 | `geocoding.py` 的 `resolve`、`_add` |
| 搜尋計畫怎麼給（名稱寫法、範圍的規則） | `prompts/plan.md`、`ask_place.plan`；範圍怎麼檢查在 `geocoding._area`，自動放寬在 `geocoding._widen` |
| 給 llm 挑地點時附什麼資訊 | `ask_place.py` 的 `_describe`、`describe_candidate`，和 `prompts/pick.md` |
| 上網查地址的規則 | `prompts/web_address.md`、`ask_place.web_address` |
| 名稱正規化、比對方式、排序、範圍、縣市怎麼對 | `local_map.py` 的 `normalize`、`city_key`、`_search_sql`、`admin_area` |
| 本機地圖收哪些資料、哪些算區域、縣市層級、Overture 版本 | `scripts/geo/build_japan.py`（改完重建） |
| 國土地理院的參數、限速 | `service/gsi.py` |
| log 寫成什麼樣子、統計 | `geocode_log.py`（一個地點那一段在 `render`），哪一步填什麼在 `geocoding.py` |
| 解析不到的地點怎麼寫入、`move` 改寫 | `store.write_article` |
| 分批、一批幾個、`猜的` 排在後面 | `geocoding._batches`、`config.GEOCODE_BATCH` |
| 終端機印的進度 | `pipeline.py` 的 `tell` 和寫入後那行 `print`；結束時的總結在 `__main__.brief` |
| log 裡進度的每一行 | `pipeline.py`（`say`、`_place_line`） |
| 各部分的 token 和時間怎麼分、怎麼印 | `usage.py`；哪段工作算哪個部分看 `usage.part(...)` 包在哪（`pipeline`、`ask_place._ask`、`geocoding`） |
| 查正名、別名的方式 | `src/v4/place.py` |
| 推文怎麼合併、發文時間怎麼讀 | `article.py` |
| 哪些文章略過、並行、什麼時候整批停 | `pipeline.py` |
| 印出的結果、結束代碼、命令列參數 | `__main__.py` |
| `--verbose` 寫的訊息 | llm 的輸入輸出在 `llm_log.py`；其他是各模組的 `log.info(...)`（`logging`，logger 名稱是 `v4.extract.<模組>`）；接到 log 在 `pipeline._run`（`GeocodeLog.handler`，寫進哪一篇的檔看 `GeocodeLog.article`），`__main__.py` 只負責開層級 |
