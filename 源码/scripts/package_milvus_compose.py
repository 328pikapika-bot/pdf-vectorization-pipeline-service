"""Create a new self-contained Compose handoff from the verified 28-field package."""

import hashlib
import json
import pathlib
import shutil
import time
import zipfile


BASE = pathlib.Path(r'E:\milvus_exports')
ORIGINAL = 'agri_breeding_pdf_chunk_28fields_20261010_224223'
NAME = ORIGINAL + '_compose'
SOURCE = BASE / ORIGINAL
TARGET = BASE / NAME
ZIP = BASE / (NAME + '.zip')
KIT_SOURCE = pathlib.Path(__file__).resolve().parent / 'migration_compose'
RECEIVER = pathlib.Path(__file__).resolve().parent / 'milvus_migration_receiver.py'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_original():
    expected = 'dde71376fafee068105150cbcc47011d590c199f2c68d9f22505ebea154c01db'
    assert digest(BASE / (ORIGINAL + '.zip')) == expected, 'Original ZIP has changed'
    manifest = SOURCE / '校验资料' / '文件校验清单.sha256'
    for line in manifest.read_text(encoding='utf-8').splitlines():
        sha256, relative = line.split('  ', 1)
        assert digest(SOURCE / relative) == sha256, 'Original package file differs: ' + relative
    print('Verified original ZIP and files', flush=True)


def main():
    start = time.monotonic()
    assert SOURCE.is_dir() and not TARGET.exists() and not ZIP.exists(), 'Target already exists'
    assert not ZIP.with_suffix('.zip.sha256').exists(), 'Checksum already exists'
    verify_original()
    assert shutil.disk_usage(BASE).free > 2 * 7654864044 + 5 * 1024**3
    shutil.copytree(SOURCE, TARGET)
    for file in ['Dockerfile.restore', 'restore_auto.py']:
        shutil.copy2(KIT_SOURCE / file, TARGET / '工具及配置模板' / file)
    shutil.copy2(RECEIVER, TARGET / '工具及配置模板' / RECEIVER.name)
    for file in ['compose.yaml', 'Docker Compose 恢复说明.md']:
        shutil.copy2(KIT_SOURCE / file, TARGET / file)
    guide = TARGET / '说明文档' / '迁移恢复说明.md'
    guide.write_text(
        '# 迁移恢复说明\n\n'
        '本版交付包提供自动部署和恢复流程。请先阅读交付目录根部的 '
        '[Docker Compose 恢复说明.md](../Docker%20Compose%20恢复说明.md)。'
        '接收方只需 Docker Engine 与 Docker Compose v2；Compose 将启动目标 Milvus、'
        'etcd、MinIO、Attu 并自动恢复。以下为人工分阶段排障参考。\n\n'
        + (SOURCE / '说明文档' / '迁移恢复说明.md').read_text(encoding='utf-8'),
        encoding='utf-8')
    delivery = TARGET / '说明文档' / '交付说明.md'
    delivery.write_text(
        (SOURCE / '说明文档' / '交付说明.md').read_text(encoding='utf-8')
        + '\n本版另附根目录 `compose.yaml` 及 Docker Compose 恢复说明；'
        '接收方可自动启动 Milvus、etcd、MinIO、Attu，恢复数据后重建索引。'
        'Docker 镜像需在目标机器上下载构建。\n', encoding='utf-8')
    checksum = TARGET / '校验资料' / '文件校验清单.sha256'
    expected = {p.relative_to(TARGET).as_posix(): (p.stat().st_size, digest(p))
                for p in sorted(TARGET.rglob('*')) if p.is_file() and p != checksum}
    checksum.write_text(''.join(sha + '  ' + name + '\n'
                                for name, (_, sha) in expected.items()), encoding='utf-8')
    expected[checksum.relative_to(TARGET).as_posix()] = (checksum.stat().st_size, digest(checksum))
    part = ZIP.with_suffix('.zip.part')
    assert not part.exists()
    with zipfile.ZipFile(part, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for name, (size, sha) in expected.items():
            with (TARGET / name).open('rb') as source, archive.open(NAME + '/' + name, 'w', force_zip64=True) as out:
                h = hashlib.sha256()
                while block := source.read(4 * 1024 * 1024):
                    h.update(block)
                    out.write(block)
            assert h.hexdigest() == sha
    with zipfile.ZipFile(part, 'r') as archive:
        assert len(archive.infolist()) == len(expected)
        for name, (size, sha) in expected.items():
            item = archive.getinfo(NAME + '/' + name)
            assert item.file_size == size
            with archive.open(item, 'r') as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == sha
    archive_sha = digest(part)
    part.replace(ZIP)
    ZIP.with_suffix('.zip.sha256').write_text(archive_sha + '  ' + ZIP.name + '\n', encoding='ascii')
    receipt = {'completed': True, 'original_zip_sha256': 'dde71376fafee068105150cbcc47011d590c199f2c68d9f22505ebea154c01db',
               'package': str(TARGET), 'zip': str(ZIP), 'zip_bytes': ZIP.stat().st_size,
               'zip_sha256': archive_sha, 'files': len(expected),
               'archive_members_sha256_verified': True, 'restore_rehearsal_performed': False,
               'elapsed_seconds': round(time.monotonic() - start, 1)}
    (BASE / (NAME + '_交付完成.json')).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
