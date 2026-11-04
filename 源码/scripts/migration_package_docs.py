"""Generate the migration handoff documents from checked live schema and audits."""
import gzip
import json
import pathlib
import shutil

from pymilvus import MilvusClient

MEANINGS = {
    'id':'切片主键，不自动生成；通常为文章标识加切片序号',
    'pdf_index':'PDF 文件标识，当前记录通常包含末尾 .pdf',
    'chunk_index':'文章内部的切片序号，从 0 开始',
    'text_embed':'文本切片的原始 2560 维 float32 向量',
    'text':'切片正文文本，不是整篇文章全文',
    'char_start':'切片在清理后基准 Markdown 文本中的起始字符偏移，含起点',
    'char_end':'切片在清理后基准 Markdown 文本中的结束字符偏移，不含终点',
    'section_path':'切片所属章节路径；无法识别时为 body',
    'gid':'SQL 文章唯一标识，也是去掉末尾 .pdf 后的 pdf_index',
    'title':'文章标题', 'year':'发表年份，保留 SQL 字符串', 'volume':'期刊卷号', 'issue':'期刊期号',
    'doi':'文章 DOI', 'journal_name':'期刊名称', 'issn':'期刊 ISSN', 'author':'作者原始字符串',
    'author_add':'作者机构／地址原始字符串', 'keyword':'关键词原始字符串', 'abstracts':'文章摘要',
    'indexed_by':'收录状态／收录数据库原始字符串', 'url':'SQL 提供的链接地址，不保证是全文或服务窗下载链接',
    'language':'语言原始标识', 'published_date':'发表日期原始字符串', 'start_page':'起始页码或文章编号',
    'end_page':'结束页码或文章编号', 'publisher':'出版者',
    'hasFullText':'SQL 提供的全文状态字符串，未转为布尔值；业务取值含义由源数据定义',
}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def dump(path, data):
    write(path, json.dumps(data, ensure_ascii=False, indent=2, default=str))


def example(value):
    if isinstance(value, list):
        value = repr(value[:5]) + ' …（前 5 维，共 2560 维）'
    elif value is None:
        value = 'NULL'
    else:
        value = str(value)
    if len(value) > 150:
        value = value[:150] + ' …（节选）'
    return value.replace('|', '\\|').replace('\r', ' ').replace('\n', ' ').replace('`', "'")


