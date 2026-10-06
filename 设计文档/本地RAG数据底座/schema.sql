-- Navren 本地 RAG 建库设计。仅供隔离建库验证，尚未接入生产调用链。
-- SQLite 是事实与原文定位的权威记录；Qdrant 是可重建的派生索引。
-- 每个连接均须启用 foreign_keys。十进制值另由应用用 Decimal 验证。
PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

CREATE TABLE sources (
  source_id TEXT PRIMARY KEY NOT NULL,
  publisher TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  read_scope TEXT NOT NULL CHECK (read_scope IN ('full_text','metadata_only')),
  usage_policy_json TEXT NOT NULL CHECK (json_valid(usage_policy_json)),
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1))
);

CREATE TABLE products (
  product_id TEXT PRIMARY KEY NOT NULL,
  issuer_id TEXT NOT NULL,
  registry_scheme TEXT NOT NULL,
  registry_code TEXT NOT NULL,
  name TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  data_origin TEXT NOT NULL CHECK (data_origin IN ('real','simulated')),
  identity_status TEXT NOT NULL CHECK (identity_status IN ('pending','verified','rejected')),
  UNIQUE (issuer_id, registry_scheme, registry_code)
);

CREATE TABLE share_classes (
  share_id TEXT PRIMARY KEY NOT NULL,
  product_id TEXT NOT NULL REFERENCES products(product_id),
  variant_key TEXT NOT NULL,
  registry_scheme TEXT NOT NULL,
  share_code TEXT NOT NULL,
  currency TEXT NOT NULL,
  UNIQUE (product_id, variant_key),
  UNIQUE (registry_scheme, share_code),
  UNIQUE (share_id, product_id)
);

CREATE TABLE channels (
  channel_id TEXT PRIMARY KEY NOT NULL,
  name TEXT NOT NULL
);

CREATE TABLE documents (
  document_id TEXT PRIMARY KEY NOT NULL,
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  canonical_url TEXT NOT NULL,
  document_type TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK (visibility IN ('public','workspace')),
  workspace_id TEXT,
  outbound_policy TEXT NOT NULL CHECK (outbound_policy IN ('deny','public','approved_results')),
  data_origin TEXT NOT NULL CHECK (data_origin IN ('real','simulated')),
  CHECK ((visibility = 'public' AND workspace_id IS NULL)
      OR (visibility = 'workspace' AND workspace_id IS NOT NULL AND length(workspace_id) > 0)),
  UNIQUE (source_id, canonical_url)
);

CREATE TABLE document_versions (
  version_id TEXT PRIMARY KEY NOT NULL,
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  version_no INTEGER NOT NULL CHECK (version_no > 0),
  previous_version_id TEXT REFERENCES document_versions(version_id),
  raw_sha256 TEXT NOT NULL CHECK (length(raw_sha256) = 64),
  raw_local_path TEXT NOT NULL,
  published_at TEXT,
  effective_from TEXT,
  effective_to TEXT,
  fetched_at TEXT NOT NULL,
  read_scope TEXT NOT NULL CHECK (read_scope IN ('full_text','metadata_only')),
  lifecycle TEXT NOT NULL CHECK (lifecycle IN ('staging','verified','withdrawn','quarantined')),
  CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from),
  UNIQUE (document_id, version_no)
);

CREATE TRIGGER immutable_document_version BEFORE UPDATE ON document_versions
WHEN NEW.document_id IS NOT OLD.document_id OR NEW.version_no IS NOT OLD.version_no
 OR NEW.raw_sha256 IS NOT OLD.raw_sha256 OR NEW.raw_local_path IS NOT OLD.raw_local_path
 OR NEW.published_at IS NOT OLD.published_at OR NEW.effective_from IS NOT OLD.effective_from
 OR NEW.effective_to IS NOT OLD.effective_to OR NEW.read_scope IS NOT OLD.read_scope
BEGIN SELECT RAISE(ABORT, '文档内容与适用日期变更必须新增版本'); END;

