"""Add SQL article metadata to existing chunks without supplying old field values.

Fixed target; backup prerequisite; deterministic cache/checkpoints; no source delete,
drop, reingestion, embeddings, or automatic restore. Run stages separately.
"""
import argparse
import datetime
import gzip
import hashlib
import json
import pathlib
import re
import sqlite3
import struct
import time
import uuid

import requests
from pymilvus import DataType, MilvusClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
SQL = ROOT / 'article_sciwatch_dandan' / 'article_sciwatch_dandan.sql'
BACKUP = pathlib.Path(r'E:\milvus_backups\agri_pdf_memories\agri_breeding_pdf_chunk\20261010_195051')
OUT = BACKUP / 'enrichment'
COLL = 'agri_breeding_pdf_chunk'
URI = 'http://127.0.0.1:19534'
DBNAME = 'agri_pdf_memories'
COUNT = 1413886
OLD = ['id', 'pdf_index', 'chunk_index', 'text_embed', 'text', 'char_start', 'char_end', 'section_path']
NEW = ['gid', 'title', 'year', 'volume', 'issue', 'doi', 'journal_name', 'issn',
       'author', 'author_add', 'keyword', 'abstracts', 'indexed_by', 'url', 'language',
       'published_date', 'start_page', 'end_page', 'publisher', 'hasFullText']
LENGTHS = {f: 512 for f in NEW}
LENGTHS.update({f: 256 for f in ['gid', 'year', 'volume']})
LENGTHS.update({f: 65535 for f in ['title', 'author', 'author_add', 'keyword', 'abstracts', 'url']})
LENGTHS.update({f: 2048 for f in ['journal_name', 'indexed_by', 'publisher']})
LENGTHS.update(issue=1024, hasFullText=64)


def log(message):
    print(datetime.datetime.now().astimezone().isoformat(timespec='seconds'), message, flush=True)


