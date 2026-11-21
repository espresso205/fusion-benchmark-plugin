"""Build a minimal, reversible update against an exact installed plugin baseline."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

FILES=(
    'README.md','AGENTS.md','docs/BUILD.md','docs/ARCHITECTURE.md','scripts/assembly_update/安装与恢复.md',
    'evaluate.py','evaluate_v5.py','worker_v5.py','weights_v5.json',
    'src/assembly_v5.py','src/contract_v5.py','src/mass_v5.py','src/stages_v5.py',
    'fusion_pipeline/runner.py','fusion_pipeline/v5_validation.py','scripts/build_portable_plugin.py',
    'fusion_pipeline/service.py','fusion_pipeline/capability_study.py','fusion_pipeline/reporting.py',
    'docs/SCORING_V5.md','docs/RESULT_SCHEMA_V5.json','docs/V5_V4_COMPARISON.md','docs/RELEASE_V5.md',
    'scripts/assembly_update/apply_update.py','scripts/assembly_update/build_update.py',
    'SCORING_SOURCE_MANIFEST.json','SCORING_SOURCE_MANIFEST_V4.json','VERSION')

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def build(baseline,source,output):
    baseline=Path(baseline).resolve();source=Path(source).resolve();output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False);payload=output/'payload';payload.mkdir()
    rows={}
    for name in FILES:
        path=source/name
        if not path.is_file():raise ValueError('missing release file: '+name)
        previous=baseline/name;before=sha(previous) if previous.is_file() else None
        if before==sha(path):continue
        dest=payload/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,dest)
        rows[name]={'before_sha256':before,'sha256':sha(dest),'bytes':dest.stat().st_size}
    # Preserve the original package inventory and runtime/license identities.
    manifest=json.loads((baseline/'PACKAGE_MANIFEST.json').read_text(encoding='utf-8'))
    manifest['created_at_utc']=datetime.now(timezone.utc).isoformat()
    manifest['assembly_update']={'protocol':'fusion-step-adapted-v5.0','previous_package_manifest_sha256':sha(baseline/'PACKAGE_MANIFEST.json')}
    for name,row in rows.items():manifest['files'][name]={'sha256':row['sha256'],'bytes':row['bytes']}
    mp=payload/'PACKAGE_MANIFEST.json';mp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    sums={n:r['sha256'] for n,r in manifest['files'].items()};sums['PACKAGE_MANIFEST.json']=sha(mp)
    (payload/'SHA256SUMS').write_text(''.join(f'{v}  {n}\n' for n,v in sorted(sums.items())),encoding='utf-8')
    for name in ('PACKAGE_MANIFEST.json','SHA256SUMS'):
        path=payload/name;rows[name]={'before_sha256':sha(baseline/name),'sha256':sha(path),'bytes':path.stat().st_size}
    (output/'update-manifest.json').write_text(json.dumps({'schema':'fusion-assembly-update/1',
        'protocol':'fusion-step-adapted-v5.0','baseline_directory_name':baseline.name,'files':rows},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    shutil.copyfile(Path(__file__).with_name('apply_update.py'),output/'apply_update.py')
    readme=Path(__file__).with_name('安装与恢复.md')
    if readme.exists():shutil.copyfile(readme,output/readme.name)
    installer='''param([Parameter(Mandatory=$true)][string]$PluginDirectory, [string]$RollbackBackup)
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PluginDirectory 'runtime\\python\\python.exe'
$updatePath = Join-Path $PSScriptRoot 'apply_update.py'
if ($RollbackBackup) { & $pythonPath -X utf8 -B $updatePath --target $PluginDirectory --rollback $RollbackBackup }
else { & $pythonPath -X utf8 -B $updatePath --target $PluginDirectory }
exit $LASTEXITCODE
'''
    (output/'install.ps1').write_text(installer,encoding='utf-8-sig')
    with zipfile.ZipFile(output.with_suffix('.zip'),'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob('*')):
            if path.is_file():archive.write(path,path.relative_to(output).as_posix())
    with zipfile.ZipFile(output.with_suffix('.zip')) as archive:
        for name,row in rows.items():
            if hashlib.sha256(archive.read('payload/'+name)).hexdigest()!=row['sha256']:raise ValueError('ZIP verification failed')
    output.with_suffix('.zip.sha256').write_text(sha(output.with_suffix('.zip'))+'  '+output.name+'.zip\n',encoding='utf-8')
    return {'package':str(output.with_suffix('.zip')),'file_count':len(rows),'sha256':sha(output.with_suffix('.zip'))}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('baseline','source','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();print(json.dumps(build(args.baseline,args.source,args.output),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