def create_docs(root, work, prior, tools, name):
    docs, audit, kit = root / '说明文档', root / '校验资料', root / '工具及配置模板'
    for directory in [docs, audit, kit]:
        directory.mkdir(parents=True, exist_ok=True)
    source = read(work / 'source-before.json')
    result = read(work / 'backup-validation.json')
    enrich = read(prior / 'enrichment/final-result.json')
    c = MilvusClient(uri='http://127.0.0.1:19534', db_name='agri_pdf_memories')
    try:
        smoke = read(prior / 'enrichment/search-smoke-test.json')
        sample_id = smoke['query_id']
        sample = c.get('agri_breeding_pdf_chunk', [sample_id], output_fields=['*'], consistency_level='Strong')[0]
        schema = c.describe_collection('agri_breeding_pdf_chunk')
        assert schema == source['schema']
    finally:
        c.close()
    assert set(MEANINGS) == {f['name'] for f in schema['fields']}
    articles = []
    with gzip.open(prior / 'enrichment/matched-articles.jsonl.gz', 'rt', encoding='utf-8') as stream:
        articles = [json.loads(line) for line in stream]
    assert len(articles) == 29167
    for field in schema['fields'][8:]:
        key = field['name']
        if sample.get(key) is None:
            sample[key] = next((r[key] for r in articles if r[key] is not None), None)
    rows = []
    for i, f in enumerate(schema['fields']):
        dtype = {5:'INT64', 21:'VARCHAR', 101:'FLOAT_VECTOR'}[f['type']]
        if dtype == 'VARCHAR':
            dtype += '(' + str(f['params']['max_length']) + ')'
        elif dtype == 'FLOAT_VECTOR':
            dtype += '(dim=' + str(f['params']['dim']) + ')'
        origin = '原切片处理流程' if i < 8 else 'SQL.' + f['name']
        rows.append('| ' + ' | '.join([f['name'], MEANINGS[f['name']], dtype,
                                      '允许 NULL' if f.get('nullable', False) else '不允许 NULL',
                                      origin, example(sample.get(f['name']))]) + ' |')
    missing = []
    for f in schema['fields'][8:]:
        k = f['name']
        null = sum(r[k] is None for r in articles)
        empty = sum(r[k] == '' for r in articles)
        missing.append(f'| {k} | {len(articles)-null-empty:,} | {null:,} | {empty:,} |')
    field_text = (
        '# 数据库字段说明\n\n'
        '数据库：`agri_pdf_memories`；集合：`agri_breeding_pdf_chunk`。共 28 个字段、1,413,886 条切片，来自 29,167 篇 PDF。\n\n'
        '前 8 个字段是原字段，后 20 个字段为本次从 SQL 增补的文章元数据。类型、长度和 NULL 属性来自当前实际 schema。'
        'VARCHAR 的 max_length 单位为 UTF-8 字节，不是汉字个数。示例为真实记录中的值，长内容仅在文档中节选，备份数据没有截断；'
        '缺失的示例从其他匹配文章选取，因此表内示例不一定全部来自同一篇文章。\n\n'
        '| 字段 | 含义 | 实际类型 | NULL 属性 | 来源 | 真实示例／节选 |\n'
        '|---|---|---|---|---|---|\n' + '\n'.join(rows) + '\n\n'
        '## 主键与文章关联\n\n'
        '`id` 是每个切片的主键，`pdf_index` 是 PDF 标识，`gid` 是文章标识。仅删除 pdf_index 最末尾的 `.pdf`，得到 SQL gid；'
        '不能拿完整切片 id 直接匹配 gid，也不能替换字符串中间的 .pdf。同一篇文章的所有切片共享新增的文章元数据。\n\n'
        f'真实示例：id=`{sample_id}`，pdf_index=`{sample["pdf_index"]}`，gid=`{sample["gid"]}`。\n\n'
        '字符位置使用 `[char_start, char_end)`，定位对象是清理后的基准 Markdown，不是 PDF 页码或字节位置。'
        '切片可能重叠，因此不能简单拼接 text 来重建全文。\n\n'
        '## 空值与字符串规则\n\n'
        'SQL NULL 保存为数据库 NULL；空字符串保持空字符串；字面字符串 "NULL" 保持字符串。年份、卷期、页码、日期、hasFullText 都是 VARCHAR，'
        '未转换成数字、日期或布尔类型。原字符串中的 ;; 等分隔符保留；不假定作者列表与机构列表逐项对应。\n\n'
        '以下按文章统计 SQL 源值，不按 141 万条切片统计。“非空值”排除 NULL 和空字符串，但不排除空白或字面字符串 NULL。\n\n'
        '| 新增字段 | 非空值文章数 | NULL 文章数 | 空字符串文章数 |\n|---|---:|---:|---:|\n' + '\n'.join(missing) + '\n\n'
        '## 向量检索\n\n'
        'text_embed 为 qwen3-embed 的 2560 维原向量；索引 HNSW，距离 COSINE，M=32，efConstruction=800。'
        '查询侧需要相同或经验证兼容的模型版本、维度及文本处理配置，不能只用同维度的任意模型。迁移无需调用向量服务。'
        '向量模型服务和 PDF 原文件不包含在此包内，模型访问方式由接收方自行配置。\n')
    write(docs / '数据库字段说明.md', field_text)
    write(docs / '交付说明.md', f'''# 农业育种向量库迁移交付说明

这是字段增补完成后的完整数据备份，包含原 8 字段及新增 20 字段，共 28 个字段。

- 源数据库／集合：agri_pdf_memories.agri_breeding_pdf_chunk。
- 逻辑记录数：1,413,886；对应文章数：29,167。
- Milvus：2.6.9；备份工具：milvus-backup 0.6.0；验证 SDK：PyMilvus 2.6.14。
- 新备份名称：{name}。名称中的北京时间是任务创建时间；实际备份状态和时间见校验资料中的备份元数据及检查记录。
- 备份对象：{result['object_count']} 个；数据容量：{result['backup_bytes']:,} 字节（约 {result['backup_bytes']/1024**3:.2f} GiB）。
- 本包只含该集合的原向量、正文、元数据及恢复资料，不含 PDF、全量 SQL、运行环境和账号密码。

备份数据目录保留官方备份层级，不能只拿其中的 JSON 文件或只拿 binlogs。恢复需要所有文件，尤其是删除日志；不能将物理段行数简单求和作为逻辑记录数。

说明文档解释字段和迁移；工具及配置模板提供 Linux x86_64 官方备份工具包、配置和辅助脚本；校验资料包含 schema、索引、对象清单、数据值校验基线及原字段增补报告。

完整文件夹及 ZIP64 包都保留。ZIP 采用存储模式，以减少打包耗时，不承诺压缩体积。ZIP 外部 .sha256 文件用于检查整个压缩包，包内文件清单用于检查解压内容。

本次检查了源库未变化、官方备份成功、文件引用及传输完整性；没有在另一台服务器执行恢复演练，不将文件检查表述为实际恢复验证。

原来的增补前旧备份仍在服务器保留。本迁移包是新的 28 字段备份，不能与旧 8 字段备份混用。

继续导入新 PDF 前，原项目入库程序需要适配 28 字段；旧程序按 8 字段验证结构，不应未经调整直接运行。检索时需明确 output_fields 才能返回所需新增字段。
''')
    guide = f'''# 迁移恢复说明

默认接收环境：Linux x86_64、Milvus 2.6.9、Milvus 使用 MinIO/S3 兼容对象存储。其他平台、TLS 或版本须先确认兼容性，此模板不直接宣称支持。无需重新计算向量。

## 1. 检查、解压

在压缩包所在目录运行 `sha256sum -c {name}.zip.sha256`（Windows 可用 Get-FileHash 检查），通过后解压 ZIP64。需要支持大文件的解压工具。

进入解压出的 `{name}` 目录，后续命令均在这个目录执行：

```bash
python3 -m pip install pymilvus==2.6.14 minio PyYAML
python3 '工具及配置模板/milvus_migration_receiver.py' verify-files
mkdir -p ./milvus-backup-tool
tar -xzf '工具及配置模板/milvus-backup_0.6.0_Linux_x86_64.tar.gz' -C ./milvus-backup-tool
./milvus-backup-tool/milvus-backup restore --help
cp '工具及配置模板/restore-target.example.yaml' ./restore-target.yaml
```

工具包 SHA256 和官方 checksum 文件均在包内。全包校验要在编辑模板、增加本地配置前完成；新增文件不在原文件清单内。

## 2. 填写目标服务器配置

编辑 restore-target.yaml，填写目标 Milvus、目标对象存储地址、端口、凭据、bucketName 和 rootPath。milvus.storage 必须与目标 Milvus 的实际对象存储完全一致，常见 rootPath 为 files，但必须确认。backup.storage 是用于上传本备份的目录，默认 rootPath 为 migration_backup。

先创建配置指定的备份 bucket。脚本不会修改集群的存储配置，也不会创建整套运行环境。目标地址不能指向源服务器 127.0.0.1。

```bash
python3 '工具及配置模板/milvus_migration_receiver.py' check-target --config ./restore-target.yaml
```

默认恢复数据库名 agri_pdf_memories，集合名 agri_breeding_pdf_chunk。目标同名集合必须不存在，存在时脚本停止，不删除或覆盖。

## 3. 上传完整备份

```bash
python3 '工具及配置模板/milvus_migration_receiver.py' upload --config ./restore-target.yaml
```

对象存储最终路径：`<backup.bucketName>/<backup.rootPath>/{name}/meta/` 与对应 `binlogs/`。脚本保持整个相对路径。

备份上传前检查包内 SHA256、目标集合不存在、目标备份前缀为空。上传中断时保留已上传文件；再次运行遇到同名前缀会停止，不能绕过检查直接覆盖。接收方应在目标 backup.storage.rootPath 选择一个新的空目录重试。

## 4. 恢复、重建索引并加载

```bash
python3 '工具及配置模板/milvus_migration_receiver.py' restore --config ./restore-target.yaml --binary ./milvus-backup-tool/milvus-backup
```

脚本核查版本、目标集合不存在、备份对象数量及大小，然后调用已核实的官方命令：

```bash
./milvus-backup-tool/milvus-backup --config ./restore-target.yaml restore -n {name} --filter agri_pdf_memories.agri_breeding_pdf_chunk --rebuild_index
```

推荐使用辅助脚本，避免绕过目标保护。命令不启用 drop、skip_create 或 meta_only。官方备份保存原向量及索引定义，未复制实体索引文件，--rebuild_index 在目标重建 HNSW/COSINE 索引。恢复完成后脚本加载集合。

恢复和索引构建耗时取决于目标 CPU、内存、磁盘及对象存储速度，不应使用源服务器备份耗时估计。目标需容纳 2560 维向量、文本、元数据及索引，备份大小不等于运行时内存需求。

## 5. 检查结果

```bash
python3 '工具及配置模板/milvus_migration_receiver.py' verify-restored --config ./restore-target.yaml
# 如需完整逐行核验原8字段及新增20字段，可另行执行：
python3 '工具及配置模板/milvus_migration_receiver.py' verify-restored --config ./restore-target.yaml --full
```

默认检查 28 字段的类型、长度、NULL 属性和主键属性、强一致 count(*)=1,413,886、索引完成、示例元数据及向量查询。--full 进一步对所有原字段 SHA256 及新增字段与文章缓存逐条比较，可能需要几十分钟。

Attu 连接目标 Milvus 后，选择 agri_pdf_memories → agri_breeding_pdf_chunk，即可查看 28 字段及记录；源服务器的 Attu 链接不代表目标已恢复成功。

## 异常处理

校验失败、目标同名集合存在、配置未填、源地址被填为目标、对象缺失或版本不符时停止。恢复中断可能在目标留下未完成集合；不要盲目重复恢复，不要清空源集合，交由目标管理员检查。本包没有自动回滚、删除或覆盖命令。
'''
    write(docs / '迁移恢复说明.md', guide)
    template = '''configVersion: v2
log:
  level: info
  console: true
  file:
    path: logs/restore.log
milvus:
  user: ""
  password: ""
  grpc:
    address: TARGET_MILVUS_HOST
    port: 19530
    tlsMode: disabled
  rest:
    endpoint: http://TARGET_MILVUS_HOST:19530
  management:
    endpoint: http://TARGET_MILVUS_MANAGEMENT_HOST:9091
  storage:
    provider: minio
    address: TARGET_MINIO_HOST
    port: 9000
    useSSL: false
    bucketName: TARGET_MILVUS_BUCKET
    rootPath: files
    auth:
      type: static
      accessKeyID: TARGET_ACCESS_KEY
      secretAccessKey: TARGET_SECRET_KEY
backup:
  storage:
    provider: minio
    address: TARGET_MINIO_HOST
    port: 9000
    useSSL: false
    bucketName: TARGET_BACKUP_BUCKET
    rootPath: migration_backup
    auth:
      type: static
      accessKeyID: TARGET_ACCESS_KEY
      secretAccessKey: TARGET_SECRET_KEY
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
'''
    write(kit / 'restore-target.example.yaml', template)
    project = pathlib.Path(__file__).resolve().parents[1]
    shutil.copy2(project / 'scripts/milvus_migration_receiver.py', kit / 'milvus_migration_receiver.py')
    for filename in ['milvus-backup_0.6.0_Linux_x86_64.tar.gz', 'milvus-backup_0.6.0_checksums.txt', 'LICENSE', 'README.md']:
        shutil.copy2(tools / filename, kit / filename)
    dump(audit / '交付标识.json', {'backup_name':name, 'database':'agri_pdf_memories',
                                'collection':'agri_breeding_pdf_chunk', 'rows':1413886, 'fields':28,
                                'articles':29167, 'sample_id':sample_id, 'milvus_version':'2.6.9'})
    dump(audit / 'schema.json', schema)
    dump(audit / 'indexes.json', source['indexes'])
    dump(audit / '备份对象清单.json', read(work / 'remote-objects.json'))
    dump(audit / '下载SHA256记录.json', read(work / 'download-files.json'))
    dump(audit / '备份检查结果.json', result)
    dump(audit / 'source-before.json', source)
    dump(audit / 'source-after.json', read(work / 'source-final.json'))
    for filename in ['增补结果.md', 'final-result.json', 'prepare.json', 'matched-articles.jsonl.gz']:
        shutil.copy2(prior / 'enrichment' / filename, audit / filename)
    shutil.copy2(prior / 'original-eight-fields-sha256.sqlite', audit / 'original-eight-fields-sha256.sqlite')
    # Do not copy raw backup configs or logs: they may contain source credentials.
    assert not any(p.name == 'private-backup.yaml' for p in root.rglob('*'))
