"""Detached restore: verified upload, official restore, full row-by-row acceptance."""
import concurrent.futures
import hashlib
import importlib.util
import json
import pathlib
import sys
import time
import traceback

PACKAGE = pathlib.Path('/package')
AUDIT = pathlib.Path('/audit')
CONFIG = pathlib.Path('/config/restore-target.yaml')
SPEC = importlib.util.spec_from_file_location('receiver', PACKAGE / '工具及配置模板/milvus_migration_receiver.py')
receiver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receiver)


def save(name, value):
    path = AUDIT / name
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def stage(name, **extra):
    save('progress.json', {'stage': name, 'updated_unix': time.time(), **extra})
    print('STAGE:', name, extra, flush=True)


def upload_verified(cfg):
    m = receiver.storage(cfg)
    s = cfg['backup']['storage']
    bucket = s['bucketName']
    if not m.bucket_exists(bucket):
        m.make_bucket(bucket)
    identity = receiver.load(PACKAGE / '校验资料/交付标识.json')
    directory = PACKAGE / '备份数据' / identity['backup_name']
    prefix = s['rootPath'].strip('/') + '/' + identity['backup_name'] + '/'
    files = sorted(p for p in directory.rglob('*') if p.is_file())
    assert len(files) == 101
    expected_keys = {prefix + p.relative_to(directory).as_posix() for p in files}
    existing = {o.object_name for o in m.list_objects(bucket, prefix=prefix, recursive=True)}
    assert existing <= expected_keys, 'Unexpected objects: refusing overwrite'

    def one(p):
        key = prefix + p.relative_to(directory).as_posix()
        digest = receiver.sha(p)
        if key not in existing:
            m.fput_object(bucket, key, str(p))
        assert m.stat_object(bucket, key).size == p.stat().st_size
        response = m.get_object(bucket, key)
        try:
            remote_digest = hashlib.sha256()
            while data := response.read(8 * 1024 * 1024):
                remote_digest.update(data)
            assert remote_digest.hexdigest() == digest, 'Uploaded object content mismatch: ' + key
        finally:
            response.close()
            response.release_conn()
        return {'key': key, 'bytes': p.stat().st_size, 'sha256': digest}

    receipts = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(one, p) for p in files]
        for future in concurrent.futures.as_completed(futures):
            receipts.append(future.result())
            print('Object SHA256 verified:', len(receipts), '/101', flush=True)
            stage('upload', objects_verified=len(receipts), total_objects=101)
    assert len(list(m.list_objects(bucket, prefix=prefix, recursive=True))) == 101
    save('uploaded-object-sha256.json', sorted(receipts, key=lambda r: r['key']))


def main():
    started = time.time()
    try:
        stage('verify-package')
        receiver.verify_files(PACKAGE)
        cfg = receiver.configuration(CONFIG)
        c = receiver.client(cfg)
        try:
            assert c.get_server_version() == '2.6.9'
            receiver.require_absent(c)
        finally:
            c.close()
        stage('upload')
        upload_verified(cfg)
        stage('restore-and-rebuild-index')
        receiver.restore(PACKAGE, cfg, CONFIG, pathlib.Path('/usr/local/bin/milvus-backup'))
        stage('full-verification')
        receiver.verify_restored(PACKAGE, cfg, full=True)
        result = {'success': True, 'started_unix': started, 'completed_unix': time.time(),
                  'elapsed_seconds': time.time()-started, 'records': 1413886, 'articles': 29167,
                  'fields': 28, 'uploaded_objects_sha256_verified': 101,
                  'original_8_fields_including_vectors_full_verified': True,
                  'new_20_fields_full_verified': True, 'differences': 0,
                  'baseline_version': '2026-10-10 backup', 'current_source_verified': False}
        save('restore-result.json', result)
        stage('completed', **result)
        print('RESTORE AND FULL VERIFICATION COMPLETE', flush=True)
    except BaseException as exc:
        save('restore-error.json', {'success': False, 'time': time.time(), 'error': str(exc),
                                   'traceback': traceback.format_exc(), 'automatic_cleanup': False})
        stage('failed', error=str(exc))
        raise


if __name__ == '__main__':
    main()