CREATE TABLE document_parses (
  parse_id TEXT PRIMARY KEY NOT NULL,
  version_id TEXT NOT NULL REFERENCES document_versions(version_id),
  parser_name TEXT NOT NULL,
  parser_revision TEXT NOT NULL,
  normalization_revision TEXT NOT NULL,
  parsed_sha256 TEXT NOT NULL CHECK (length(parsed_sha256) = 64),
  parsed_local_path TEXT NOT NULL,
  completeness TEXT NOT NULL CHECK (completeness IN ('complete','partial','failed')),
  quality_report_json TEXT NOT NULL CHECK (json_valid(quality_report_json)),
  UNIQUE (parse_id, version_id),
  UNIQUE (version_id, parser_name, parser_revision, normalization_revision, parsed_sha256)
);

CREATE TRIGGER immutable_parse BEFORE UPDATE ON document_parses
BEGIN SELECT RAISE(ABORT, '重解析必须新增 parse_id，不覆盖旧定位'); END;

CREATE TABLE document_subjects (
  version_id TEXT NOT NULL REFERENCES document_versions(version_id),
  share_id TEXT NOT NULL REFERENCES share_classes(share_id),
  channel_id TEXT REFERENCES channels(channel_id),
  relation TEXT NOT NULL CHECK (relation IN ('terms','report','distribution','observation'))
);
CREATE UNIQUE INDEX unique_document_subject ON document_subjects
  (version_id, share_id, coalesce(channel_id, ''), relation);

CREATE TABLE evidence_spans (
  span_id INTEGER PRIMARY KEY,
  parse_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  parent_span_id INTEGER,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  kind TEXT NOT NULL CHECK (kind IN ('heading','paragraph','table','table_row','footnote')),
  section_path TEXT NOT NULL,
  page_from INTEGER CHECK (page_from > 0),
  page_to INTEGER CHECK (page_to >= page_from),
  locator_json TEXT NOT NULL CHECK (json_valid(locator_json)),
  original_text TEXT NOT NULL CHECK (length(original_text) > 0),
  structured_json TEXT NOT NULL CHECK (json_valid(structured_json)),
  UNIQUE (span_id, parse_id, version_id),
  UNIQUE (parse_id, ordinal),
  FOREIGN KEY (parse_id, version_id) REFERENCES document_parses(parse_id, version_id),
  FOREIGN KEY (parent_span_id, parse_id, version_id)
    REFERENCES evidence_spans(span_id, parse_id, version_id)
);

CREATE TRIGGER immutable_span BEFORE UPDATE ON evidence_spans
BEGIN SELECT RAISE(ABORT, '原文跨度不可覆盖，变更必须重解析'); END;

CREATE TABLE chunks (
  chunk_id INTEGER PRIMARY KEY,
  parse_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  chunker_revision TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  original_text TEXT NOT NULL CHECK (length(original_text) > 0),
  embedding_text TEXT NOT NULL CHECK (length(embedding_text) > 0),
  embedding_text_sha256 TEXT NOT NULL CHECK (length(embedding_text_sha256) = 64),
  title_terms TEXT NOT NULL,
  body_terms TEXT NOT NULL,
  token_count INTEGER NOT NULL CHECK (token_count > 0),
  UNIQUE (chunk_id, parse_id, version_id),
  UNIQUE (parse_id, chunker_revision, ordinal),
  FOREIGN KEY (parse_id, version_id) REFERENCES document_parses(parse_id, version_id)
);

CREATE TRIGGER chunks_require_full_text BEFORE INSERT ON chunks
WHEN (SELECT read_scope FROM document_versions WHERE version_id = NEW.version_id) != 'full_text'
BEGIN SELECT RAISE(ABORT, '仅元数据来源不能创建正文检索片段'); END;

CREATE TRIGGER immutable_chunk BEFORE UPDATE ON chunks
BEGIN SELECT RAISE(ABORT, '检索片段不可覆盖，切分变更必须产生新片段'); END;

CREATE TABLE chunk_spans (
  chunk_id INTEGER NOT NULL,
  span_id INTEGER NOT NULL,
  parse_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  PRIMARY KEY (chunk_id, span_id),
  FOREIGN KEY (chunk_id, parse_id, version_id) REFERENCES chunks(chunk_id, parse_id, version_id),
  FOREIGN KEY (span_id, parse_id, version_id) REFERENCES evidence_spans(span_id, parse_id, version_id)
);

