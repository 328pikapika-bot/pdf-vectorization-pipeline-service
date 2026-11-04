"""Receiver-side verification and guarded restore for this migration package.

No drop/delete/overwrite operation is provided. Fill a target configuration first.
"""
import argparse
import gzip
import hashlib
import ipaddress
import json
import pathlib
import socket
import struct
import subprocess
import sys
from urllib.parse import urlsplit

SOURCE_IP = '127.0.0.1'
DATABASE = 'agri_pdf_memories'
COLLECTION = 'agri_breeding_pdf_chunk'
OLD = ['id', 'pdf_index', 'chunk_index', 'text_embed', 'text', 'char_start', 'char_end', 'section_path']
NEW = ['gid', 'title', 'year', 'volume', 'issue', 'doi', 'journal_name', 'issn', 'author',
       'author_add', 'keyword', 'abstracts', 'indexed_by', 'url', 'language', 'published_date',
       'start_page', 'end_page', 'publisher', 'hasFullText']


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def relative_path(root, relative):
    pure = pathlib.PurePosixPath(relative)
    assert not pure.is_absolute() and '..' not in pure.parts and '\\' not in relative and ':' not in relative
    path = root.joinpath(*pure.parts)
    assert path.resolve().is_relative_to(root.resolve())
    return path


def verify_files(root):
    manifest = root / '校验资料/文件校验清单.sha256'
    assert manifest.is_file()
    names = set()
    for line in manifest.read_text(encoding='utf-8').splitlines():
        digest, relative = line.split('  ', 1)
        assert relative not in names
        names.add(relative)
        assert sha(relative_path(root, relative)) == digest, 'File SHA256 mismatch: ' + relative
    print(f'文件校验通过：{len(names)} 个文件（清单本身由外部 ZIP SHA256 保护）', flush=True)


def reject_source_host(host):
    assert host and 'TARGET_' not in host and 'FILL_' not in host, 'Target host placeholder is not filled'
    resolved = {item[4][0] for item in socket.getaddrinfo(host, None)}
    normalized = set()
    for address in resolved:
        ip = ipaddress.ip_address(address)
        normalized.add(str(ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip))
    assert SOURCE_IP not in normalized and host != SOURCE_IP, 'Refusing connection to source server'


def configuration(path):
    import yaml
    cfg = yaml.safe_load(path.read_text(encoding='utf-8'))
    reject_source_host(cfg['milvus']['grpc']['address'])
    for key in ['rest', 'management']:
        endpoint = urlsplit(cfg['milvus'][key]['endpoint'])
        assert endpoint.scheme in ['http', 'https'] and endpoint.hostname
        reject_source_host(endpoint.hostname)
    for section in [cfg['milvus']['storage'], cfg['backup']['storage']]:
        reject_source_host(section['address'])
        assert section['auth']['accessKeyID'] and section['auth']['secretAccessKey']
        assert 'TARGET_' not in section['auth']['accessKeyID'] and 'TARGET_' not in section['auth']['secretAccessKey']
        assert section['bucketName'] and section['rootPath']
        assert 'TARGET_' not in section['bucketName'] and 'TARGET_' not in section['rootPath']
        assert '..' not in pathlib.PurePosixPath(section['rootPath']).parts
    return cfg


def client(cfg):
    from pymilvus import MilvusClient
    grpc = cfg['milvus']['grpc']
    assert grpc['tlsMode'] == 'disabled', 'This template is for private-network, non-TLS targets; configure TLS separately'
    return MilvusClient(uri=f"http://{grpc['address']}:{grpc['port']}",
                        user=cfg['milvus'].get('user', ''), password=cfg['milvus'].get('password', ''))


def require_absent(c):
    if DATABASE in c.list_databases():
        c.use_database(DATABASE)
        assert not c.has_collection(COLLECTION), 'Target collection exists; no overwrite is allowed'


