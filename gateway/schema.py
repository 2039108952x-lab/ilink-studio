"""迁移序列（只向前）。

v1 是 PRD §7.1 的逻辑模型。字段名就是实施基线：可以加索引和审计字段，
但不能削弱这里的约束（唯一键、CHECK、FTS 触发器）。

__pycache__ 里没有秘密：这里只写 DDL，凭据永远不进这张库。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


_INITIAL = """
-- 接入槽位（M0 已建过同结构表；这里 IF NOT EXISTS 让旧开发库原地升级）
CREATE TABLE IF NOT EXISTS account (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,
  channel TEXT NOT NULL,
  platform_uid TEXT NOT NULL DEFAULT '',
  display_name TEXT,
  auth_state TEXT NOT NULL DEFAULT 'unbound',
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
  expected_uid TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  connection_state TEXT NOT NULL DEFAULT 'stopped',
  send_health TEXT NOT NULL DEFAULT 'unknown',
  last_auth_ok_at REAL,
  last_probe_at REAL,
  last_send_at REAL,
  last_error_code TEXT,
  last_send_error_safe TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(platform, channel)
);

CREATE TABLE IF NOT EXISTS app_setting (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE person (
  id INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL,
  is_self INTEGER NOT NULL DEFAULT 0 CHECK (is_self IN (0,1)),
  note TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE identity (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES person(id),
  platform TEXT NOT NULL,
  platform_uid TEXT NOT NULL,
  id_kind TEXT NOT NULL CHECK (id_kind IN ('stable','opaque','provisional')),
  display_name TEXT,
  is_bot INTEGER NOT NULL DEFAULT 0 CHECK (is_bot IN (0,1)),
  created_at REAL NOT NULL,
  UNIQUE(platform, id_kind, platform_uid)
);

CREATE TABLE thread (
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('private','group','comment')),
  person_id INTEGER REFERENCES person(id),
  title TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  CHECK ((scope='private' AND person_id IS NOT NULL)
      OR (scope IN ('group','comment') AND person_id IS NULL))
);
CREATE UNIQUE INDEX ux_private_thread
  ON thread(person_id) WHERE scope='private';

CREATE TABLE conversation (
  id INTEGER PRIMARY KEY,
  thread_id INTEGER NOT NULL REFERENCES thread(id),
  account_id INTEGER NOT NULL REFERENCES account(id),
  kind TEXT NOT NULL CHECK (kind IN ('private','group','comment')),
  chat_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(account_id, kind, chat_id)
);

CREATE TABLE message (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversation(id),
  account_id INTEGER NOT NULL REFERENCES account(id),
  platform TEXT NOT NULL,
  platform_msg_id TEXT,
  dedup_key TEXT,
  direction TEXT NOT NULL CHECK (direction IN ('in','out')),
  sender_identity_id INTEGER REFERENCES identity(id),
  body TEXT,
  attachments_json TEXT NOT NULL DEFAULT '[]',
  storage_class TEXT NOT NULL CHECK (storage_class IN ('full','metadata_only')),
  processing_status TEXT NOT NULL CHECK (processing_status IN (
    'received','queued','processing','replied','skipped','failed','needs_review','outbound')),
  skip_reason TEXT,
  error_code TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL,
  lease_owner TEXT,
  lease_expires_at REAL,
  imported INTEGER NOT NULL DEFAULT 0 CHECK (imported IN (0,1)),
  reply_policy TEXT NOT NULL DEFAULT 'normal' CHECK (reply_policy IN ('normal','never')),
  occurred_at REAL,
  ingested_at REAL NOT NULL,
  time_source TEXT NOT NULL CHECK (time_source IN ('platform','received','import')),
  CHECK ((storage_class='full' AND body IS NOT NULL)
      OR (storage_class='metadata_only' AND body IS NULL))
);
CREATE UNIQUE INDEX ux_message_platform_id ON message(account_id, platform_msg_id)
  WHERE platform_msg_id IS NOT NULL AND platform_msg_id <> '';
CREATE UNIQUE INDEX ux_message_dedup ON message(account_id, dedup_key)
  WHERE dedup_key IS NOT NULL AND dedup_key <> '';
CREATE INDEX ix_message_inbox ON message(ingested_at DESC, id DESC);
CREATE INDEX ix_message_conversation ON message(conversation_id, ingested_at DESC, id DESC);
CREATE INDEX ix_message_claim ON message(processing_status, next_attempt_at);
CREATE INDEX ix_message_lease ON message(lease_expires_at)
  WHERE processing_status='processing';
CREATE INDEX ix_message_sender ON message(sender_identity_id);

CREATE VIRTUAL TABLE message_fts USING fts5(
  body, content='message', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER message_fts_insert AFTER INSERT ON message
  WHEN new.storage_class='full' AND new.body IS NOT NULL BEGIN
  INSERT INTO message_fts(rowid, body) VALUES (new.id, new.body);
END;
-- 只删除真正进过索引的行：metadata_only 的行从来没写进 FTS，
-- 对它下 'delete' 指令会让索引和内容表对不上。
CREATE TRIGGER message_fts_delete AFTER DELETE ON message
  WHEN old.storage_class='full' AND old.body IS NOT NULL BEGIN
  INSERT INTO message_fts(message_fts, rowid, body)
    VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER message_fts_update AFTER UPDATE OF body, storage_class ON message BEGIN
  INSERT INTO message_fts(message_fts, rowid, body)
    SELECT 'delete', old.id, old.body
    WHERE old.storage_class='full' AND old.body IS NOT NULL;
  INSERT INTO message_fts(rowid, body)
    SELECT new.id, new.body
    WHERE new.storage_class='full' AND new.body IS NOT NULL;
END;

CREATE TABLE outbox (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL UNIQUE REFERENCES message(id),
  source_message_id INTEGER REFERENCES message(id),
  client_request_id TEXT NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL UNIQUE,
  requested_kind TEXT NOT NULL CHECK (requested_kind IN ('text','image')),
  effective_kind TEXT CHECK (effective_kind IN ('text','image')),
  state TEXT NOT NULL CHECK (state IN (
    'draft','pending','sending','sent','degraded','failed','cancelled','unknown')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires_at REAL,
  next_attempt_at REAL,
  error_code TEXT,
  error_detail_safe TEXT,
  platform_msg_id TEXT,
  sent_at REAL,
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX ux_outbox_auto_reply ON outbox(source_message_id)
  WHERE source_message_id IS NOT NULL;
CREATE INDEX ix_outbox_state ON outbox(state, next_attempt_at);

CREATE TABLE adapter_checkpoint (
  account_id INTEGER NOT NULL REFERENCES account(id),
  stream TEXT NOT NULL,
  cursor TEXT,
  state_json TEXT NOT NULL DEFAULT '{}',
  primed INTEGER NOT NULL DEFAULT 0 CHECK (primed IN (0,1)),
  updated_at REAL NOT NULL,
  PRIMARY KEY(account_id, stream)
);

CREATE TABLE memory_fact (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES person(id),
  kind TEXT NOT NULL CHECK (kind IN ('event','preference','promise','meme','profile')),
  content TEXT NOT NULL,
  source_message_id INTEGER REFERENCES message(id),
  source_identity_id INTEGER REFERENCES identity(id),
  confidence REAL NOT NULL DEFAULT 0.7 CHECK (confidence BETWEEN 0 AND 1),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','quarantined','deleted')),
  frozen INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0,1)),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_used_at REAL
);

CREATE TABLE sticker (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  mime TEXT NOT NULL,
  w INTEGER, h INTEGER, bytes INTEGER,
  source TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE sticker_tag (
  sticker_id INTEGER NOT NULL REFERENCES sticker(id) ON DELETE CASCADE,
  tag TEXT NOT NULL,
  PRIMARY KEY(sticker_id, tag)
);

CREATE TABLE merge_log (
  id INTEGER PRIMARY KEY,
  source_person_id INTEGER NOT NULL,
  target_person_id INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  reverted_at REAL
);

CREATE TABLE audit_event (
  id INTEGER PRIMARY KEY,
  event_type TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX ix_audit_created ON audit_event(created_at DESC, id DESC);
"""

# M2：人格单例与版本、注入记录、身份边界版本。
# 只加表和列，不改 v1 的约束；已有开发库会被原地升级（迁移前自动备份）。
_ADVANCED = """
CREATE TABLE persona (
  id INTEGER PRIMARY KEY CHECK (id=1),
  name TEXT NOT NULL,
  content TEXT NOT NULL,
  version INTEGER NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE persona_version (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at REAL NOT NULL,
  note TEXT
);
INSERT INTO persona(id,name,content,version,updated_at)
  VALUES (1, '蛆',
          '你叫蛆，破甲执行器。说话像发微信：短句、口语、直接，不端着，也不客套。拿不准就说不知道。',
          1, CAST(strftime('%s','now') AS REAL));
INSERT INTO persona_version(version,name,content,created_at,note)
  VALUES (1, '蛆',
          '你叫蛆，破甲执行器。说话像发微信：短句、口语、直接，不端着，也不客套。拿不准就说不知道。',
          CAST(strftime('%s','now') AS REAL), 'initial');

-- 出站消息记下这次用的是哪一版人格；后续改人格不改写历史回复。
ALTER TABLE message ADD COLUMN persona_version INTEGER;
-- 本次注入的来源与规模（只存 id 和计数，不存正文）。
ALTER TABLE message ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}';
-- 身份/归属变化时自增；生成中的任务保存回复前要比对，避免发给错误对象。
ALTER TABLE thread ADD COLUMN boundary_version INTEGER NOT NULL DEFAULT 0;

CREATE INDEX ix_conversation_thread ON conversation(thread_id);
CREATE INDEX ix_fact_person ON memory_fact(person_id, status);
"""

# M3：表情库。表结构在 v1 就有了，这里补「能不能用」「从哪来」「最后改过」。
# 名称可以改，主键 id 永远不变 —— 模型引用的是 id，改名不影响指向。
_STICKERS = """
ALTER TABLE sticker ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1
  CHECK (enabled IN (0,1));
ALTER TABLE sticker ADD COLUMN source_message_id INTEGER REFERENCES message(id);
ALTER TABLE sticker ADD COLUMN updated_at REAL NOT NULL DEFAULT 0;
CREATE INDEX ix_sticker_enabled ON sticker(enabled);
CREATE INDEX ix_sticker_tag ON sticker_tag(tag);
"""


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="message-base", sql=_INITIAL),
    Migration(version=2, name="identity-memory", sql=_ADVANCED),
    Migration(version=3, name="sticker-library", sql=_STICKERS),
)
