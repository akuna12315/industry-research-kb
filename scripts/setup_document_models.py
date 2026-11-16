"""Fetch official public CPU vision weights into this project's work directory."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time

import httpx

ROOT=Path(__file__).resolve().parents[1]
REPO='Qwen/Qwen3-VL-2B-Instruct-GGUF'
FILES=('Qwen3VL-2B-Instruct-Q4_K_M.gguf','mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf')


def main():
    folder=ROOT/'work/document-models';folder.mkdir(parents=True,exist_ok=True)
    with httpx.Client(trust_env=False,timeout=30) as client:
        response=client.get('https://huggingface.co/api/models/'+REPO+'?blobs=true');response.raise_for_status();meta=response.json()
    def download(name):
        info=next(row for row in meta['siblings'] if row['rfilename']==name)
        target=folder/name;sha=info['lfs']['sha256'];size=info['lfs']['size']
        if target.exists() and target.stat().st_size==size:
            with target.open('rb') as source:
                if hashlib.file_digest(source,'sha256').hexdigest()==sha:
                    return {'file':name,'sha256':sha,'bytes':size}
        stage=target.with_suffix('.download')
        for attempt in range(5):
            received=stage.stat().st_size if stage.exists() else 0
            if received==size:break
            try:
                with httpx.Client(trust_env=False,follow_redirects=True,timeout=45) as client:
                    with client.stream('GET',f'https://huggingface.co/{REPO}/resolve/{meta["sha"]}/{name}',
                            headers={'Range':f'bytes={received}-'} if received else {}) as stream:
                        stream.raise_for_status()
                        append=received>0 and stream.status_code==206
                        if append and not stream.headers.get('content-range','').startswith(f'bytes {received}-'):
                            raise ValueError('Invalid public download range')
                        if not append:received=0
                        with stage.open('ab' if append else 'wb') as output:
                            for part in stream.iter_bytes(4*1024*1024):
                                output.write(part);received+=len(part)
                                if received//(128*1024*1024)!=(received-len(part))//(128*1024*1024):
                                    print(json.dumps({'file':name,'bytes':received,'total':size}),flush=True)
                break
            except httpx.HTTPError:
                print(json.dumps({'file':name,'retry':attempt+1,'saved_bytes':received}),flush=True)
                if attempt==4:raise
                time.sleep(1)
        with stage.open('rb') as source:digest=hashlib.file_digest(source,'sha256')
        if stage.stat().st_size!=size or digest.hexdigest()!=sha:
            raise ValueError('Public model checksum mismatch: '+name)
        stage.replace(target)
        return {'file':name,'sha256':sha,'bytes':size}
    with ThreadPoolExecutor(max_workers=2) as pool:
        files=list(pool.map(download,FILES))
    manifest={'repository':REPO,'revision':meta['sha'],'files':files,'source_url':'https://huggingface.co/'+REPO,'role':'CPU image/table recognition, separate from answer model'}
    (folder/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