def storage(cfg):
    from minio import Minio
    s = cfg['backup']['storage']
    return Minio(s['address'] + ':' + str(s['port']), secure=s['useSSL'],
                 access_key=s['auth']['accessKeyID'], secret_key=s['auth']['secretAccessKey'])


def upload(root, cfg):
    verify_files(root)
    c = client(cfg)
    try:
        require_absent(c)
    finally:
        c.close()
    identity = load(root / '校验资料/交付标识.json')
    m = storage(cfg)
    s = cfg['backup']['storage']
    bucket = s['bucketName']
    assert m.bucket_exists(bucket), 'Create the target backup bucket before upload'
    prefix = s['rootPath'].strip('/') + '/' + identity['backup_name'] + '/'
    assert not list(m.list_objects(bucket, prefix=prefix, recursive=True)), 'Backup prefix exists; refusing overwrite'
    directory = root / '备份数据' / identity['backup_name']
    for p in sorted(directory.rglob('*')):
        if p.is_file():
            key = prefix + p.relative_to(directory).as_posix()
            m.fput_object(bucket, key, str(p))
            assert m.stat_object(bucket, key).size == p.stat().st_size
            print('Uploaded:', p.relative_to(directory), flush=True)
    print('备份上传完成；未恢复、未修改任何集合。', flush=True)


def restore(root, cfg, config_path, binary):
    verify_files(root)
    c = client(cfg)
    try:
        assert c.get_server_version() == '2.6.9', 'Default receiver supports Milvus 2.6.9; validate other versions separately'
        require_absent(c)
    finally:
        c.close()
    assert binary.is_file()
    help_text = subprocess.check_output([str(binary), 'restore', '--help'], text=True)
    assert '--rebuild_index' in help_text and '--filter' in help_text
    identity = load(root / '校验资料/交付标识.json')
    m = storage(cfg)
    section = cfg['backup']['storage']
    prefix = section['rootPath'].strip('/') + '/' + identity['backup_name'] + '/'
    remote = {o.object_name: o for o in m.list_objects(section['bucketName'], prefix=prefix, recursive=True)}
    expected = load(root / '校验资料/备份对象清单.json')
    for item in expected:
        key = prefix + item['key'].split('/' + identity['backup_name'] + '/', 1)[1]
        assert key in remote and remote[key].size == item['bytes'], 'Target backup missing or wrong size'
    assert len(remote) == len(expected)
    command = [str(binary), '--config', str(config_path), 'restore', '-n', identity['backup_name'],
               '--filter', DATABASE + '.' + COLLECTION, '--rebuild_index']
    print('恢复到目标服务器；重建索引，不重新计算向量。', flush=True)
    subprocess.run(command, check=True)
    c = client(cfg)
    try:
        c.use_database(DATABASE)
        c.load_collection(COLLECTION, timeout=3600)
    finally:
        c.close()


