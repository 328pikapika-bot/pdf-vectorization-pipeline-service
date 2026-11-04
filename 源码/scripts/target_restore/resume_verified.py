"""Resume only the isolated, approved deployment with digest-pinned mirror images."""
import hashlib
import json
import pathlib
import shutil
import socket
import subprocess
import time
import traceback

ROOT = pathlib.Path('/data/agri-breeding-pdf-memories/restore_20261013_115719')
AUDIT = ROOT / 'audit'
MINIO = 'docker.1panel.live/minio/minio@sha256:34c8e2f52a5984492555427fee07254c80036bdb7079bb91679232abd7a4fa20'
PYTHON = 'docker.1panel.live/library/python@sha256:e529028263dbe6910a2d96f7d2b8f5266385e917fd45d286ef166977c094a51e'
MINIO_TAG = 'minio/minio:RELEASE.2024-12-18T13-15-44Z'
MINIO_BINARY_SHA = '88182336ab5793488f2caad54846056b330f0dcb5c488b60d52434beeebae3ca'
COMPOSE = ['docker', 'compose', '-f', str(ROOT / 'compose.yaml')]


def status(stage, **details):
    value = {'stage': stage, 'updated_unix': time.time(), **details}
    temp = AUDIT / 'resume-status.tmp'
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temp.replace(AUDIT / 'resume-status.json')
    print(json.dumps(value), flush=True)


def run(args, capture=False):
    print('RUN:', ' '.join(args), flush=True)
    if capture:
        return subprocess.check_output(args, text=True)
    subprocess.run(args, check=True)


def write_new(path, content):
    with path.open('x', encoding='utf-8') as stream:
        stream.write(content)


def main():
    status('recheck-safety')
    names = set(run(['docker', 'ps', '-a', '--format', '{{.Names}}'], True).splitlines())
    planned = {'agri-breeding-pdf-memories-' + n for n in ('etcd', 'minio', 'milvus', 'attu', 'restore')}
    assert not names & planned, 'Planned containers already exist; inspect before retry'
    for port in (19534, 18003, 19006, 19007, 9095):
        with socket.socket() as sock:
            sock.bind(('0.0.0.0', port))
    for directory in ('etcd', 'minio', 'milvus'):
        assert not list((ROOT / directory).iterdir()), 'Data directory no longer empty'
    assert shutil.disk_usage(ROOT).free > 100 * 1024**3
    run(COMPOSE + ['config', '--quiet'])
    status('pull-minio-pinned')
    run(['docker', 'pull', '--platform', 'linux/amd64', MINIO])
    details = json.loads(run(['docker', 'image', 'inspect', MINIO], True))[0]
    assert details['Architecture'] == 'amd64'
    assert MINIO in details['RepoDigests']
    output = run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'sh', MINIO,
                  '-c', 'sha256sum /usr/bin/minio && /usr/bin/minio --version'], True)
    print(output, flush=True)
    assert output.split()[0] == MINIO_BINARY_SHA, 'MinIO binary differs from official GitHub release SHA256'
    assert 'RELEASE.2024-12-18T13-15-44Z' in output
    try:
        previous = json.loads(subprocess.check_output(['docker', 'image', 'inspect', MINIO_TAG], text=True, stderr=subprocess.DEVNULL))[0]
    except subprocess.CalledProcessError:
        previous = None
    assert previous is None or previous['Id'] == details['Id'], 'Refusing to replace another image tag'
    run(['docker', 'tag', MINIO, MINIO_TAG])
    status('pull-python-pinned')
    run(['docker', 'pull', '--platform', 'linux/amd64', PYTHON])
    python_details = json.loads(run(['docker', 'image', 'inspect', PYTHON], True))[0]
    assert PYTHON in python_details['RepoDigests'] and python_details['Architecture'] == 'amd64'
    provenance = {'mirror': 'third-party docker.1panel.live', 'minio': MINIO, 'python': PYTHON,
                  'minio_image_id': details['Id'], 'python_image_id': python_details['Id'],
                  'minio_official_binary_sha256': MINIO_BINARY_SHA, 'minio_binary_sha256_verified': True,
                  'binary_version_output': output, 'time_unix': time.time()}
    write_new(AUDIT / 'verified-images.json', json.dumps(provenance, indent=2))
    original = (ROOT / 'package/工具及配置模板/Dockerfile.restore').read_text(encoding='utf-8')
    assert original.startswith('FROM python:3.11-slim\n')
    dockerfile = ROOT / 'config/Dockerfile.verified'
    write_new(dockerfile, original.replace('FROM python:3.11-slim', 'FROM ' + PYTHON, 1))
    status('build-restore-tool')
    run(['docker', 'build', '--progress=plain', '-f', str(dockerfile), '-t',
         'agri-breeding-restore:20261013_115719', str(ROOT / 'package/工具及配置模板')])
    help_text = run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint',
                     'milvus-backup', 'agri-breeding-restore:20261013_115719', 'restore', '--help'], True)
    assert '--rebuild_index' in help_text and '--filter' in help_text
    write_new(AUDIT / 'actual-restore-help.txt', help_text)
    status('start-base-services')
    run(COMPOSE + ['up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '600', 'etcd', 'minio', 'standalone'])
    run(COMPOSE + ['up', '-d', '--pull', 'never', 'attu'])
    status('start-detached-restore')
    run(COMPOSE + ['--profile', 'migration', 'up', '-d', '--no-build', '--pull', 'never', 'receiver'])
    status('restore-running', progress_file=str(AUDIT / 'progress.json'))
    run(COMPOSE + ['ps', '-a'])


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        status('failed', error=str(error), traceback=traceback.format_exc(), automatic_cleanup=False)
        raise
