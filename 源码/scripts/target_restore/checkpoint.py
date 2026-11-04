"""Record the stopped preparation phase and verify preserved original files."""
import hashlib
import json
import pathlib
import subprocess
import sys
import time

root = pathlib.Path(sys.argv[1]).resolve()
assert root.parent == pathlib.Path('/data/agri-breeding-pdf-memories')
preflight = json.loads((root / 'audit/preflight.json').read_text())
differences = []
for name, expected in preflight['preserved_files'].items():
    p = pathlib.Path(name)
    if not p.is_file() or p.stat().st_size != expected['bytes']:
        differences.append(name)
        continue
    h = hashlib.sha256()
    with p.open('rb') as stream:
        while data := stream.read(8 * 1024 * 1024):
            h.update(data)
    if h.hexdigest() != expected['sha256']:
        differences.append(name)
status = {
    'time_unix': time.time(), 'phase': 'prepared_not_restored', 'restored': False,
    'deployment': str(root), 'package_manifest_files_verified': 127,
    'backup_objects_verified': 101,
    'preserved_files_checked': len(preflight['preserved_files']),
    'preservation_differences': differences,
    'blockers': [
        'Docker Hub MinIO pull denied; Quay pull unauthorized; Docker Hub direct connection timed out',
        'Official archived MinIO binary checksum URL returns HTTP 410 Gone',
        'Python 3.11-slim pull failed through existing mirror: HTTP 502 Bad Gateway',
    ],
    'existing_custom_minio': {
        'image_id': 'sha256:df0166eed2823e1b4f88d9729ea6866c979fcb88be855f7f755a91401a7c6369',
        'reported_version': 'RELEASE.2024-12-18T13-15-44Z',
        'official_image_provenance_verified': False,
        'used_for_database': False,
    },
    'containers': subprocess.check_output(['docker', 'ps', '-a'], text=True),
    'automatic_cleanup': False,
}
with (root / 'audit/execution-status.json').open('x', encoding='utf-8') as stream:
    json.dump(status, stream, ensure_ascii=False, indent=2)
print(json.dumps(status, ensure_ascii=False, indent=2))
assert not differences, 'Preserved files changed'
