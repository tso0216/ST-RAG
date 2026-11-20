-- v4 資料庫：點（place_note）、線（day、day_place、day_note、day_note_day）、段（move）；最後面是看資料用的 view
-- 欄位只放時空條件（地點、時間）；適合誰、交通方式這類其他條件寫在 content 裡
-- day、place_note、day_note、move 的 id 是「文章 id 的時間戳-第幾筆」，例如文章 M.1767020760.A.E80 的 1767020760-1：
--   day 的第幾筆是行程裡的第幾天（沒有任何一站的那天不寫，編號會跳過）；其他照寫入的順序。
--   引用它們的欄位（day_place.day_id、day_note_day、place_note_time.note_id、move_time.move_id）存同樣的字串。

CREATE TABLE article (
    id        TEXT PRIMARY KEY,
    title     TEXT NOT NULL,
    posted_at TEXT NOT NULL
);

CREATE TABLE place (
    id         INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL CHECK (kind IN ('景點','區域')),
    name_ja    TEXT    NOT NULL,
    name_zh    TEXT,                              -- 第一次抽到的繁中名稱，顯示用
    city       TEXT    NOT NULL DEFAULT '',
    lat        REAL    NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon        REAL    NOT NULL CHECK (lon BETWEEN -180 AND 180),
    boundary   TEXT                               -- 區域的範圍（geojson）
);

-- 地點的來源：osm（node/123、way/456、relation/789）、overture（GERS id）、
-- gsi（<國土地理院回傳的地址>｜<name_ja>）、manual（使用者填的座標）
CREATE TABLE place_source (
    place_id  INTEGER NOT NULL REFERENCES place (id),
    source    TEXT    NOT NULL CHECK (source IN ('osm','overture','gsi','manual')),
    source_id TEXT    NOT NULL,
    UNIQUE (source, source_id)
);

CREATE TABLE place_alias_ja (
    id    INTEGER NOT NULL REFERENCES place (id),
    alias TEXT    NOT NULL,
    UNIQUE (id, alias)
);

CREATE TABLE place_alias_zh (
    id    INTEGER NOT NULL REFERENCES place (id),
    alias TEXT    NOT NULL,
    UNIQUE (id, alias)
);

-- 點：景點、區域的經驗
-- poi_id、day_place.poi_id、move.from_id／to_id 留空：地點解析不到（沒有 place），只有原文的名稱
CREATE TABLE place_note (
    id         TEXT    PRIMARY KEY,
    article_id TEXT    NOT NULL REFERENCES article (id),
    poi_id     INTEGER REFERENCES place (id),
    poi_name   TEXT    NOT NULL,
    content    TEXT
);

CREATE TABLE place_note_time (
    note_id    TEXT    NOT NULL REFERENCES place_note (id),
    year       INTEGER,
    month      TEXT,
    day_type   TEXT,
    time       TEXT
);

-- 線：行程（以一天為單位）
CREATE TABLE day (
    id         TEXT    PRIMARY KEY,
    article_id TEXT    NOT NULL REFERENCES article (id),
    month      TEXT
);

-- 線：行程中的點
CREATE TABLE day_place (
    day_id   TEXT    NOT NULL REFERENCES day (id),
    seq      INTEGER NOT NULL,
    poi_id   INTEGER REFERENCES place (id),
    poi_name TEXT    NOT NULL,
    type     TEXT    NOT NULL CHECK (type IN ('景點','住宿')),   -- 這一站是去玩的還是住的
    PRIMARY KEY (day_id, seq)
);

CREATE TABLE day_note (
    id         TEXT    PRIMARY KEY,
    article_id TEXT    NOT NULL REFERENCES article (id),
    content    TEXT    NOT NULL
);
-- day_note 講的是哪幾天；沒有任何一筆是講整趟
CREATE TABLE day_note_day (
    note_id TEXT NOT NULL REFERENCES day_note (id),
    day_id  TEXT NOT NULL REFERENCES day (id),
    PRIMARY KEY (note_id, day_id)
);

-- 段：A 到 B 的移動經驗
CREATE TABLE move (
    id         TEXT    PRIMARY KEY,
    article_id TEXT    NOT NULL REFERENCES article (id),
    from_id    INTEGER REFERENCES place (id),
    to_id      INTEGER REFERENCES place (id),
    from_name  TEXT    NOT NULL,
    to_name    TEXT    NOT NULL,
    content    TEXT    NOT NULL
);

CREATE TABLE move_time (
    move_id    TEXT    NOT NULL REFERENCES move (id),
    year       INTEGER,
    month      TEXT,
    day_type   TEXT,
    time       TEXT
);

-- ---------- 看資料用的 view ----------
-- 地點名稱優先用 place 的繁中名稱，解析不到的（沒有 place）用原文的名稱。
-- 多筆的東西合成 JSON 陣列，要拆開查用 json_each，例如 SELECT … FROM place_note_view, json_each(times)。

-- 一筆 place_note 一列；times：[{"year", "month", "day_type", "time"}, …]，沒有時間是 []
CREATE VIEW place_note_view AS
SELECT n.id, n.article_id, a.title, n.poi_id,
       COALESCE(p.name_zh, n.poi_name) AS poi_name, n.content,
       (SELECT json_group_array(json_object('year', t.year, 'month', t.month,
                                            'day_type', t.day_type, 'time', t.time) ORDER BY t.rowid)
          FROM place_note_time t WHERE t.note_id = n.id) AS times
FROM place_note n
JOIN article a ON a.id = n.article_id
LEFT JOIN place p ON p.id = n.poi_id;

-- 一筆 day_note 一列；days：講到的每一天照順序 [{"day_id", "month", "stops": [{"seq", "poi_id", "name", "type"}, …]}, …]，
-- 講整趟的是 []。天照 id 後面的數字排（第 10 天排在第 2 天後面）。
CREATE VIEW day_note_view AS
SELECT n.id, n.article_id, a.title, n.content,
       (SELECT json_group_array(json_object(
                  'day_id', d.id, 'month', d.month,
                  'stops', json((SELECT json_group_array(json_object(
                                'seq', dp.seq, 'poi_id', dp.poi_id,
                                'name', COALESCE(p.name_zh, dp.poi_name), 'type', dp.type) ORDER BY dp.seq)
                              FROM day_place dp LEFT JOIN place p ON p.id = dp.poi_id
                             WHERE dp.day_id = d.id))
               ) ORDER BY CAST(substr(d.id, instr(d.id, '-') + 1) AS INTEGER))
          FROM day_note_day nd JOIN day d ON d.id = nd.day_id
         WHERE nd.note_id = n.id) AS days
FROM day_note n
JOIN article a ON a.id = n.article_id;
