-- v4 資料庫：點（place_note）、線（day、day_place、day_note）、段（move）
-- 欄位只放時空條件（地點、時間）；適合誰、交通方式這類其他條件寫在 content 裡

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
CREATE TABLE place_note (
    id         INTEGER PRIMARY KEY,
    article_id TEXT    NOT NULL REFERENCES article (id),
    poi_id     INTEGER NOT NULL REFERENCES place (id),
    poi_name   TEXT    NOT NULL,
    content    TEXT
);

CREATE TABLE place_note_time (
    note_id    INTEGER NOT NULL REFERENCES place_note (id),
    year       INTEGER,
    month      TEXT,
    day_type   TEXT,
    time       TEXT
);

-- 線：行程（以一天為單位）
CREATE TABLE day (
    id         INTEGER PRIMARY KEY,
    article_id TEXT    NOT NULL REFERENCES article (id),
    month      TEXT
);

-- 線：行程中的點
CREATE TABLE day_place (
    day_id INTEGER NOT NULL REFERENCES day (id),
    seq    INTEGER NOT NULL,
    poi_id INTEGER NOT NULL REFERENCES place (id),
    PRIMARY KEY (day_id, seq)
);

CREATE TABLE day_note (
    id         INTEGER PRIMARY KEY,
    article_id TEXT    NOT NULL REFERENCES article (id),
    day_id     INTEGER REFERENCES day (id),       -- 留空是講整趟
    content    TEXT    NOT NULL
);

-- 段：A 到 B 的移動經驗
CREATE TABLE move (
    id         INTEGER PRIMARY KEY,
    article_id TEXT    NOT NULL REFERENCES article (id),
    from_id    INTEGER NOT NULL REFERENCES place (id),
    to_id      INTEGER NOT NULL REFERENCES place (id),
    from_name  TEXT    NOT NULL,
    to_name    TEXT    NOT NULL,
    content    TEXT    NOT NULL
);

CREATE TABLE move_time (
    move_id    INTEGER NOT NULL REFERENCES move (id),
    year       INTEGER,
    month      TEXT,
    day_type   TEXT,
    time       TEXT
);