def fingerprint(row):
    h = hashlib.sha256()
    for f in OLD:
        raw = (struct.pack('<' + 'f'*len(row[f]), *row[f]) if f == 'text_embed' else
               json.dumps(row[f], ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        h.update(struct.pack('<Q', len(raw)))
        h.update(raw)
    return h.digest()


def field_shape(schema):
    return [(f['name'], int(f['type']), f['params'], bool(f.get('nullable', False)),
             bool(f.get('is_primary', False))) for f in schema['fields']]


def verify_restored(root, cfg, full):
    import sqlite3
    c = client(cfg)
    try:
        c.use_database(DATABASE)
        expected_schema = load(root / '校验资料/schema.json')
        assert field_shape(c.describe_collection(COLLECTION)) == field_shape(expected_schema)
        count = c.query(COLLECTION, filter='', output_fields=['count(*)'], consistency_level='Strong')[0]['count(*)']
        assert count == 1413886
        indexes = [c.describe_index(COLLECTION, n) for n in c.list_indexes(COLLECTION)]
        index = next(i for i in indexes if i['field_name'] == 'text_embed')
        assert index['index_type'] == 'HNSW' and index['metric_type'] == 'COSINE' and index['state'] == 'Finished'
        assert int(index['params']['M']) == 32 and int(index['params']['efConstruction']) == 800
        sample_id = load(root / '校验资料/交付标识.json')['sample_id']
        row = c.get(COLLECTION, [sample_id], output_fields=OLD+NEW, consistency_level='Strong')[0]
        articles = {}
        with gzip.open(root / '校验资料/matched-articles.jsonl.gz', 'rt', encoding='utf-8') as stream:
            for line in stream:
                data = json.loads(line)
                articles[data['gid']] = data
        def check_metadata(r):
            gid = r['pdf_index'][:-4] if r['pdf_index'].endswith('.pdf') else r['pdf_index']
            assert all(r.get(f) == articles[gid][f] for f in NEW), r['id']
        check_metadata(row)
        fields = ['pdf_index']+NEW
        hits = c.search(COLLECTION, data=[row['text_embed']], anns_field='text_embed', limit=3,
                        search_params={'metric_type':'COSINE', 'params':{'ef':64}},
                        output_fields=fields, consistency_level='Strong')[0]
        assert len(hits) == 3
        for hit in hits:
            check_metadata({'id':hit['id'], **hit['entity']})
        if full:
            baseline = sqlite3.connect('file:' + (root / '校验资料/original-eight-fields-sha256.sqlite').as_posix() + '?mode=ro', uri=True)
            iterator = c.query_iterator(COLLECTION, batch_size=100, output_fields=OLD+NEW, consistency_level='Strong')
            total = 0
            seen = set()
            try:
                while rows := iterator.next():
                    for r in rows:
                        assert r['id'] not in seen, 'Duplicate primary key in verification'
                        seen.add(r['id'])
                        digest = baseline.execute('SELECT sha256 FROM row_digest WHERE id=?', (r['id'],)).fetchone()
                        assert digest and digest[0] == fingerprint(r), 'Original fields differ: ' + r['id']
                        check_metadata(r)
                    total += len(rows)
                    if total % 10000 == 0:
                        print('Verified', total, '/ 1413886', flush=True)
                assert total == count
            finally:
                iterator.close();baseline.close()
        print('恢复检查通过：28字段、1413886记录、索引及新增信息检索正常。全量逐条核验：' + str(full), flush=True)
    finally:
        c.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['verify-files', 'check-target', 'upload', 'restore', 'verify-restored', 'migrate'])
    parser.add_argument('--package', type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent.parent)
    parser.add_argument('--config', type=pathlib.Path)
    parser.add_argument('--binary', type=pathlib.Path)
    parser.add_argument('--full', action='store_true')
    args = parser.parse_args()
    root = args.package.resolve()
    if args.action == 'verify-files':
        verify_files(root);return
    assert args.config, '--config is required'
    cfg = configuration(args.config)
    if args.action == 'migrate':
        assert args.binary, '--binary is required'
        print('Step 1/5: verify local package', flush=True)
        verify_files(root)
        print('Step 2/5: check target version and collection', flush=True)
        c = client(cfg)
        try:
            assert c.get_server_version() == '2.6.9'
            require_absent(c)
        finally:
            c.close()
        print('Step 3/5: upload backup objects', flush=True)
        upload(root, cfg)
        print('Step 4/5: restore and rebuild index', flush=True)
        restore(root, cfg, args.config.resolve(), args.binary.resolve())
        print('Step 5/5: verify restored collection', flush=True)
        verify_restored(root, cfg, args.full)
    elif args.action == 'check-target':
        c = client(cfg)
        try:
            assert c.get_server_version() == '2.6.9'
            require_absent(c)
            print('目标检查通过；集合不存在，可以按说明迁移。')
        finally:
            c.close()
    elif args.action == 'upload':
        upload(root, cfg)
    elif args.action == 'restore':
        assert args.binary, '--binary is required'
        restore(root, cfg, args.config.resolve(), args.binary.resolve())
    elif args.action == 'verify-restored':
        verify_restored(root, cfg, args.full)


if __name__ == '__main__':
    main()