-- title_terms/body_terms 存本地中文预分词文本；展示和引用使用原文。
CREATE VIRTUAL TABLE chunks_fts USING fts5 (
  title_terms, body_terms,
  content='chunks', content_rowid='chunk_id', tokenize='unicode61'
);
CREATE TRIGGER chunks_fts_insert AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, title_terms, body_terms)
    VALUES (NEW.chunk_id, NEW.title_terms, NEW.body_terms);
END;
CREATE TRIGGER chunks_fts_delete AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, title_terms, body_terms)
    VALUES ('delete', OLD.chunk_id, OLD.title_terms, OLD.body_terms);
END;

CREATE TABLE fact_assertions (
  fact_id TEXT PRIMARY KEY NOT NULL,
  share_id TEXT NOT NULL REFERENCES share_classes(share_id),
  channel_id TEXT REFERENCES channels(channel_id),
  predicate TEXT NOT NULL,
  value_type TEXT NOT NULL CHECK (value_type IN ('decimal','integer','boolean','text','json')),
  value_text TEXT NOT NULL,
  unit TEXT NOT NULL,
  currency TEXT,
  observed_on TEXT,
  period_from TEXT,
  period_to TEXT,
  effective_from TEXT,
  effective_to TEXT,
  recorded_at TEXT NOT NULL,
  nature TEXT NOT NULL CHECK (nature IN ('observed','assumption','forecast','scenario')),
  conditions_json TEXT NOT NULL CHECK (json_valid(conditions_json)),
  verification TEXT NOT NULL CHECK (verification IN ('pending','verified','conflict','rejected','superseded')),
  verification_note TEXT NOT NULL DEFAULT '',
  CHECK (period_to IS NULL OR period_from IS NULL OR period_to >= period_from),
  CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from)
);

CREATE TABLE fact_evidence (
  fact_id TEXT NOT NULL REFERENCES fact_assertions(fact_id),
  span_id INTEGER NOT NULL REFERENCES evidence_spans(span_id),
  relation TEXT NOT NULL CHECK (relation IN ('supports','contradicts')),
  start_char INTEGER NOT NULL CHECK (start_char >= 0),
  end_char INTEGER NOT NULL CHECK (end_char > start_char),
  quote_text TEXT NOT NULL,
  PRIMARY KEY (fact_id, span_id, relation, start_char, end_char)
);
CREATE TRIGGER immutable_fact_value BEFORE UPDATE ON fact_assertions
WHEN NEW.share_id IS NOT OLD.share_id OR NEW.channel_id IS NOT OLD.channel_id
 OR NEW.predicate IS NOT OLD.predicate OR NEW.value_type IS NOT OLD.value_type
 OR NEW.value_text IS NOT OLD.value_text OR NEW.unit IS NOT OLD.unit
 OR NEW.currency IS NOT OLD.currency OR NEW.observed_on IS NOT OLD.observed_on
 OR NEW.period_from IS NOT OLD.period_from OR NEW.period_to IS NOT OLD.period_to
 OR NEW.effective_from IS NOT OLD.effective_from OR NEW.effective_to IS NOT OLD.effective_to
 OR NEW.nature IS NOT OLD.nature OR NEW.conditions_json IS NOT OLD.conditions_json
BEGIN SELECT RAISE(ABORT, '事实内容变更必须产生新断言'); END;
CREATE TRIGGER validate_fact_quote BEFORE INSERT ON fact_evidence
WHEN NEW.end_char > (SELECT length(original_text) FROM evidence_spans WHERE span_id = NEW.span_id)
 OR NEW.quote_text IS NOT (SELECT substr(original_text, NEW.start_char + 1,
  NEW.end_char - NEW.start_char) FROM evidence_spans WHERE span_id = NEW.span_id)
BEGIN SELECT RAISE(ABORT, '引用必须逐字匹配指定原文跨度'); END;

CREATE TRIGGER fact_evidence_requires_subject BEFORE INSERT ON fact_evidence
WHEN NOT EXISTS (
  SELECT 1 FROM fact_assertions f JOIN evidence_spans e ON e.span_id = NEW.span_id
  JOIN document_subjects ds ON ds.version_id = e.version_id AND ds.share_id = f.share_id
  WHERE f.fact_id = NEW.fact_id AND (ds.channel_id IS NULL OR ds.channel_id IS f.channel_id)
)
BEGIN SELECT RAISE(ABORT, '事实与原文未绑定同一份额或适用渠道'); END;

