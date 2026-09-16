-- Chess Trainer schema. Version lives in meta; migrations are by version number.

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Analysis cache. The key includes depth, MultiPV and engine version, so a
-- lookup at depth 20 never silently serves a depth 12 result.
CREATE TABLE IF NOT EXISTS analysis (
  pos_hash     INTEGER NOT NULL,
  depth        INTEGER NOT NULL,
  multipv      INTEGER NOT NULL,
  engine_ver   TEXT    NOT NULL,
  fen          TEXT    NOT NULL,
  lines        TEXT    NOT NULL,   -- JSON: [{move, cp, mate, wdl, pv}, ...]
  computed_at  INTEGER NOT NULL,
  PRIMARY KEY (pos_hash, depth, multipv, engine_ver)
);

-- Real answers only. Navigation, skips and mode switches record nothing.
CREATE TABLE IF NOT EXISTS answers (
  id            INTEGER PRIMARY KEY,
  pos_hash      INTEGER NOT NULL,   -- position AFTER the opponent's move
  root_hash     INTEGER NOT NULL,   -- the drill's starting position
  depth_level   INTEGER NOT NULL,   -- 0 = drill root, 1 = one Drill-From-Here deeper
  opp_move      TEXT    NOT NULL,   -- UCI
  my_move       TEXT,               -- UCI; NULL when shown
  delta_wp      REAL,               -- win-probability loss vs best, in points
  verdict       TEXT    NOT NULL,   -- best | second | ... | blunder | shown
  answered_at   INTEGER NOT NULL,
  step          INTEGER NOT NULL DEFAULT 1   -- which move of the chain
);
CREATE INDEX IF NOT EXISTS answers_pos ON answers(pos_hash);
CREATE INDEX IF NOT EXISTS answers_root ON answers(root_hash, depth_level);

CREATE TABLE IF NOT EXISTS games (
  id          INTEGER PRIMARY KEY,
  url         TEXT UNIQUE,
  white       TEXT,
  black       TEXT,
  result      TEXT,
  white_elo   INTEGER,
  black_elo   INTEGER,
  time_class  TEXT,
  played_at   INTEGER,
  pgn         TEXT NOT NULL,
  my_colour   TEXT            -- 'white' | 'black' | NULL for a foreign game
);

-- The drillable pools.
CREATE TABLE IF NOT EXISTS positions (
  id          INTEGER PRIMARY KEY,
  pos_hash    INTEGER NOT NULL,
  fen         TEXT NOT NULL,
  phase       TEXT NOT NULL,       -- opening | middlegame | endgame
  source_game INTEGER REFERENCES games(id),
  ply         INTEGER,
  eval_cp     INTEGER,             -- from your side
  tail_san    TEXT,                -- last few moves leading here
  my_colour   TEXT,                -- 'white' | 'black' (the side you answer as)
  name        TEXT,                -- opening title, or a label
  UNIQUE (pos_hash, phase)
);
CREATE INDEX IF NOT EXISTS positions_phase ON positions(phase);
CREATE INDEX IF NOT EXISTS positions_name ON positions(name);

-- Path-keyed, session-persistent drill trees.
CREATE TABLE IF NOT EXISTS tree_nodes (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL,
  root_hash   INTEGER NOT NULL,
  parent_id   INTEGER REFERENCES tree_nodes(id),
  move_uci    TEXT,                -- move that reached this node from the parent
  pos_hash    INTEGER NOT NULL,
  fen         TEXT NOT NULL,
  depth_level INTEGER NOT NULL DEFAULT 0,
  visit_count INTEGER NOT NULL DEFAULT 1,
  first_seen  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tree_root ON tree_nodes(session_id, root_hash);
CREATE INDEX IF NOT EXISTS tree_parent ON tree_nodes(parent_id);

CREATE TABLE IF NOT EXISTS saved (
  id        INTEGER PRIMARY KEY,
  fen       TEXT NOT NULL,
  name      TEXT,
  saved_at  INTEGER NOT NULL,
  from_root INTEGER             -- root_hash of the drill it was saved from
);

-- Draw history, for the recency penalty in weighted replay.
CREATE TABLE IF NOT EXISTS draws (
  id        INTEGER PRIMARY KEY,
  pos_hash  INTEGER NOT NULL,
  phase     TEXT NOT NULL,
  drawn_at  INTEGER NOT NULL
);

-- Engine review of whole games: one row per move, from the mover's side.
CREATE TABLE IF NOT EXISTS reviews (
  game_id      INTEGER PRIMARY KEY REFERENCES games(id),
  depth        INTEGER NOT NULL,
  engine_ver   TEXT NOT NULL,
  reviewed_at  INTEGER NOT NULL,
  accuracy     REAL,               -- your accuracy in this game, 0-100
  plies        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS review_moves (
  id           INTEGER PRIMARY KEY,
  game_id      INTEGER NOT NULL REFERENCES games(id),
  ply          INTEGER NOT NULL,   -- 1 = White's first move
  is_me        INTEGER NOT NULL,   -- 1 for your moves
  fen          TEXT NOT NULL,      -- position before the move
  move         TEXT NOT NULL,      -- what was played, UCI
  best         TEXT,               -- what the engine wanted
  wp_before    REAL,               -- win probability before, mover's side
  wp_after     REAL,               -- after the move played
  delta_wp     REAL,
  verdict      TEXT NOT NULL,
  accuracy     REAL,               -- this move's accuracy, 0-100
  mate_in      INTEGER,            -- forced mate available to the mover, if any
  kept_mate    INTEGER,            -- 1 if the move played still forces mate
  phase        TEXT NOT NULL,
  themes       TEXT NOT NULL,      -- JSON: what the best move was about
  allowed      TEXT                -- JSON: what the reply could then do, when
                                   -- the move played was a mistake or worse
);
CREATE INDEX IF NOT EXISTS review_moves_game ON review_moves(game_id, ply);
CREATE INDEX IF NOT EXISTS review_moves_me ON review_moves(is_me, verdict);

-- Explanations worked out during review, where there is time to search
-- properly. The drill panel reads them instead of recomputing in the
-- half second it has.
CREATE TABLE IF NOT EXISTS explanations (
  pos_hash     INTEGER NOT NULL,
  best_move    TEXT NOT NULL,      -- the move being explained, UCI
  depth        INTEGER NOT NULL,   -- what the claims were checked at
  engine_ver   TEXT NOT NULL,
  items        TEXT NOT NULL,      -- JSON list of claims
  computed_at  INTEGER NOT NULL,
  PRIMARY KEY (pos_hash, best_move)
);

-- What a drilled position is about, for the gym statistics.
CREATE TABLE IF NOT EXISTS position_themes (
  pos_hash     INTEGER PRIMARY KEY,
  themes       TEXT NOT NULL,      -- JSON list
  computed_at  INTEGER NOT NULL
);
