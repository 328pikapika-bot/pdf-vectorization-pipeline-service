"""Fixed-target, non-destructive backup/download/ZIP64 handoff workflow."""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import pathlib
import re
import select
import shutil
import socket
import subprocess
import threading
import time
import zipfile

import requests
import urllib3
import yaml
from minio import Minio
from pymilvus import MilvusClient

PROJECT = pathlib.Path(__file__).resolve().parents[1]
TOOLS = pathlib.Path(r'E:\milvus_backups\tools\milvus-backup-0.6.0')
PRIOR = pathlib.Path(r'E:\milvus_backups\agri_pdf_memories\agri_breeding_pdf_chunk\20261010_195051')
NAME = 'agri_breeding_pdf_chunk_28fields_20261010_224223'
ROOT = pathlib.Path(r'E:\milvus_exports') / NAME
WORK = ROOT.parent / '_work' / NAME
DB = 'agri_pdf_memories'
COLL = 'agri_breeding_pdf_chunk'
URI = 'http://127.0.0.1:19534'
COUNT = 1413886
PREFIX = 'backup_agri_breeding/' + NAME + '/'
BUCKET = 'a-bucket'
NEW = ['gid', 'title', 'year', 'volume', 'issue', 'doi', 'journal_name', 'issn',
       'author', 'author_add', 'keyword', 'abstracts', 'indexed_by', 'url',
       'language', 'published_date', 'start_page', 'end_page', 'publisher', 'hasFullText']
OLD = ['id', 'pdf_index', 'chunk_index', 'text_embed', 'text', 'char_start', 'char_end', 'section_path']