CREATE TABLE channel_offers (
  offer_version_id TEXT PRIMARY KEY NOT NULL,
  share_id TEXT NOT NULL REFERENCES share_classes(share_id),
  channel_id TEXT NOT NULL REFERENCES channels(channel_id),
  channel_product_code TEXT,
  sale_status TEXT NOT NULL CHECK (sale_status IN ('unknown','open','closed','suspended')),
  subscription_status TEXT NOT NULL CHECK (subscription_status IN ('unknown','open','closed','suspended')),
  redemption_status TEXT NOT NULL CHECK (redemption_status IN ('unknown','open','closed','suspended')),
  eligibility_json TEXT NOT NULL CHECK (json_valid(eligibility_json)),
  effective_from TEXT,
  effective_to TEXT,
  checked_at TEXT NOT NULL,
  status_fact_id TEXT NOT NULL REFERENCES fact_assertions(fact_id),
  CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from)
);

CREATE TABLE series_observations (
  observation_id TEXT PRIMARY KEY NOT NULL,
  share_id TEXT NOT NULL REFERENCES share_classes(share_id),
  series_kind TEXT NOT NULL CHECK (series_kind IN
    ('unit_nav','cumulative_nav','adjusted_nav','distribution','split','income_per_10000','yield_7d_annualized')),
  on_date TEXT NOT NULL,
  value_decimal TEXT NOT NULL,
  unit TEXT NOT NULL,
  currency TEXT,
  adjustment_method TEXT,
  fact_id TEXT NOT NULL REFERENCES fact_assertions(fact_id)
);

CREATE TRIGGER observation_requires_same_fact BEFORE INSERT ON series_observations
WHEN EXISTS (SELECT 1 FROM fact_assertions WHERE fact_id = NEW.fact_id
 AND (share_id != NEW.share_id OR value_text != NEW.value_decimal OR unit != NEW.unit
      OR currency IS NOT NEW.currency OR observed_on IS NOT NEW.on_date))
BEGIN SELECT RAISE(ABORT, '序列值、份额、单位和日期必须与事实一致'); END;

CREATE TABLE fee_rules (
  fee_rule_id TEXT PRIMARY KEY NOT NULL,
  share_id TEXT NOT NULL REFERENCES share_classes(share_id),
  channel_id TEXT REFERENCES channels(channel_id),
  fee_kind TEXT NOT NULL,
  calculation_basis TEXT NOT NULL,
  investor_class TEXT NOT NULL,
  condition_dsl_json TEXT NOT NULL CHECK (json_valid(condition_dsl_json)),
  charge_type TEXT NOT NULL CHECK (charge_type IN ('rate','fixed','tiered')),
  rate_decimal TEXT,
  fixed_minor INTEGER,
  currency TEXT NOT NULL,
  included_in_nav INTEGER NOT NULL CHECK (included_in_nav IN (0,1)),
  day_count_rule TEXT NOT NULL,
  rounding_rule TEXT NOT NULL,
  effective_from TEXT,
  effective_to TEXT,
  fact_id TEXT NOT NULL REFERENCES fact_assertions(fact_id),
  CHECK ((charge_type = 'rate' AND rate_decimal IS NOT NULL AND fixed_minor IS NULL)
      OR (charge_type = 'fixed' AND rate_decimal IS NULL AND fixed_minor IS NOT NULL AND fixed_minor >= 0)
      OR (charge_type = 'tiered' AND rate_decimal IS NULL AND fixed_minor IS NULL)),
  CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from)
);

CREATE TABLE embedding_profiles (
  embedding_profile_id TEXT PRIMARY KEY NOT NULL,
  model_name TEXT NOT NULL,
  model_revision TEXT NOT NULL,
  weights_sha256 TEXT NOT NULL CHECK (length(weights_sha256) = 64),
  tokenizer_revision TEXT NOT NULL,
  dimensions INTEGER NOT NULL CHECK (dimensions > 0),
  max_input_tokens INTEGER NOT NULL CHECK (max_input_tokens > 0),
  distance TEXT NOT NULL CHECK (distance IN ('cosine','dot','euclid')),
  normalize INTEGER NOT NULL CHECK (normalize IN (0,1)),
  query_prefix TEXT NOT NULL
);

