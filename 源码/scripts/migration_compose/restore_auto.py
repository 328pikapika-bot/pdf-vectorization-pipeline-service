"""Generate an internal target config and run the guarded receiver."""

import pathlib
import subprocess
import sys

import yaml
from minio import Minio


PACKAGE = pathlib.Path('/package')
KIT = PACKAGE / '工具及配置模板'
CONFIG = pathlib.Path('/tmp/restore-target.yaml')
SECRET = pathlib.Path('/secrets/minio-secret')


def main():
    assert len(sys.argv) == 2 and sys.argv[1] in {'init-storage', 'migrate', 'verify-restored', 'verify-full'}
    password = SECRET.read_text(encoding='ascii').strip()
    assert len(password) == 64 and all(c in '0123456789abcdef' for c in password)
    if sys.argv[1] == 'init-storage':
        client = Minio('minio:9000', access_key='agri_migration_admin', secret_key=password)
        if not client.bucket_exists('a-bucket'):
            client.make_bucket('a-bucket')
        print('Target MinIO bucket ready', flush=True)
        return
    cfg = yaml.safe_load((KIT / 'restore-target.example.yaml').read_text(encoding='utf-8'))
    cfg['milvus']['grpc'].update(address='milvus', port=19530)
    cfg['milvus']['rest']['endpoint'] = 'http://milvus:19530'
    cfg['milvus']['management']['endpoint'] = 'http://milvus:9091'
    for section in [cfg['milvus']['storage'], cfg['backup']['storage']]:
        section.update(address='minio', port=9000, bucketName='a-bucket', useSSL=False)
        section['auth'].update(accessKeyID='agri_migration_admin', secretAccessKey=password)
    cfg['milvus']['storage']['rootPath'] = 'files'
    cfg['backup']['storage']['rootPath'] = 'migration_backup'
    CONFIG.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding='utf-8')
    CONFIG.chmod(0o600)
    action = 'migrate' if sys.argv[1] == 'migrate' else 'verify-restored'
    command = [sys.executable, str(KIT / 'milvus_migration_receiver.py'), action,
               '--package', str(PACKAGE), '--config', str(CONFIG)]
    if action == 'migrate':
        command += ['--binary', '/usr/local/bin/milvus-backup']
    if sys.argv[1] == 'verify-full':
        command.append('--full')
    subprocess.run(command, check=True)


if __name__ == '__main__':
    main()
