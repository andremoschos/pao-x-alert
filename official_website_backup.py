import json, os, re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
import telegram_delivery as telegram

STATE = Path('official_website_backup_seen.json')
OFFICIAL_STATE = Path('official_seen.json')
TOPIC = os.environ['NTFY_OFFICIAL_TOPIC']
SOURCES = [
    ('PAE','site_pae','https://www.pao.gr/','pao.gr'),
    ('AO','site_ao','https://www.pao1908.com/category/nea/','pao1908.com'),
    ('KAE','site_kae','https://www.paobc.gr/news/','paobc.gr'),
]

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.items=[]; self.href=None; self.parts=[]
    def handle_starttag(self, tag, attrs):
        if tag=='a':
            self.href=dict(attrs).get('href'); self.parts=[]
    def handle_data(self, data):
        if self.href: self.parts.append(data)
    def handle_endtag(self, tag):
        if tag=='a' and self.href:
            text=' '.join(''.join(self.parts).split())
            if text: self.items.append((text,self.href))
            self.href=None; self.parts=[]

def load_ids(path):
    try: return set(map(str,json.loads(path.read_text(encoding='utf-8')).get('ids',[])))
    except Exception: return set()

def valid(url, host):
    p=urlparse(url); h=p.netloc.lower().removeprefix('www.')
    if h != host.removeprefix('www.'): return False
    path=p.path.rstrip('/')
    if not path or any(x in path for x in ('/category/','/tag/','/author/','/page/','/wp-content/')): return False
    return not path.lower().endswith(('.jpg','.png','.webp','.svg','.pdf','.css','.js'))

def notify(org,title,url):
    body=f'{title}\n{url}'
    tg_ok=telegram.send('official_pao',f'OFFICIAL PAO - {org} - WEBSITE',body,url)
    if tg_ok:
        print('TELEGRAM PRIMARY SENT', org, url)
    r=requests.post(f'https://ntfy.sh/{TOPIC}',data=body.encode('utf-8'),headers={'Title':f'OFFICIAL PAO - {org} - WEBSITE','Priority':'high','Tags':'green_circle','Click':url},timeout=20)
    if not tg_ok:
        r.raise_for_status()

def main():
    seen=load_ids(STATE); official=load_ids(OFFICIAL_STATE); changed=False
    for org,key,source,host in SOURCES:
        try:
            r=requests.get(source,headers={'User-Agent':'Mozilla/5.0'},timeout=30); r.raise_for_status()
            p=LinkParser(); p.feed(r.text); unique=[]; used=set()
            for title,href in p.items:
                url=urljoin(source,href).split('#')[0]
                if url in used or not valid(url,host): continue
                used.add(url); unique.append((title,url))
            print(f'{key} backup results: {len(unique)}')
            for title,url in unique[:40]:
                item_id=f'{key}:{url}'
                if item_id in official or item_id in seen: continue
                notify(org,title[:300],url); seen.add(item_id); changed=True
                print('BACKUP SENT',org,url)
        except Exception as exc:
            print(f'{key} backup error: {exc}')
    STATE.write_text(json.dumps({'ids':sorted(seen)},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('backup changed=',changed)

if __name__=='__main__': main()