CREATE TABLE releases (
  release_id TEXT PRIMARY KEY NOT NULL,
  scope_key TEXT NOT NULL,
  embedding_profile_id TEXT NOT NULL REFERENCES embedding_profiles(embedding_profile_id),
  vector_collection TEXT NOT NULL UNIQUE,
  chunker_revision TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('staging','ready','retired','failed')),
  expected_chunk_count INTEGER NOT NULL CHECK (expected_chunk_count > 0),
  manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
  check_report_json TEXT NOT NULL CHECK (json_valid(check_report_json)),
  created_at TEXT NOT NULL,
  UNIQUE (release_id, scope_key)
);

CREATE TABLE release_chunks (
  release_id TEXT NOT NULL REFERENCES releases(release_id),
  chunk_id INTEGER NOT NULL REFERENCES chunks(chunk_id),
  vector_point_id TEXT NOT NULL,
  embedding_text_sha256 TEXT NOT NULL CHECK (length(embedding_text_sha256) = 64),
  lexical_ready INTEGER NOT NULL DEFAULT 0 CHECK (lexical_ready IN (0,1)),
  vector_ready INTEGER NOT NULL DEFAULT 0 CHECK (vector_ready IN (0,1)),
  PRIMARY KEY (release_id, chunk_id),
  UNIQUE (release_id, vector_point_id)
);

CREATE TRIGGER release_ready_requires_complete_manifest BEFORE UPDATE OF status ON releases
WHEN NEW.status = 'ready' AND (
  (SELECT count(*) FROM release_chunks WHERE release_id = NEW.release_id) != NEW.expected_chunk_count
  OR EXISTS (SELECT 1 FROM release_chunks rc JOIN chunks c ON c.chunk_id = rc.chunk_id
    JOIN document_parses p ON p.parse_id = c.parse_id
    JOIN document_versions v ON v.version_id = c.version_id
    JOIN documents d ON d.document_id = v.document_id JOIN sources s ON s.source_id = d.source_id
    WHERE rc.release_id = NEW.release_id AND
      (rc.lexical_ready != 1 OR rc.vector_ready != 1 OR rc.embedding_text_sha256 != c.embedding_text_sha256
       OR p.completeness != 'complete' OR v.lifecycle != 'verified' OR v.read_scope != 'full_text' OR s.enabled != 1))
  OR EXISTS (SELECT 1 FROM release_chunks rc WHERE rc.release_id = NEW.release_id
    AND NOT EXISTS (SELECT 1 FROM chunk_spans cs WHERE cs.chunk_id = rc.chunk_id))
)
BEGIN SELECT RAISE(ABORT, '发布清单、原文映射或索引未完成'); END;

CREATE TRIGGER releases_start_staging BEFORE INSERT ON releases
WHEN NEW.status != 'staging'
BEGIN SELECT RAISE(ABORT, '新发布必须先进入 staging'); END;

CREATE TABLE current_releases (
  scope_key TEXT PRIMARY KEY NOT NULL,
  release_id TEXT NOT NULL,
  FOREIGN KEY (release_id, scope_key) REFERENCES releases(release_id, scope_key)
);
CREATE TRIGGER freeze_release_members_insert BEFORE INSERT ON release_chunks
WHEN (SELECT status FROM releases WHERE release_id = NEW.release_id) != 'staging'
BEGIN SELECT RAISE(ABORT, '已发布的索引清单不可增加片段'); END;
CREATE TRIGGER freeze_release_members_update BEFORE UPDATE ON release_chunks
WHEN (SELECT status FROM releases WHERE release_id = OLD.release_id) != 'staging'
 OR (SELECT status FROM releases WHERE release_id = NEW.release_id) != 'staging'
BEGIN SELECT RAISE(ABORT, '已发布的索引清单不可覆盖'); END;
CREATE TRIGGER freeze_release_members_delete BEFORE DELETE ON release_chunks
WHEN (SELECT status FROM releases WHERE release_id = OLD.release_id) != 'staging'
BEGIN SELECT RAISE(ABORT, '已发布的索引清单不可删除片段'); END;
CREATE TRIGGER current_release_insert_requires_ready BEFORE INSERT ON current_releases
WHEN (SELECT status FROM releases WHERE release_id = NEW.release_id) != 'ready'
BEGIN SELECT RAISE(ABORT, '只能切换到已核对的 ready 发布'); END;
CREATE TRIGGER current_release_update_requires_ready BEFORE UPDATE ON current_releases
WHEN (SELECT status FROM releases WHERE release_id = NEW.release_id) != 'ready'
BEGIN SELECT RAISE(ABORT, '只能切换到已核对的 ready 发布'); END;