def dump(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temp.replace(path)


def sha_file(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def fingerprint(row):
    h = hashlib.sha256()
    for f in OLD:
        if f == 'text_embed':
            raw = struct.pack('<' + 'f' * len(row[f]), *row[f])
        else:
            raw = json.dumps(row[f], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        h.update(struct.pack('<Q', len(raw)))
        h.update(raw)
    return h.digest()


def metrics():
    response = requests.get('http://127.0.0.1:9095/metrics', timeout=30)
    response.raise_for_status()
    return sorted(line for line in response.text.splitlines()
                  if line.startswith(('milvus_proxy_insert_vectors_count{',
                                      'milvus_proxy_delete_vectors_count{',
                                      'milvus_proxy_upsert_vectors_count{'))
                  and f'collection_name="{COLL}"' in line)


def connect():
    return MilvusClient(uri=URI, db_name=DBNAME, timeout=120)


def state_db():
    OUT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(OUT / 'progress.sqlite')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, pdf_index TEXT NOT NULL, gid TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)')
    db.execute('CREATE INDEX IF NOT EXISTS chunks_done_id ON chunks(done,id)')
    db.execute('CREATE TABLE IF NOT EXISTS articles (gid TEXT PRIMARY KEY, metadata TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS batches (seq INTEGER PRIMARY KEY, rows INTEGER, seconds REAL, bytes INTEGER, completed_at TEXT)')
    return db


def put(db, key, value):
    db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', (key, json.dumps(value)))
    db.commit()


def get(db, key):
    row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def baseline_db():
    return sqlite3.connect((BACKUP / 'original-eight-fields-sha256.sqlite').as_uri() + '?mode=ro', uri=True)


def check_original_schema(c):
    before = json.loads((BACKUP / 'source-after.json').read_text(encoding='utf-8'))
    current = c.describe_collection(COLL)
    assert current['collection_id'] == before['schema']['collection_id'], 'Collection identity changed'
    fields = {f['name']: f for f in current['fields']}
    for field in before['schema']['fields']:
        assert fields[field['name']] == field, f'Original field definition changed: {field["name"]}'
    assert set(fields) <= set(OLD + NEW), 'Unexpected fields'
    for field in current['fields']:
        if field['name'] in NEW:
            assert field['type'] == DataType.VARCHAR and field.get('nullable') is True, field
            assert int(field['params']['max_length']) == LENGTHS[field['name']], field
    return current


def logical_count(c, collection=COLL):
    return c.query(collection, filter='', output_fields=['count(*)'], consistency_level='Strong')[0]['count(*)']


def parse_values(line):
    """Decode quoted MySQL values; never execute dump SQL (including DROP TABLE)."""
    prefix = 'INSERT INTO `article_sciwatch_dandan` VALUES ('
    assert line.startswith(prefix)
    text = line[len(prefix):].rstrip()
    assert text.endswith(');'), 'Unexpected/multiline INSERT'
    text = text[:-2]
    values, i = [], 0
    escapes = {'0': '\0', 'b': '\b', 'n': '\n', 'r': '\r', 't': '\t', 'Z': '\x1a'}
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if text[i] == "'":
            i += 1
            chars = []
            while i < len(text):
                ch = text[i]
                i += 1
                if ch == '\\':
                    assert i < len(text), 'Incomplete SQL escape'
                    escaped = text[i]
                    i += 1
                    chars.append(('\\' + escaped) if escaped in ('%', '_') else escapes.get(escaped, escaped))
                elif ch == "'":
                    if i < len(text) and text[i] == "'":
                        chars.append("'")
                        i += 1
                    else:
                        break
                else:
                    chars.append(ch)
            else:
                raise ValueError('Unterminated SQL string')
            values.append(''.join(chars))
        else:
            stop = text.find(',', i)
            stop = len(text) if stop == -1 else stop
            token = text[i:stop].strip()
            assert token.upper() == 'NULL', f'Unexpected unquoted token: {token}'
            values.append(None)
            i = stop
        while i < len(text) and text[i].isspace():
            i += 1
        if i < len(text):
            assert text[i] == ',', 'Unexpected SQL value delimiter'
            i += 1
    assert len(values) == len(NEW), f'Expected 20 columns, received {len(values)}'
    return dict(zip(NEW, values))


def prepare(c, db):
    validation = json.loads((BACKUP / 'backup-validation.json').read_text(encoding='utf-8'))
    assert validation['backup_success'] and validation['original_field_fingerprint_complete']
    assert sha_file(BACKUP / 'original-eight-fields-sha256.sqlite') == validation['original_field_fingerprint_file_sha256']
    source = json.loads((BACKUP / 'source-after.json').read_text(encoding='utf-8'))
    assert c.describe_collection(COLL) == source['schema'], 'Preflight schema differs from backup baseline'
    assert metrics() == source['mutation_counters'], 'Source DML changed since baseline'
    assert logical_count(c) == COUNT
    assert not get(db, 'prepared'), 'Cache already prepared; use later stages'
    assert db.execute('SELECT count(*) FROM chunks').fetchone()[0] == 0, 'Partial cache exists; inspect before retry'
    start = time.monotonic()
    base = baseline_db()
    iterator = c.query_iterator(COLL, batch_size=3000, output_fields=['id', 'pdf_index'], consistency_level='Strong')
    total = 0
    try:
        while rows := iterator.next():
            ids = [r['id'] for r in rows]
            known = set()
            for offset in range(0, len(ids), 500):
                subset = ids[offset:offset + 500]
                known.update(r[0] for r in base.execute('SELECT id FROM row_digest WHERE id IN (' + ','.join('?' for _ in subset) + ')', subset))
            assert known == set(ids), 'Unexpected primary key relative to backup'
            records = []
            for row in rows:
                pdf = row['pdf_index']
                gid = pdf[:-4] if pdf.endswith('.pdf') else pdf
                assert gid and '/' not in gid and '\\' not in gid
                records.append((row['id'], pdf, gid))
            db.executemany('INSERT INTO chunks (id,pdf_index,gid) VALUES (?,?,?)', records)
            db.commit()
            total += len(rows)
    finally:
        iterator.close()
        base.close()
    assert total == COUNT
    needed = set(r[0] for r in db.execute('SELECT DISTINCT gid FROM chunks'))
    digest = hashlib.sha256()
    seen, matches, sql_rows = set(), [], 0
    maxima = dict.fromkeys(NEW, 0)
    article_nonnull = dict.fromkeys(NEW, 0)
    with SQL.open('rb') as stream:
        for raw in stream:
            digest.update(raw)
            if not raw.startswith(b'INSERT INTO '):
                continue
            record = parse_values(raw.decode('utf-8'))
            gid = record['gid']
            assert gid and gid not in seen, 'Missing or duplicate SQL GID'
            seen.add(gid)
            sql_rows += 1
            if gid not in needed:
                continue
            for name in NEW:
                value = record[name]
                if value is not None:
                    size = len(value.encode('utf-8'))
                    assert size <= LENGTHS[name], f'Value too long: {gid}/{name}/{size}'
                    maxima[name] = max(maxima[name], size)
                    article_nonnull[name] += 1
            matches.append(record)
    assert {r['gid'] for r in matches} == needed, 'Some PDF sources have no SQL metadata'
    db.executemany('INSERT INTO articles VALUES (?,?)', [(r['gid'], json.dumps(r, ensure_ascii=False, separators=(',', ':'))) for r in matches])
    db.commit()
    # Portable matched-only compressed cache needs no extra service or package.
    with gzip.open(OUT / 'matched-articles.jsonl.gz', 'wt', encoding='utf-8', compresslevel=3) as cache:
        for record in matches:
            cache.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
    assert metrics() == source['mutation_counters'], 'Source changed during preparation'
    report = {'sql_sha256': digest.hexdigest(), 'sql_bytes': SQL.stat().st_size, 'sql_rows': sql_rows,
              'matched_articles': len(matches), 'chunks': total, 'missing_matches': 0,
              'utf8_maxima': maxima, 'article_nonnull': article_nonnull,
              'seconds': time.monotonic() - start, 'preflight_original_schema_and_dml_unchanged': True}
    put(db, 'prepared', report)
    dump('prepare.json', report)
    log(f'PREPARED: {len(matches)} articles, {total} chunks, missing=0; {report["seconds"]:.1f}s')


def pk_filter(ids):
    return 'id in ' + json.dumps(ids, ensure_ascii=False)


def query_ids(c, collection, ids, fields):
    result = {}
    for offset in range(0, len(ids), 100):
        for row in c.query(collection, filter=pk_filter(ids[offset:offset + 100]), output_fields=fields, consistency_level='Strong', timeout=180):
            assert row['id'] not in result
            result[row['id']] = row
    assert set(result) == set(ids), 'Readback PK mismatch'
    return result


def verify_old_rows(rows):
    base = baseline_db()
    try:
        for row in rows.values():
            expected = base.execute('SELECT sha256 FROM row_digest WHERE id=?', (row['id'],)).fetchone()
            assert expected and expected[0] == fingerprint(row), f'Original field value changed: {row["id"]}'
    finally:
        base.close()


def self_test(c, db):
    assert get(db, 'prepared')
    name = 'metadata_enrichment_probe_' + uuid.uuid4().hex[:12]
    ids = [r[0] for r in db.execute('SELECT id FROM chunks ORDER BY id LIMIT 2')]
    originals = query_ids(c, COLL, ids, OLD)
    verify_old_rows(originals)
    before = check_original_schema(c)
    before_metrics = metrics()
    schema = c.create_schema(auto_id=False, enable_dynamic_field=False)
    for f in before['fields']:
        if f['name'] in OLD:
            schema.add_field(f['name'], f['type'], is_primary=f.get('is_primary', False), **f['params'])
    created = False
    try:
        index = c.prepare_index_params()
        index.add_index('text_embed', index_type='FLAT', metric_type='COSINE')
        c.create_collection(name, schema=schema, index_params=index)
        created = True
        c.insert(name, list(originals.values()))
        for field in NEW:
            c.add_collection_field(name, field, DataType.VARCHAR, nullable=True, max_length=LENGTHS[field])
        fields = c.describe_collection(name)['fields']
        assert len(fields) == 28
        empty = query_ids(c, name, ids, ['id'] + NEW)
        assert all(row.get(f) is None for row in empty.values() for f in NEW)
        sample = json.loads(db.execute('SELECT metadata FROM articles WHERE gid=(SELECT gid FROM chunks WHERE id=?)', (ids[0],)).fetchone()[0])
        payload = {'id': ids[0], **sample}
        payload['title'] = "NULL literal; quote ' ; slash \\ ; unicode 水稻"
        payload['issue'] = None
        result = c.upsert(name, [payload], partial_update=True)
        assert result['upsert_count'] == 1
        read = query_ids(c, name, ids, OLD + NEW)
        for key, row in read.items():
            assert fingerprint(row) == fingerprint(originals[key]), 'Partial upsert changed original values'
        assert all(read[ids[0]].get(f) == payload[f] for f in NEW)
        assert all(read[ids[1]].get(f) is None for f in NEW)
        c.upsert(name, [{'id': ids[0], 'title': 'second pass'}], partial_update=True)
        read = query_ids(c, name, ids, OLD + NEW)
        assert read[ids[0]]['title'] == 'second pass'
        assert all(read[ids[0]].get(f) == payload[f] for f in NEW if f != 'title')
        assert fingerprint(read[ids[0]]) == fingerprint(originals[ids[0]])
        assert logical_count(c, name) == 2
        # A missing PK must fail with omitted nonnullable original fields; never try on source.
        try:
            c.upsert(name, [{'id': 'probe_only_missing_pk', 'title': 'must fail'}], partial_update=True)
        except Exception:
            pass
        else:
            raise AssertionError('Unexpected missing-PK insertion behavior')
        assert logical_count(c, name) == 2
        assert metrics() == before_metrics
        put(db, 'self_test', {'passed': True, 'test_collection': name, 'original_hashes_unchanged': True})
        dump('self-test.json', get(db, 'self_test'))
        log('ISOLATED TEST PASSED: nullable fields, partial update, NULL, repeat update, original hashes, missing PK refusal')
    finally:
        if created:
            # Only the uniquely named collection created by this test can be removed.
            assert name.startswith('metadata_enrichment_probe_') and name != COLL
            c.drop_collection(name)


def add_fields(c, db):
    assert get(db, 'prepared') and get(db, 'self_test')['passed']
    source = json.loads((BACKUP / 'source-after.json').read_text(encoding='utf-8'))
    assert metrics() == source['mutation_counters'], 'DML occurred before schema additions'
    before = check_original_schema(c)
    assert logical_count(c) == COUNT
    existing = {f['name'] for f in before['fields']}
    for name in NEW:
        if name not in existing:
            c.add_collection_field(COLL, name, DataType.VARCHAR,
                                   desc='Article metadata from article_sciwatch_dandan SQL',
                                   nullable=True, max_length=LENGTHS[name], timeout=180)
            log(f'ADDED nullable field: {name}')
    current = check_original_schema(c)
    assert len(current['fields']) == 28
    assert logical_count(c) == COUNT and metrics() == source['mutation_counters']
    put(db, 'fields_added', True)
    dump('schema-after-add.json', current)
    log('SCHEMA COMPLETE: original 8 definitions preserved + 20 nullable VARCHAR fields')


def metadata_check(rows, metadata):
    for key, row in rows.items():
        expected = metadata[key]
        for f in NEW:
            assert row.get(f) == expected[f], f'Metadata differs: {key}/{f}'


def run(c, db, limit):
    assert get(db, 'prepared') and get(db, 'fields_added')
    assert sha_file(SQL) == get(db, 'prepared')['sql_sha256'], 'SQL source changed; cannot continue'
    assert len(check_original_schema(c)['fields']) == 28
    assert logical_count(c) == COUNT
    articles = {r[0]: json.loads(r[1]) for r in db.execute('SELECT gid,metadata FROM articles')}
    done_initial = db.execute('SELECT count(*) FROM chunks WHERE done=1').fetchone()[0]
    if done_initial == 0:
        source = json.loads((BACKUP / 'source-after.json').read_text(encoding='utf-8'))
        assert metrics() == source['mutation_counters'], 'Unexpected pre-write DML'
    elif limit == 0:
        assert get(db, 'pilot_passed'), 'Full write requires validated pilot'
    start = time.monotonic()
    processed, payload_bytes, next_log = 0, 0, 0
    seq = db.execute('SELECT COALESCE(max(seq),0) FROM batches').fetchone()[0]
    while not limit or processed < limit:
        amount = min(1000, limit - processed) if limit else 1000
        records = db.execute('SELECT id,pdf_index,gid FROM chunks WHERE done=0 ORDER BY id LIMIT ?', (amount,)).fetchall()
        if not records:
            break
        batch, size = [], 0
        for key, pdf, gid in records:
            row = {'id': key, **articles[gid]}
            row_size = len(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
            if batch and size + row_size > 8 * 1024 * 1024:
                break
            assert set(row) == set(['id'] + NEW) and row['gid'] == gid
            batch.append(row)
            size += row_size
        ids = [r['id'] for r in batch]
        # Guard every batch against stale/mismatched join keys before partial upsert.
        present = query_ids(c, COLL, ids, ['id', 'pdf_index'])
        source_pdf = {r[0]: r[1] for r in records}
        assert all(present[key]['pdf_index'] == source_pdf[key] for key in ids)
        tick = time.monotonic()
        for attempt in range(5):
            try:
                result = c.upsert(COLL, batch, partial_update=True, timeout=180)
                assert result['upsert_count'] == len(batch), result
                break
            except Exception as exc:
                # Only transport/transient overload errors are retried, with identical payload.
                transient = any(word in str(exc).lower() for word in ('unavailable', 'deadline', 'timeout', 'rate limit', 'resource exhausted', 'connection reset'))
                if not transient or attempt == 4:
                    raise
                log(f'Transient write error, retry {attempt+1}: {type(exc).__name__}')
                time.sleep(min(2 ** attempt, 16))
        expected = {r['id']: r for r in batch}
        readback = query_ids(c, COLL, ids, ['id'] + NEW)
        metadata_check(readback, expected)
        # Pilot checks all old values immediately. Full run checks every old value at end.
        if limit:
            verify_old_rows(query_ids(c, COLL, ids, OLD))
        db.executemany('UPDATE chunks SET done=1 WHERE id=? AND done=0', [(key,) for key in ids])
        seq += 1
        db.execute('INSERT INTO batches VALUES (?,?,?,?,?)', (seq, len(batch), time.monotonic()-tick, size, datetime.datetime.now().astimezone().isoformat()))
        db.commit()
        processed += len(batch)
        payload_bytes += size
        if time.monotonic() >= next_log:
            elapsed = time.monotonic()-start
            speed = processed / elapsed
            remaining = (COUNT-done_initial-processed) / speed if speed else 0
            log(f'UPDATED+READBACK {done_initial+processed}/{COUNT}; {speed:.1f} rows/s; ETA {remaining/60:.1f} min')
            next_log = time.monotonic() + 45
    elapsed = time.monotonic()-start
    report = {'processed': processed, 'done_total': done_initial+processed, 'seconds': elapsed,
              'rows_per_second': processed/elapsed if elapsed else 0, 'metadata_payload_bytes': payload_bytes,
              'all_submitted_batches_readback_verified': True, 'pilot_original_hashes_checked': bool(limit)}
    assert logical_count(c) == COUNT
    if limit:
        put(db, 'pilot_passed', True)
    elif report['done_total'] == COUNT:
        put(db, 'writes_complete', report)
    dump(f'write-{done_initial}-{done_initial+processed}.json', report)
    log(f'WRITE STAGE COMPLETE: {report}')


def verify(c, db):
    assert get(db, 'writes_complete') and db.execute('SELECT count(*) FROM chunks WHERE done=1').fetchone()[0] == COUNT
    log('Final flush once, then verify complete original values and metadata')
    c.flush(COLL, timeout=600)
    schema = check_original_schema(c)
    assert len(schema['fields']) == 28 and logical_count(c) == COUNT
    articles = {r[0]: json.loads(r[1]) for r in db.execute('SELECT gid,metadata FROM articles')}
    base = baseline_db()
    verify_path = OUT / 'full-verification.sqlite'
    checked = sqlite3.connect(verify_path)
    checked.execute('CREATE TABLE IF NOT EXISTS verified (id TEXT PRIMARY KEY)')
    assert checked.execute('SELECT count(*) FROM verified').fetchone()[0] == 0, 'Verification file already has rows; inspect before retry'
    # Vector + 20 scalar columns can exceed the per-query result-size budget
    # when candidates from many sealed segments are merged. Use small read batches.
    iterator = c.query_iterator(COLL, batch_size=100, output_fields=OLD + NEW, consistency_level='Strong', timeout=180)
    start, total, next_log = time.monotonic(), 0, 0
    nonnull = dict.fromkeys(NEW, 0)
    before_metrics = metrics()
    try:
        while rows := iterator.next():
            for row in rows:
                expected = base.execute('SELECT sha256 FROM row_digest WHERE id=?', (row['id'],)).fetchone()
                assert expected and expected[0] == fingerprint(row), f'ORIGINAL VALUE MISMATCH: {row["id"]}'
                gid = row['pdf_index'][:-4] if row['pdf_index'].endswith('.pdf') else row['pdf_index']
                expected_meta = articles[gid]
                for f in NEW:
                    assert row.get(f) == expected_meta[f], f'METADATA MISMATCH: {row["id"]}/{f}'
                    if row.get(f) is not None:
                        nonnull[f] += 1
            checked.executemany('INSERT INTO verified VALUES (?)', [(r['id'],) for r in rows])
            checked.commit()
            total += len(rows)
            if time.monotonic() >= next_log:
                elapsed = time.monotonic()-start
                log(f'FULL ORIGINAL HASH+METADATA CHECK {total}/{COUNT}; {elapsed:.0f}s; ETA {(COUNT-total)/(total/elapsed)/60:.1f} min')
                next_log = time.monotonic() + 45
        assert total == COUNT and checked.execute('SELECT count(*) FROM verified').fetchone()[0] == COUNT
        assert logical_count(c) == COUNT and metrics() == before_metrics, 'Source changed during final verification'
        sample_id = db.execute('SELECT id FROM chunks ORDER BY id LIMIT 1').fetchone()[0]
        sample = query_ids(c, COLL, [sample_id], ['id', 'text_embed'])[sample_id]
        search_fields = ['pdf_index', 'gid', 'title', 'year', 'journal_name', 'author', 'abstracts']
        hits = c.search(COLL, data=[sample['text_embed']], anns_field='text_embed', limit=3,
                        search_params={'metric_type': 'COSINE', 'params': {'ef': 64}},
                        output_fields=search_fields, consistency_level='Strong', timeout=180)[0]
        assert len(hits) == 3, 'Vector search did not return expected results'
        for hit in hits:
            entity = hit['entity']
            expected = articles[entity['gid']]
            assert all(entity.get(f) == expected[f] for f in search_fields if f != 'pdf_index'), 'Search metadata mismatch'
        dump('search-smoke-test.json', {'passed': True, 'query_id': sample_id,
                                       'returned_ids': [hit['id'] for hit in hits],
                                       'output_fields': search_fields})
        report = {'success': True, 'verified_rows': total, 'original_fields_unchanged': OLD,
                  'original_hash_mismatches': 0, 'metadata_mismatches': 0, 'new_fields': NEW,
                  'total_fields': len(schema['fields']), 'chunk_nonnull': nonnull,
                  'seconds': time.monotonic()-start, 'backup_retained': str(BACKUP),
                  'backup_server_prefix': 'a-bucket/backup_agri_breeding/agri_breeding_pdf_chunk_20261010_195051/',
                  'finished_at': datetime.datetime.now().astimezone().isoformat()}
        report['vector_search_with_new_metadata_verified'] = True
        report['indexes'] = [c.describe_index(COLL, name) for name in c.list_indexes(COLL)]
        report['load_state'] = c.get_load_state(COLL)
        report['physical_storage_stats'] = c.get_collection_stats(COLL)
        report['logical_count_strong_query'] = logical_count(c)
        retention_path = OUT / 'backup-retention-after-enrichment.json'
        if retention_path.exists():
            report['backup_retention_recheck'] = json.loads(retention_path.read_text(encoding='utf-8'))
        put(db, 'verified', report)
        dump('final-result.json', report)
        dump('schema-final.json', schema)
        text = ('# 字段增补完成\n\n原集合：`agri_pdf_memories.agri_breeding_pdf_chunk`。\n\n'
                f'记录总数：{total:,}；字段数：8 → 28。\n\n'
                '原来的 8 个字段对全部记录逐条按备份前 SHA256 校验，0 条差异；新增元数据逐条与 SQL 对照，0 条差异。\n\n'
                '仅通过 pdf_index 去除末尾 .pdf 匹配 SQL gid；SQL 未执行，未导入 MySQL，未重新生成向量。\n\n'
                '新增字段：' + '、'.join(NEW) + '。SQL 中缺失值保留为空，未编造、未截断；字面字符串 NULL 保留原值。\n\n'
                '服务器原备份仍保留于 `' + report['backup_server_prefix'] + '`。\n\n'
                '本次使用 Milvus partial_update 合并新字段，底层可能生成历史行版本和删除标记，原 8 字段逻辑值已完整核对一致。\n\n'
                '界面的后台 row_count 可能暂时包含历史版本；本报告的记录数来自强一致 count(*) 和全部主键唯一性检查。\n\n'
                '注意：原 PDF 入库程序仍按 8 字段验证结构，继续导入新 PDF 前需要另行适配；本次未运行或改动该入库程序。\n')
        (OUT / '增补结果.md').write_text(text, encoding='utf-8')
        log(f'ALL VERIFIED: {total} rows; original fields/hash mismatches=0; metadata mismatches=0')
    finally:
        iterator.close()
        base.close()
        checked.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['preflight', 'prepare', 'test', 'add', 'run', 'verify', 'status'])
    parser.add_argument('--limit', type=int, default=0, help='Pilot additional rows; 0 means all remaining')
    args = parser.parse_args()
    assert args.limit >= 0
    c, db = connect(), state_db()
    try:
        if args.stage == 'preflight':
            baseline = json.loads((BACKUP / 'source-after.json').read_text(encoding='utf-8'))
            result = {'schema_unchanged': c.describe_collection(COLL) == baseline['schema'],
                      'logical_count': logical_count(c), 'mutation_counters_unchanged': metrics() == baseline['mutation_counters']}
            log(json.dumps(result))
            assert all(result[k] for k in ['schema_unchanged', 'mutation_counters_unchanged']) and result['logical_count'] == COUNT
        elif args.stage == 'prepare':
            prepare(c, db)
        elif args.stage == 'test':
            self_test(c, db)
        elif args.stage == 'add':
            add_fields(c, db)
        elif args.stage == 'run':
            run(c, db, args.limit)
        elif args.stage == 'verify':
            verify(c, db)
        elif args.stage == 'status':
            log(json.dumps({'done': db.execute('SELECT count(*) FROM chunks WHERE done=1').fetchone()[0],
                            'state': {k: json.loads(v) for k,v in db.execute('SELECT key,value FROM state')}}))
    except BaseException as exc:
        dump('last-error.json', {'stage': args.stage, 'type': type(exc).__name__, 'message': str(exc),
                                 'timestamp': datetime.datetime.now().astimezone().isoformat(),
                                 'automatic_rollback_performed': False})
        raise
    finally:
        db.close()
        c.close()


if __name__ == '__main__':
    main()
