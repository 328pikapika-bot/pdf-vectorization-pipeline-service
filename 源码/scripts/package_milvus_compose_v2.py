"""Repackage the verified migration data with the reviewed Compose guide."""

import hashlib
import json
import pathlib
import shutil
import time
import zipfile


BASE = pathlib.Path(r'E:\milvus_exports')
SOURCE_NAME = 'agri_breeding_pdf_chunk_28fields_20261010_224223_compose'
TARGET_NAME = SOURCE_NAME + '_v2'
SOURCE = BASE / SOURCE_NAME
TARGET = BASE / TARGET_NAME
ARCHIVE = BASE / (TARGET_NAME + '.zip')
KIT = pathlib.Path(__file__).resolve().parent / 'migration_compose'
ORIGINAL_ZIP = BASE / 'agri_breeding_pdf_chunk_28fields_20261010_224223.zip'
ORIGINAL_SHA = 'dde71376fafee068105150cbcc47011d590c199f2c68d9f22505ebea154c01db'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    started = time.monotonic()
    assert SOURCE.is_dir() and not TARGET.exists() and not ARCHIVE.exists()
    assert not ARCHIVE.with_suffix('.zip.sha256').exists()
    assert sha(ORIGINAL_ZIP) == ORIGINAL_SHA
    for line in (SOURCE / '校验资料/文件校验清单.sha256').read_text(encoding='utf-8').splitlines():
        digest, relative = line.split('  ', 1)
        assert sha(SOURCE / relative) == digest, relative
    print('Source package verified', flush=True)
    assert shutil.disk_usage(BASE).free > 2 * 7654877350 + 5 * 1024**3
    shutil.copytree(SOURCE, TARGET)
    for name in ['compose.yaml', 'Docker Compose 恢复说明.md']:
        shutil.copy2(KIT / name, TARGET / name)
    shutil.copy2(KIT / 'Dockerfile.restore', TARGET / '工具及配置模板/Dockerfile.restore')
    shutil.copy2(KIT / 'restore_auto.py', TARGET / '工具及配置模板/restore_auto.py')
    manual_source = BASE / 'agri_breeding_pdf_chunk_28fields_20261010_224223/说明文档/迁移恢复说明.md'
    text = manual_source.read_text(encoding='utf-8')
    text = text.replace(SOURCE_NAME.removesuffix('_compose') + '.zip.sha256', TARGET_NAME + '.zip.sha256')
    text = text.replace('进入解压出的 `agri_breeding_pdf_chunk_28fields_20261010_224223`',
                        '进入解压出的 `' + TARGET_NAME + '`')
    (TARGET / '说明文档/迁移恢复说明.md').write_text(
        '# 分阶段恢复参考\n\n默认请使用根目录的 [Docker Compose 恢复说明.md](../Docker%20Compose%20恢复说明.md)。'
        '以下是针对已有 Milvus 的人工恢复命令，和一键 Compose 新建环境的流程互为备选；'
        '请不要在已经运行 Compose 恢复的目标上再执行人工恢复。\n\n' + text,
        encoding='utf-8')
    delivery = TARGET / '说明文档/交付说明.md'
    delivery.write_text(
        (SOURCE / '说明文档/交付说明.md').read_text(encoding='utf-8')
        + '\n此版更新了根目录的 Docker Compose 说明和镜像版本清单。'
        '同事按该说明使用 Compose 新建环境并恢复，无需手工填写对象存储密钥。\n', encoding='utf-8')
    manifest = TARGET / '校验资料/文件校验清单.sha256'
    files = {p.relative_to(TARGET).as_posix(): (p.stat().st_size, sha(p))
             for p in sorted(TARGET.rglob('*')) if p.is_file() and p != manifest}
    manifest.write_text(''.join(digest + '  ' + name + '\n'
                                for name, (_, digest) in files.items()), encoding='utf-8')
    files[manifest.relative_to(TARGET).as_posix()] = (manifest.stat().st_size, sha(manifest))
    part = ARCHIVE.with_suffix('.zip.part')
    assert not part.exists()
    with zipfile.ZipFile(part, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as zip_out:
        for name, (size, expected) in files.items():
            h = hashlib.sha256()
            with (TARGET / name).open('rb') as source, zip_out.open(TARGET_NAME + '/' + name, 'w', force_zip64=True) as dest:
                while block := source.read(4 * 1024 * 1024):
                    h.update(block)
                    dest.write(block)
            assert h.hexdigest() == expected and (TARGET / name).stat().st_size == size
    with zipfile.ZipFile(part) as zip_in:
        assert len(zip_in.infolist()) == len(files)
        for name, (size, expected) in files.items():
            entry = zip_in.getinfo(TARGET_NAME + '/' + name)
            assert entry.file_size == size
            with zip_in.open(entry) as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected, name
    zip_digest = sha(part)
    part.replace(ARCHIVE)
    ARCHIVE.with_suffix('.zip.sha256').write_text(zip_digest + '  ' + ARCHIVE.name + '\n', encoding='ascii')
    receipt = {'completed': True, 'original_data_zip_sha256': ORIGINAL_SHA, 'zip': str(ARCHIVE),
               'zip_bytes': ARCHIVE.stat().st_size, 'zip_sha256': zip_digest,
               'files': len(files), 'file_sha256_verified': True,
               'restore_rehearsal_performed': False, 'elapsed_seconds': round(time.monotonic()-started, 1)}
    (BASE / (TARGET_NAME + '_交付完成.json')).write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
