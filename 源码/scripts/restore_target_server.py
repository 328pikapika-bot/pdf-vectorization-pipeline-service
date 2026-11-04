"""Scoped SSH operations for the approved Milvus migration (no stored passwords)."""
import argparse
import getpass
import hashlib
import pathlib
import shlex
import sys
import time
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import paramiko


def connect():
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.load_host_keys(str(pathlib.Path.home() / '.ssh' / 'known_hosts'))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect('demo.example.invalid', port=65502, username='root',
                   password=getpass.getpass('Target SSH password: '),
                   look_for_keys=False, allow_agent=False, timeout=20,
                   auth_timeout=20, banner_timeout=20)
    client.get_transport().set_keepalive(20)
    return client


def run(client, command):
    channel = client.get_transport().open_session()
    channel.exec_command(command)
    while True:
        for ready, reader, stream in [(channel.recv_ready, channel.recv, sys.stdout),
                                      (channel.recv_stderr_ready, channel.recv_stderr, sys.stderr)]:
            while ready():
                stream.write(reader(65536).decode('utf-8', errors='replace'))
                stream.flush()
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            return channel.recv_exit_status()
        time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', nargs='?')
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--upload', type=pathlib.Path)
    parser.add_argument('--remote')
    args = parser.parse_args()
    client = connect()
    try:
        if args.upload:
            remote = pathlib.PurePosixPath(args.remote)
            assert str(remote).startswith('/data/agri-breeding-pdf-memories/restore_')
            assert '..' not in remote.parts
            with client.open_sftp() as sftp:
                with sftp.open(str(remote), 'wx') as out:
                    out.write(args.upload.read_bytes())
                sftp.chmod(str(remote), 0o600)
            if not args.command:
                return
        if args.prepare:
            archive = pathlib.Path(r'E:\milvus_exports\agri_breeding_pdf_chunk_28fields_20261010_224223_compose_v2.zip')
            print('Verifying original delivery ZIP SHA256...', flush=True)
            with archive.open('rb') as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == 'd3e16432833c0d327741f94d6bad63f462b09457041e9673af994ee3835c2165'
            stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S')
            root = '/data/agri-breeding-pdf-memories/restore_' + stamp
            sftp = client.open_sftp()
            try:
                try:
                    sftp.lstat(root)
                except FileNotFoundError:
                    pass
                else:
                    raise RuntimeError('Deployment path exists; refusing overwrite: ' + root)
                sftp.mkdir(root, mode=0o700)
                for filename in ['prepare_remote.py', 'restore_job.py']:
                    with sftp.open(root + '/' + filename, 'wx') as out:
                        out.write((pathlib.Path(__file__).parent / 'target_restore' / filename).read_bytes())
                with zipfile.ZipFile(archive) as z:
                    for source, name in [('compose.yaml', 'original-compose.yaml'),
                                         ('校验资料/文件校验清单.sha256', 'original-manifest.sha256')]:
                        with sftp.open(root + '/' + name, 'wx') as out:
                            out.write(z.read(archive.stem + '/' + source))
            finally:
                sftp.close()
            print('DEPLOYMENT_ROOT=' + root, flush=True)
            raise SystemExit(run(client, 'python3 ' + shlex.quote(root + '/prepare_remote.py') + ' ' + shlex.quote(root)))
        assert args.command
        raise SystemExit(run(client, args.command))
    finally:
        client.close()


if __name__ == '__main__':
    main()
