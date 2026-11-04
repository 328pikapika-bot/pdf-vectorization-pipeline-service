"""Prepare a fresh deployment; every write stays within a new approved subdirectory."""
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path(sys.argv[1]).resolve()
PARENT = pathlib.Path('/data/agri-breeding-pdf-memories')
assert ROOT.parent == PARENT and ROOT.name.startswith('restore_')
SOURCE = pathlib.Path('/data')


def sha(path):
    with path.open('rb') as f:
        h = hashlib.sha256()
        while data := f.read(8 * 1024 * 1024):
            h.update(data)
        return h.hexdigest()


def write(name, value, mode=0o600):
    path = ROOT / name
    assert path.resolve().is_relative_to(ROOT)
    with path.open('x', encoding='utf-8') as f:
        f.write(value)
    path.chmod(mode)


def capture(command):
    return subprocess.check_output(command, text=True)


def main():
    started = time.time()
    ports = [19534, 18003, 19006, 19007, 9095]
    for port in ports:
        with socket.socket() as sock:
            sock.bind(('0.0.0.0', port))
    names = capture(['docker', 'ps', '-a', '--format', '{{.Names}}']).splitlines()
    planned = ['agri-breeding-pdf-memories-' + n for n in ['etcd', 'minio', 'milvus', 'attu', 'restore']]
    assert not set(names).intersection(planned), 'Container names occupied'
    assert shutil.disk_usage(ROOT).free > 100 * 1024**3
    for name in ['config', 'audit', 'package', 'etcd', 'minio', 'milvus']:
        (ROOT / name).mkdir(mode=0o700)
    original_manifest = ROOT / 'original-manifest.sha256'
    assert sha(original_manifest) == sha(SOURCE / '校验资料/文件校验清单.sha256')
    entries = []
    for line in original_manifest.read_text(encoding='utf-8').splitlines():
        digest, relative = line.split('  ', 1)
        p = pathlib.PurePosixPath(relative)
        assert not p.is_absolute() and '..' not in p.parts
        entries.append((digest, relative))
    assert len(entries) == len({r for _, r in entries}) == 127
    preserved = {}
    paths = {SOURCE / r for _, r in entries}
    paths.add(SOURCE / '校验资料/文件校验清单.sha256')
    paths.add(SOURCE / 'Dockerfile.minio')
    paths.update(p for p in PARENT.rglob('*') if p.is_file() and not p.is_relative_to(ROOT))
    for path in sorted(paths):
        if path.is_file():
            preserved[str(path)] = {'bytes': path.stat().st_size, 'sha256': sha(path)}
    write('audit/preflight.json', json.dumps({
        'started_unix': started, 'root': str(ROOT), 'preserved_files': preserved,
        'docker_ps': capture(['docker', 'ps', '-a', '--no-trunc']),
        'docker_images': capture(['docker', 'image', 'ls', '--no-trunc']),
        'docker_networks': capture(['docker', 'network', 'ls']),
        'disk_free': shutil.disk_usage(ROOT).free,
    }, ensure_ascii=False, indent=2))
    for digest, relative in entries:
        source = ROOT / 'original-compose.yaml' if relative == 'compose.yaml' else SOURCE / relative
        assert sha(source) == digest, 'Input checksum mismatch: ' + relative
        target = ROOT / 'package' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        assert not target.exists()
        shutil.copy2(source, target)
        assert sha(target) == digest, 'Copied checksum mismatch: ' + relative
        print('Verified package copy:', relative, flush=True)
    shutil.copy2(original_manifest, ROOT / 'package/校验资料/文件校验清单.sha256')
    secret = secrets.token_hex(32)
    account = 'agri_migration_admin'
    write('config/storage.env', 'MINIO_ROOT_USER=' + account + '\nMINIO_ROOT_PASSWORD=' + secret + '\n')
    write('config/milvus-user.yaml', f'''proxy:
  maxVectorFieldNum: 10
  maxFieldNum: 200
minio:
  address: minio
  port: 9000
  accessKeyID: {account}
  secretAccessKey: {secret}
  bucketName: a-bucket
  rootPath: files
''')
    write('config/restore-target.yaml', f'''configVersion: v2
log:
  level: info
  console: true
  file:
    path: /audit/milvus-backup.log
milvus:
  user: ""
  password: ""
  grpc:
    address: standalone
    port: 19530
    tlsMode: disabled
  rest:
    endpoint: http://standalone:19530
  management:
    endpoint: http://standalone:9091
  storage:
    provider: minio
    address: minio
    port: 9000
    useSSL: false
    bucketName: a-bucket
    rootPath: files
    auth:
      type: static
      accessKeyID: {account}
      secretAccessKey: {secret}
backup:
  storage:
    provider: minio
    address: minio
    port: 9000
    useSSL: false
    bucketName: a-bucket
    rootPath: migration_backup
    auth:
      type: static
      accessKeyID: {account}
      secretAccessKey: {secret}
  concurrency:
    collections: 1
    segments: 4
restore:
  concurrency:
    collections: 1
    importJobs: 4
transfer:
  mode: direct
  concurrency: 4
''')
    shutil.copy2(ROOT / 'restore_job.py', ROOT / 'config/restore_job.py')
    stamp = ROOT.name.removeprefix('restore_')
    compose = '''name: agri-breeding-restore-STAMP
services:
  etcd:
    container_name: agri-breeding-pdf-memories-etcd
    image: quay.io/coreos/etcd:v3.5.25
    restart: unless-stopped
    environment:
      ETCD_AUTO_COMPACTION_MODE: revision
      ETCD_AUTO_COMPACTION_RETENTION: "1000"
      ETCD_QUOTA_BACKEND_BYTES: "4294967296"
      ETCD_SNAPSHOT_COUNT: "50000"
    volumes:
      - ./etcd:/etcd
    command: etcd -advertise-client-urls=http://etcd:2379 -listen-client-urls http://0.0.0.0:2379 --data-dir /etcd
    healthcheck:
      test: ["CMD", "etcdctl", "endpoint", "health"]
      interval: 15s
      timeout: 10s
      retries: 10
  minio:
    container_name: agri-breeding-pdf-memories-minio
    image: minio/minio:RELEASE.2024-12-18T13-15-44Z
    restart: unless-stopped
    env_file: ./config/storage.env
    volumes:
      - ./minio:/minio_data
    command: server /minio_data --console-address :9001
    ports:
      - "127.0.0.1:19006:9000"
      - "127.0.0.1:19007:9001"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 15s
      timeout: 10s
      retries: 10
  standalone:
    container_name: agri-breeding-pdf-memories-milvus
    image: milvusdb/milvus:v2.6.9
    restart: unless-stopped
    command: ["milvus", "run", "standalone"]
    security_opt:
      - seccomp:unconfined
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000
      MQ_TYPE: rocksmq
    volumes:
      - ./milvus:/var/lib/milvus
      - ./config/milvus-user.yaml:/milvus/configs/user.yaml:ro
    ports:
      - "127.0.0.1:19534:19530"
      - "127.0.0.1:9095:9091"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9091/healthz"]
      interval: 20s
      start_period: 180s
      timeout: 15s
      retries: 10
    depends_on:
      etcd:
        condition: service_healthy
      minio:
        condition: service_healthy
  attu:
    container_name: agri-breeding-pdf-memories-attu
    image: zilliz/attu:v2.6.5
    restart: unless-stopped
    environment:
      MILVUS_URL: standalone:19530
    ports:
      - "127.0.0.1:18003:3000"
    depends_on:
      standalone:
        condition: service_healthy
  receiver:
    container_name: agri-breeding-pdf-memories-restore
    image: agri-breeding-restore:STAMP
    profiles: [migration]
    build:
      context: ./package/工具及配置模板
      dockerfile: Dockerfile.restore
    environment:
      PYTHONUNBUFFERED: "1"
      PYTHONDONTWRITEBYTECODE: "1"
    volumes:
      - ./package:/package:ro
      - ./config:/config:ro
      - ./audit:/audit
    working_dir: /audit
    command: ["python3", "/config/restore_job.py"]
    depends_on:
      standalone:
        condition: service_healthy
'''.replace('STAMP', stamp)
    write('compose.yaml', compose)
    write('audit/preparation.json', json.dumps({'prepared_unix': time.time(), 'package_files_verified': len(entries),
                                               'original_files_preserved': len(preserved), 'deployment': str(ROOT)}, indent=2))
    subprocess.run(['docker', 'compose', '-f', str(ROOT / 'compose.yaml'), 'config', '--quiet'], check=True)
    print('PREPARATION COMPLETE:', ROOT, flush=True)


if __name__ == '__main__':
    main()
