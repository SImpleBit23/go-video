#!/usr/bin/env python3
"""Build the isolated Go Video USB source. No car access, signing keys or BMW SDK redistribution.

python tools/build_go_video.py [--sdk-dir /your/oap-sdk] [--output /kit/folder]
Without SDK, an SDK-free prebuilt OAP package must be present in prebuilt/.
Every downloaded binary is checked against dependency-lock.json.
"""
from pathlib import Path
import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def sha(file):
    h = hashlib.sha256()
    with Path(file).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def normalize_app(source, target):
    with tarfile.open(source, 'r:gz') as src, tarfile.open(target, 'w:gz', format=tarfile.PAX_FORMAT) as out:
        for member in src.getmembers():
            if 'node_modules/oap-sdk' in member.name: continue
            raw = src.extractfile(member).read() if member.isfile() else None
            if raw and member.name.endswith(('package.json','kit/app-spec.json')):
                value = json.loads(raw); value['version'] = '0.1.5'
                raw = (json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode(); member.size=len(raw)
            member.uid=member.gid=0; member.uname=member.gname=''
            out.addfile(member, io.BytesIO(raw) if raw is not None else None)


def build(output, sdk=None):
    lock=json.loads((ROOT/'tools/dependency-lock.json').read_text(encoding='utf8'))
    cache=ROOT/'vendor-artifacts'; cache.mkdir(exist_ok=True)
    for item in lock['downloads']:
        dest=cache/item['name']
        if not dest.is_file() or dest.stat().st_size != item['size'] or sha(dest) != item['sha256']:
            part=dest.with_name(dest.name+'.part')
            print('Downloading '+item['name'], flush=True)
            with urllib.request.urlopen(item['url'],timeout=45) as src, part.open('wb') as out: shutil.copyfileobj(src,out)
            if part.stat().st_size != item['size'] or sha(part) != item['sha256']: raise ValueError('Dependency checksum mismatch: '+item['name'])
            part.replace(dest)
    service=ROOT/'native/st-govideo-d'; builddir=service/'build'; builddir.mkdir(exist_ok=True)
    with tarfile.open(cache/'jap-ridetube-d@0.5.0.tgz') as src:
        (builddir/'ytdlp.pyz').write_bytes(src.extractfile('files/ytdlp.pyz').read())
        decoder=src.extractfile('files/ytdec').read()
    if not decoder.startswith(b'\x7fELF') or b'mgu-video-ridetube' not in decoder: raise ValueError('Unexpected upstream decoder')
    (builddir/'ytdec').write_bytes(decoder.replace(b'mgu-video-ridetube',b'mgu-video-govideox'))
    with tarfile.open(cache/'ffmpeg-static.tar.xz') as src:
        for name,dest in [('ffmpeg','ffmpeg'),('GPLv3.txt','FFMPEG_LICENSE.txt'),('readme.txt','FFMPEG_BUILD.txt')]:
            entry=next(m for m in src.getmembers() if m.name.endswith('/'+name))
            (builddir/dest).write_bytes(src.extractfile(entry).read())
    shutil.copy2(cache/'ffmpeg-source.tar.xz',builddir/'ffmpeg-source.tar.xz')
    dist=ROOT/'dist'; dist.mkdir(exist_ok=True)
    if sdk:
        sdk=Path(sdk).resolve()
        if not (sdk/'index.js').is_file(): raise ValueError('Expected your local extracted oap-sdk/index.js')
        target=ROOT/'oap-sdk'
        if sdk != target.resolve(): shutil.copytree(sdk,target,dirs_exist_ok=True)
        subprocess.run([sys.executable,str(ROOT/'kit/tools/build.py'),'--language','ru','st_go_video'],check=True)
        normalize_app(dist/'st_go_video@0.1.1-ru.1.tgz',dist/'st_go_video@0.1.5.tgz')
    else:
        source=ROOT/'prebuilt/st_go_video@0.1.1.tgz'
        if not source.is_file(): raise ValueError('Provide --sdk-dir, or the SDK-free prebuilt app package')
        normalize_app(source, dist/'st_go_video@0.1.5.tgz')
    packer=ROOT/'tools/vendor-store/deploy/hu'
    for folder in (service,ROOT/'vendor-inputgate'):
        subprocess.run(['node',str(packer),'artifact',str(folder),'--out',str(dist)],check=True)
    folder=output/'mgu-repo/go-video'; folder.mkdir(parents=True,exist_ok=True)
    rows=[]
    def add(file,meta):
        file=Path(file); target=folder/file.name
        shutil.copy2(file,target)
        fields={'id','kind','title','description','version','requires','uninstallable','mguHmi'}
        row={k:v for k,v in meta.items() if k in fields}
        row['artifact']=dict(name=file.name,size=target.stat().st_size,sha256=sha(target),url=file.name)
        rows.append(row)
    add(dist/'st_go_video@0.1.5.tgz',dict(id='st_go_video',kind='app',title='Go Video · тестовая версия',description='VK-клиент на основе RideTube. Гостевой веб-каталог и поиск без входа. Тестовый вход через VK ID после настройки приложения.',version='0.1.5',requires={'st-govideo-d':'0.1.5','mgu-render':'0.6.11','mgu-inputgate':'1.0.0','mgu-uplink':'0.7.0'},uninstallable=True,mguHmi=['ID7']))
    for component,file in [(service/'component.json',dist/'st-govideo-d@0.1.5.tgz'),(ROOT/'vendor-inputgate/component.json',dist/'mgu-inputgate@1.1.0.tgz')]:
        add(file,json.loads(component.read_text(encoding='utf8')))
    node=json.loads((ROOT/'vendor-artifacts/upstream-catalog.json').read_text(encoding='utf8')) if (ROOT/'vendor-artifacts/upstream-catalog.json').is_file() else None
    node_entry=next((x for x in node['components'] if x['id']=='jap-node22'),None) if node else dict(id='jap-node22',kind='tool',title='Node.js 22',version='22.23.4',requires={'mgu-deploy':'0.7.0'},uninstallable=True,mguHmi=['ID7'])
    add(cache/'jap-node22@22.23.4.tgz',node_entry)
    for entry in lock['store_components']: add(cache/entry['artifact']['name'],entry)
    byid={x['id']:x for x in rows}
    for item in rows:
        for name,minimum in item.get('requires',{}).items():
            if name not in byid or tuple(map(int,byid[name]['version'].split('.'))) < tuple(map(int,minimum.split('.'))): raise ValueError('Incomplete dependency closure: '+name)
    (folder/'catalog.json').write_text(json.dumps(dict(schema=2,components=rows),ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    with tarfile.open(dist/'st_go_video@0.1.5.tgz') as src:
        if any('oap-sdk' in m.name for m in src): raise ValueError('SDK was not removed')
    print('USB kit ready: '+str(output),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk-dir');parser.add_argument('--output',type=Path,default=ROOT/'dist/Go Video USB')
    args=parser.parse_args();build(args.output.resolve(),args.sdk_dir)