CREATE TRIGGER immutable_release_definition BEFORE UPDATE ON releases
WHEN OLD.status != 'staging' AND (
  NEW.scope_key IS NOT OLD.scope_key OR NEW.embedding_profile_id IS NOT OLD.embedding_profile_id
  OR NEW.vector_collection IS NOT OLD.vector_collection OR NEW.chunker_revision IS NOT OLD.chunker_revision
  OR NEW.expected_chunk_count IS NOT OLD.expected_chunk_count OR NEW.manifest_sha256 IS NOT OLD.manifest_sha256
  OR NEW.status = 'staging' OR (OLD.status != 'ready' AND NEW.status = 'ready'))
BEGIN SELECT RAISE(ABORT, '已发布的索引定义不可覆盖或重新启用'); END;

CREATE TABLE ingest_jobs (
  job_id TEXT PRIMARY KEY NOT NULL,
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  version_id TEXT REFERENCES document_versions(version_id),
  release_id TEXT REFERENCES releases(release_id),
  stage TEXT NOT NULL CHECK (stage IN ('fetch','parse','map_identity','extract','verify','chunk','embed','publish')),
  status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  started_at TEXT,
  ended_at TEXT,
  error_code TEXT
);

CREATE TABLE retrieval_runs (
  request_id TEXT PRIMARY KEY NOT NULL,
  workspace_id TEXT NOT NULL,
  release_id TEXT NOT NULL REFERENCES releases(release_id),
  query_sha256 TEXT NOT NULL CHECK (length(query_sha256) = 64),
  request_spec_json TEXT NOT NULL CHECK (json_valid(request_spec_json)),
  policy_revision TEXT NOT NULL,
  coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
  outcome TEXT NOT NULL CHECK (outcome IN ('supported','partial','insufficient','failed')),
  created_at TEXT NOT NULL,
  UNIQUE (request_id, release_id)
);

CREATE TABLE retrieval_hits (
  request_id TEXT NOT NULL,
  release_id TEXT NOT NULL,
  chunk_id INTEGER NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('lexical','dense','fused','reranked','selected')),
  rank INTEGER NOT NULL CHECK (rank > 0),
  score REAL,
  PRIMARY KEY (request_id, stage, chunk_id),
  FOREIGN KEY (request_id, release_id) REFERENCES retrieval_runs(request_id, release_id),
  FOREIGN KEY (release_id, chunk_id) REFERENCES release_chunks(release_id, chunk_id)
);

CREATE INDEX facts_lookup ON fact_assertions(share_id, predicate, channel_id, verification, effective_from, effective_to);
CREATE INDEX observations_lookup ON series_observations(share_id, series_kind, on_date);
CREATE INDEX fees_lookup ON fee_rules(share_id, channel_id, fee_kind, effective_from, effective_to);
CREATE INDEX versions_lookup ON document_versions(document_id, version_no DESC);
CREATE INDEX spans_lookup ON evidence_spans(parse_id, section_path, ordinal);
CREATE INDEX subjects_lookup ON document_subjects(share_id, channel_id, version_id);

-- 每次检索仍须在 SELECT 中按 workspace、release、产品身份、来源状态和日期过滤。
-- 该视图只处理撤回/禁用；不是完整访问控制，也不代表时点适用性已核验。
CREATE VIEW available_release_chunks AS
SELECT rc.release_id, c.*, d.visibility, d.workspace_id, d.outbound_policy, d.data_origin
FROM release_chunks rc JOIN chunks c ON c.chunk_id = rc.chunk_id
JOIN document_versions v ON v.version_id = c.version_id
JOIN documents d ON d.document_id = v.document_id JOIN sources s ON s.source_id = d.source_id
JOIN releases r ON r.release_id = rc.release_id
WHERE r.status = 'ready' AND v.lifecycle = 'verified' AND s.enabled = 1;