def log(message):
    print(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds'), message, flush=True)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def storage():
    cfg = yaml.safe_load((TOOLS / 'backup-direct.yaml').read_text(encoding='utf-8'))
    s = cfg['backup']['storage']
    assert s['address'] == '127.0.0.1' and s['bucketName'] == BUCKET
    client = Minio(s['address'] + ':' + str(s['port']),
                   access_key=s['auth']['accessKeyID'], secret_key=s['auth']['secretAccessKey'],
                   secure=s['useSSL'], http_client=urllib3.PoolManager(
                       timeout=urllib3.Timeout(connect=10, read=60), retries=3))
    return cfg, client


def snapshot():
    c = MilvusClient(uri=URI, db_name=DB)
    try:
        s = c.describe_collection(COLL)
        assert s == load(PRIOR / 'enrichment/schema-final.json'), 'Source schema changed'
        count = c.query(COLL, filter='', output_fields=['count(*)'], consistency_level='Strong')[0]['count(*)']
        assert count == COUNT
        r = requests.get('http://127.0.0.1:9095/metrics', timeout=30)
        r.raise_for_status()
        counters = sorted(line for line in r.text.splitlines() if
                          line.startswith(('milvus_proxy_insert_vectors_count{', 'milvus_proxy_delete_vectors_count{', 'milvus_proxy_upsert_vectors_count{'))
                          and 'collection_name="' + COLL + '"' in line)
        assert counters, 'No mutation counters available'
        return {'schema': s, 'logical_count': count, 'mutation_counters': counters,
                'milvus_version': c.get_server_version(),
                'indexes': [c.describe_index(COLL, n) for n in c.list_indexes(COLL)]}
    finally:
        c.close()


def relay(port):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(64)

    def connection(client):
        upstream = None
        try:
            upstream = socket.create_connection(('127.0.0.1', port), timeout=15)
            upstream.settimeout(None)
            while True:
                for source in select.select([client, upstream], [], [], 60)[0]:
                    block = source.recv(65536)
                    if not block:
                        return
                    (upstream if source is client else client).sendall(block)
        except OSError:
            pass
        finally:
            client.close()
            if upstream:
                upstream.close()

    def accept():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=connection, args=(client,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return listener


def backup():
    assert not ROOT.exists() and not WORK.exists(), 'Delivery directory already exists'
    cfg, m = storage()
    assert not list(m.list_objects(BUCKET, prefix=PREFIX, recursive=True)), 'Backup prefix already exists'
    before = snapshot()
    assert before['milvus_version'] == '2.6.9'
    release = TOOLS / 'milvus-backup_0.6.0_Linux_x86_64.tar.gz'
    assert sha(release) == '3b1e1296d58811f7f92722a748af7ad24eb96554d3644893fe1f130a2336cdef'
    ROOT.mkdir(parents=True)
    WORK.mkdir(parents=True)
    save(WORK / 'source-before.json', before)
    save(WORK / 'status.json', {'status': 'INCOMPLETE', 'backup_name': NAME})
    listeners = [relay(port) for port in [19534, 19006, 9095]]
    ports = [s.getsockname()[1] for s in listeners]
    host = 'host.docker.internal'
    cfg['milvus']['grpc'].update(address=host, port=ports[0])
    cfg['milvus']['rest']['endpoint'] = f'http://{host}:{ports[0]}'
    cfg['milvus']['management']['endpoint'] = f'http://{host}:{ports[2]}'
    for s in [cfg['milvus']['storage'], cfg['backup']['storage']]:
        s.update(address=host, port=ports[1])
    cfg['log']['file']['path'] = '/output/official-backup.log'
    (WORK / 'private-backup.yaml').write_text(yaml.safe_dump(cfg), encoding='utf-8')
    secrets = {cfg['backup']['storage']['auth']['accessKeyID'], cfg['backup']['storage']['auth']['secretAccessKey']}
    secrets.discard('')
    base = ['docker', 'run', '--rm', '--name', 'agri-migration-backup-20261010-224223',
            '--memory', '2g', '--entrypoint', '/tool/milvus-backup', '-v', str(TOOLS) + ':/tool:ro',
            '-v', str(WORK) + ':/output', 'python:3.14-slim', '--config', '/output/private-backup.yaml']
    start = time.monotonic()
    try:
        for action in ['check', 'create', 'get']:
            command = base + [action]
            if action in ['create', 'get']:
                command += ['-n', NAME]
            if action == 'create':
                command += ['--filter', DB + '.' + COLL, '--format', 'binlog', '--strategy', 'bulk_flush']
            log('Official tool: ' + action)
            p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace')
            with (WORK / (action + '.log')).open('w', encoding='utf-8') as out:
                for line in p.stdout:
                    for secret in secrets:
                        line = line.replace(secret, '[REDACTED]')
                    out.write(line)
                    out.flush()
                    if any(word in line.lower() for word in ['success', 'resume collection gc', 'error', 'panic', 'duration:']):
                        print(line.rstrip(), flush=True)
            assert p.wait() == 0, 'Official tool failed: ' + action
        console = (WORK / 'create.log').read_text(encoding='utf-8')
        assert 'create backup success' in console and 'resume collection gc done' in console
        after = snapshot()
        assert before['schema'] == after['schema'] and before['mutation_counters'] == after['mutation_counters']
        save(WORK / 'source-after.json', after)
        save(WORK / 'backup-run.json', {'success': True, 'seconds': time.monotonic()-start,
                                      'source_values_not_written': True, 'gc_resumed': True})
        log('NEW BACKUP COMPLETE; source schema/count/DML unchanged')
    finally:
        for listener in listeners:
            listener.close()


def objects(m):
    return sorted([{'key': o.object_name, 'bytes': o.size, 'etag': o.etag,
                    'version_id': o.version_id} for o in m.list_objects(BUCKET, prefix=PREFIX, recursive=True)],
                  key=lambda o: o['key'])


def validate_metadata(m, listing):
    all_objects = {o['key']: o for o in listing}
    meta = {}
    for name in ['backup_meta.json', 'collection_meta.json', 'partition_meta.json', 'segment_meta.json', 'full_meta.json']:
        response = m.get_object(BUCKET, PREFIX + 'meta/' + name)
        try:
            meta[name] = json.loads(response.read())
        finally:
            response.close()
            response.release_conn()
    full = meta['full_meta.json']
    assert full['name'] == NAME and meta['backup_meta.json']['state_code'] == 0
    assert len(full['collection_backups']) == 1
    c = full['collection_backups'][0]
    assert c['collection_name'] == COLL and c['db_name'] == DB
    schema = load(WORK / 'source-before.json')['schema']
    fields = c['schema']['fields']
    assert len(fields) == 28 and [f['name'] for f in fields] == OLD + NEW
    for bf, sf in zip(fields, schema['fields']):
        assert bf['data_type'] == sf['type']
        assert bool(bf.get('nullable', False)) == bool(sf.get('nullable', False)), bf['name']
        assert bool(bf.get('is_primary_key', False)) == bool(sf.get('is_primary', False))
        assert {v['key']: v['value'] for v in bf.get('type_params', [])} == {k:str(v) for k,v in sf['params'].items()}
    refs = set()
    segments = [s for p in c['partition_backups'] for s in p['segment_backups']]
    for s in segments:
        for kind, fk in [('insert_log', 'binlogs'), ('delta_log', 'deltalogs')]:
            for field in s.get(fk, []):
                for item in field.get('binlogs', []):
                    group = str(s['group_id']) + '/' if kind == 'insert_log' or s['partition_id'] != -1 else ''
                    key = PREFIX + f"binlogs/{kind}/{s['collection_id']}/{s['partition_id']}/" + group + str(s['segment_id']) + '/'
                    if kind == 'insert_log':
                        key += str(field.get('fieldID', field.get('field_id', 0))) + '/'
                    key += str(item['log_id'])
                    assert all_objects[key]['bytes'] == item['log_size'] > 0, key
                    refs.add(key)
    assert set(all_objects) == refs | {PREFIX + 'meta/' + n for n in meta}, 'Unreferenced/missing objects'
    return {'success': True, 'backup_name': NAME, 'logical_rows': COUNT, 'fields': 28,
            'segment_count': len(segments), 'referenced_data_files': len(refs),
            'delete_log_files': sum('/delta_log/' in k for k in refs),
            'physical_segment_rows': sum(s.get('num_of_rows', 0) for s in segments),
            'backup_bytes': sum(o['bytes'] for o in listing), 'object_count': len(listing),
            'restore_rehearsal_performed': False, 'backup_gc_resumed': True}


def download():
    assert load(WORK / 'backup-run.json')['success']
    _, m = storage()
    listing = objects(m)
    report = validate_metadata(m, listing)
    save(WORK / 'backup-validation.json', report)
    manifest_path = WORK / 'remote-objects.json'
    if manifest_path.exists():
        assert load(manifest_path) == listing, 'Backup changed since previous attempt'
    else:
        save(manifest_path, listing)
    total = report['backup_bytes']
    assert shutil.disk_usage(ROOT).free > 2*total + 5*1024**3, 'Insufficient free space'
    log(f'Validated {len(listing)} objects; {total/1024**3:.2f} GiB; 28 fields and all references verified')
    dest = ROOT / '备份数据' / NAME
    lock = threading.Lock()
    progress = {'bytes': 0, 'files': 0}
    start = time.monotonic()

    def one(obj):
        relative = pathlib.PurePosixPath(obj['key'][len(PREFIX):])
        assert not relative.is_absolute() and '..' not in relative.parts
        path = dest.joinpath(*relative.parts)
        receipt = WORK / 'download-receipts' / (hashlib.sha256(obj['key'].encode()).hexdigest() + '.json')
        if path.exists() and receipt.exists():
            saved = load(receipt)
            assert saved['object'] == obj and path.stat().st_size == obj['bytes'] and sha(path) == saved['sha256']
            with lock:
                progress['bytes'] += obj['bytes']; progress['files'] += 1
            return saved
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + '.part')
        if part.exists():
            assert part.stat().st_size <= obj['bytes']
            with lock:
                progress['bytes'] += part.stat().st_size
        for attempt in range(5):
            response = None
            try:
                current = m.stat_object(BUCKET, obj['key'])
                assert current.size == obj['bytes'] and current.etag == obj['etag']
                offset = part.stat().st_size if part.exists() else 0
                if offset < obj['bytes']:
                    response = m.get_object(BUCKET, obj['key'], offset=offset)
                    with part.open('ab') as out:
                        while block := response.read(4*1024*1024):
                            out.write(block)
                            with lock:
                                progress['bytes'] += len(block)
                assert part.stat().st_size == obj['bytes'], 'Incomplete download'
                after = m.stat_object(BUCKET, obj['key'])
                assert after.size == obj['bytes'] and after.etag == obj['etag']
                digest = sha(part)
                if re.fullmatch('[0-9a-fA-F]{32}', obj['etag'] or ''):
                    with part.open('rb') as stream:
                        assert hashlib.file_digest(stream, 'md5').hexdigest() == obj['etag'].lower(), 'Object MD5 differs'
                part.replace(path)
                saved = {'object': obj, 'sha256': digest, 'path': path.relative_to(ROOT).as_posix()}
                save(receipt, saved)
                with lock:
                    progress['files'] += 1
                return saved
            except (OSError, urllib3.exceptions.HTTPError) as exc:
                if attempt == 4:
                    raise
                log(f'Download transport retry {attempt+1}: {type(exc).__name__}')
                time.sleep(min(2**attempt, 16))
            finally:
                if response:
                    response.close(); response.release_conn()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(one, obj) for obj in listing]
        next_log = 0
        while any(not f.done() for f in futures):
            for f in futures:
                if f.done() and f.exception():
                    raise f.exception()
            if time.monotonic() >= next_log:
                elapsed = time.monotonic()-start
                with lock:
                    done, files = progress['bytes'], progress['files']
                speed = done / elapsed if elapsed else 0
                eta = (total-done) / speed / 60 if speed else 0
                log(f'DOWNLOAD {done/total:.1%}; {files}/{len(listing)} files; {speed/1024**2:.1f} MiB/s; ETA {eta:.1f} min')
                next_log = time.monotonic()+40
            time.sleep(1)
        receipts = [f.result() for f in futures]
    assert objects(m) == listing, 'Remote backup objects changed during download'
    current = snapshot()
    before = load(WORK / 'source-before.json')
    assert current['schema'] == before['schema'] and current['mutation_counters'] == before['mutation_counters']
    save(WORK / 'download-files.json', receipts)
    save(WORK / 'source-final.json', current)
    save(WORK / 'download-result.json', {'success': True, 'seconds': time.monotonic()-start, 'bytes': total})
    log('ALL DOWNLOADS SHA256-CHECKED; source unchanged')


