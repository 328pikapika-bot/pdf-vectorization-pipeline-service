"""Continue the verified build using a tested PyPI mirror, without global changes."""
import importlib.util
import json
import pathlib
import shutil
import socket
import subprocess
import traceback

ROOT = pathlib.Path('/data/agri-breeding-pdf-memories/restore_20261013_115719')
spec = importlib.util.spec_from_file_location('resume', ROOT / 'resume_verified_amd64.py')
resume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resume)


def main():
    names = set(resume.run(['docker', 'ps', '-a', '--format', '{{.Names}}'], True).splitlines())
    assert not names & {'agri-breeding-pdf-memories-' + n for n in ('etcd', 'minio', 'milvus', 'attu', 'restore')}
    for directory in ('etcd', 'minio', 'milvus'):
        assert not list((ROOT / directory).iterdir())
    for port in (19534, 18003, 19006, 19007, 9095):
        with socket.socket() as sock:
            sock.bind(('0.0.0.0', port))
    assert shutil.disk_usage(ROOT).free > 100 * 1024**3
    verified = json.loads((ROOT / 'audit/verified-images.json').read_text())
    assert verified['minio_binary_sha256_verified'] and verified['minio_official_binary_sha256'] == resume.MINIO_BINARY_SHA
    for image, key in ((resume.MINIO, 'minio_image_id'), (resume.PYTHON, 'python_image_id')):
        details = json.loads(resume.run(['docker', 'image', 'inspect', image], True))[0]
        assert details['Id'] == verified[key]
    resume.status('build-restore-tool-fast', dependency_mirror='https://pypi.tuna.tsinghua.edu.cn/simple')
    resume.run(['docker', 'build', '--progress=plain', '--build-arg',
                'PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple', '-f',
                str(ROOT / 'config/Dockerfile.verified'), '-t', 'agri-breeding-restore:20261013_115719',
                str(ROOT / 'package/工具及配置模板')])
    help_text = resume.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint',
                           'milvus-backup', 'agri-breeding-restore:20261013_115719', 'restore', '--help'], True)
    assert '--rebuild_index' in help_text and '--filter' in help_text
    resume.write_new(ROOT / 'audit/actual-restore-help.txt', help_text)
    resume.run(resume.COMPOSE + ['config', '--quiet'])
    resume.status('start-base-services')
    resume.run(resume.COMPOSE + ['up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '600', 'etcd', 'minio', 'standalone'])
    resume.run(resume.COMPOSE + ['up', '-d', '--pull', 'never', 'attu'])
    resume.status('start-detached-restore')
    resume.run(resume.COMPOSE + ['--profile', 'migration', 'up', '-d', '--no-build', '--pull', 'never', 'receiver'])
    resume.status('restore-running', progress_file=str(ROOT / 'audit/progress.json'))
    resume.run(resume.COMPOSE + ['ps', '-a'])


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        resume.status('failed', error=str(error), traceback=traceback.format_exc(), automatic_cleanup=False)
        raise
