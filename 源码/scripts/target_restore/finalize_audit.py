"""After successful acceptance, verify preservation and record scoped operations."""
import hashlib
import json
import pathlib
import subprocess
import time

ROOT = pathlib.Path('/data/agri-breeding-pdf-memories/restore_20261013_115719')
AUDIT = ROOT / 'audit'


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while data := stream.read(8 * 1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def main():
    result = json.loads((AUDIT / 'restore-result.json').read_text())
    assert result['success'] and result['differences'] == 0 and result['records'] == 1413886
    assert result['original_8_fields_including_vectors_full_verified'] and result['new_20_fields_full_verified']
    originals = json.loads((AUDIT / 'preflight.json').read_text())['preserved_files']
    differences = []
    for name, expected in originals.items():
        path = pathlib.Path(name)
        if not path.is_file() or path.stat().st_size != expected['bytes'] or sha(path) != expected['sha256']:
            differences.append(name)
    names = ['agri-breeding-pdf-memories-' + n for n in ('etcd', 'minio', 'milvus', 'attu', 'restore')]
    containers = json.loads(subprocess.check_output(['docker', 'inspect'] + names, text=True))
    services = []
    for container in containers:
        state = container['State']
        service = container['Config']['Labels']['com.docker.compose.service']
        assert container['Config']['Labels']['com.docker.compose.project'] == 'agri-breeding-restore-20261013_115719'
        if service == 'receiver':
            assert state['Status'] == 'exited' and state['ExitCode'] == 0
        else:
            assert state['Running'] and container['HostConfig']['RestartPolicy']['Name'] == 'unless-stopped'
            if service != 'attu':
                assert state['Health']['Status'] == 'healthy'
        services.append({'name': container['Name'], 'service': service, 'status': state['Status'],
                         'health': state.get('Health', {}).get('Status'),
                         'restart_policy': container['HostConfig']['RestartPolicy']['Name'],
                         'image': container['Config']['Image']})
    report = {'accepted': not differences, 'updated_unix': time.time(), 'deployment': str(ROOT),
              'restoration': result, 'preserved_files_checked': len(originals),
              'preservation_differences': differences, 'services': services,
              'baseline': 'Verified 2026-10-10 complete backup; live source not compared',
              'public_ports_opened': False, 'unrelated_services_operated': False}
    with (AUDIT / 'final-acceptance.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
    assert not differences, 'Original target files changed'
    instructions = '''# 目标服务器农业育种向量库操作说明

本实例恢复自 2026-10-10 完整备份，并非与 231 实时同步。
数据库 agri_pdf_memories，集合 agri_breeding_pdf_chunk。
1,413,886 条切片，29,167 篇文章，28 个字段；原向量 2560 维。
验收文件：audit/final-acceptance.json、audit/restore-result.json。
原 8 字段（含原向量）和新增 20 字段逐条校验通过，差异 0。

## 启动和状态

```bash
cd /data/agri-breeding-pdf-memories/restore_20261013_115719
docker compose up -d --no-build --pull never etcd minio standalone attu
docker compose ps -a
docker compose logs --tail 100 standalone
```

服务设置了 restart: unless-stopped。停止本实例时仅使用该目录下的
`docker compose stop etcd minio standalone attu`，需要时按上述启动命令重新启动。
不要删除持久化目录，不要执行 down -v，不要重复运行 receiver 恢复任务。
receiver 正常退出 0 表示一次性恢复及全量验收结束，不需要常驻运行。

## 本机访问目标服务器

本次未开放公网端口或修改防火墙。保持 SSH 隧道连接：

```bash
ssh -p 65502 -L 18013:127.0.0.1:18003 -L 19544:127.0.0.1:19534 root@demo.example.invalid
```

浏览器打开 http://127.0.0.1:18013 查看 Attu。
Attu 内填写 standalone:19530，选择 agri_pdf_memories / agri_breeding_pdf_chunk。
你本机程序的 Milvus URI 为 http://127.0.0.1:19544。
MinIO 凭据仅在 config/storage.env 和本项目配置内，不是 SSH 密码。
该实例业务访问方式参照源配置未开启 Milvus 认证；外部访问由 SSH 隧道保护。

## 保留文件及报告

package/ 为完整校验通过的恢复包副本；原 /data 交付文件没有覆盖。
etcd/、minio/、milvus/ 为本实例独立持久化目录。
config/ 为部署专用配置，包含凭据，请勿公开分享。
audit/uploaded-object-sha256.json 为 101 个对象回读校验结果。
audit/verified-images.json 记录镜像摘要及 MinIO 官方二进制校验。
audit/actual-restore-help.txt 是实际工具恢复命令帮助。
完整恢复日志：docker compose logs --no-color receiver。
完整字段含义见 package/说明文档/数据库字段说明.md。
后续继续 PDF 入库应先适配 28 字段；不要直接使用旧 8 字段入库程序。
'''
    with (AUDIT / '目标服务器操作说明.md').open('x', encoding='utf-8') as stream:
        stream.write(instructions)
    print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