def package():
    assert load(WORK / 'download-result.json')['success']
    from migration_package_docs import create_docs
    create_docs(ROOT, WORK, PRIOR, TOOLS, NAME)
    expected = {p.relative_to(ROOT).as_posix(): {'bytes':p.stat().st_size, 'sha256':sha(p)}
                for p in sorted(ROOT.rglob('*')) if p.is_file()}
    checksum = ROOT / '校验资料' / '文件校验清单.sha256'
    checksum.write_text(''.join(v['sha256'] + '  ' + k + '\n' for k,v in expected.items()), encoding='utf-8')
    expected[checksum.relative_to(ROOT).as_posix()] = {'bytes':checksum.stat().st_size, 'sha256':sha(checksum)}
    save(WORK / 'package-files.json', expected)
    target = ROOT.with_suffix('.zip')
    part = target.with_name(target.name + '.part')
    assert not target.exists() and not part.exists(), 'Archive already exists'
    start = time.monotonic()
    with zipfile.ZipFile(part, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for i, (relative, info) in enumerate(expected.items()):
            h = hashlib.sha256()
            with (ROOT / relative).open('rb') as source, z.open(NAME + '/' + relative, 'w', force_zip64=True) as out:
                while block := source.read(4*1024*1024):
                    h.update(block);out.write(block)
            assert h.hexdigest() == info['sha256'], 'File changed during packaging'
            if i % 100 == 0:
                log(f'ZIP WRITE {i+1}/{len(expected)} files')
    with zipfile.ZipFile(part) as z:
        assert len(z.infolist()) == len(expected)
        for i, (relative, info) in enumerate(expected.items()):
            zi = z.getinfo(NAME + '/' + relative)
            assert zi.file_size == info['bytes']
            with z.open(zi) as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == info['sha256'], 'Archive content mismatch'
            if i % 100 == 0:
                log(f'ZIP VERIFY {i+1}/{len(expected)} files')
    digest = sha(part)
    current = snapshot()
    before = load(WORK / 'source-before.json')
    assert current['schema'] == before['schema'] and current['mutation_counters'] == before['mutation_counters']
    _, m = storage()
    old_objects = list(m.list_objects(BUCKET, prefix='backup_agri_breeding/agri_breeding_pdf_chunk_20261010_195051/', recursive=True))
    assert len(old_objects) == 89 and sum(o.size for o in old_objects) == 7175411102, 'Old backup retention differs'
    part.replace(target)
    target.with_suffix('.zip.sha256').write_text(digest + '  ' + target.name + '\n', encoding='utf-8')
    result = {'success': True, 'directory':str(ROOT), 'archive':str(target), 'archive_bytes':target.stat().st_size,
              'archive_sha256':digest, 'files':len(expected), 'zip_contents_sha256_verified':True,
              'logical_rows':COUNT, 'fields':28, 'source_unchanged':True,
              'backup_retained':PREFIX, 'old_backup_retained':True, 'restore_rehearsal_performed':False,
              'package_seconds':time.monotonic()-start,
              'backup':load(WORK / 'backup-validation.json'), 'download':load(WORK / 'download-result.json')}
    save(ROOT.parent / (NAME + '_交付完成.json'), result)
    save(WORK / 'status.json', result)
    log('DELIVERY COMPLETE: ' + str(target))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['backup', 'download', 'package'])
    args = parser.parse_args()
    try:
        globals()[args.stage]()
    except BaseException as exc:
        if WORK.exists():
            save(WORK / 'last-error.json', {'stage':args.stage, 'type':type(exc).__name__,
                                           'message':str(exc), 'automatic_restore':False})
        raise


if __name__ == '__main__':
    main()
