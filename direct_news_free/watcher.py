#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Railway-free PAO direct-news watcher.

Runs on the public pao-x-alert repository so GitHub-hosted Actions are not used
as a paid replacement for Railway. Shadow mode is the safe default: every
current source is scanned and state is built, but Telegram is not duplicated.
Delivery is enabled only after the Telegram secret/recipients are verified.
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, quote

import aiohttp
import feedparser
from bs4 import BeautifulSoup

STATE_PATH = Path("direct_news_state.json")
HEALTH_PATH = Path("direct_news_health.json")
POLL_SECONDS = max(60, int(os.getenv("DIRECT_NEWS_POLL_SECONDS", "120")))
INTL_PRIORITY_SECONDS = max(120, int(os.getenv("DIRECT_NEWS_INTL_PRIORITY_SECONDS", "120")))
INTL_BROAD_SECONDS = max(240, int(os.getenv("DIRECT_NEWS_INTL_BROAD_SECONDS", "240")))
PROTOTHEMA_SECONDS = 45
SPORT_FM_TV_SECONDS = 90
CHECKPOINT_SECONDS = max(120, int(os.getenv("DIRECT_NEWS_CHECKPOINT_SECONDS", "300")))
MAX_RUNTIME_SECONDS = max(600, int(os.getenv("DIRECT_NEWS_MAX_RUNTIME_SECONDS", "13200")))
HTTP_TIMEOUT = max(6, int(os.getenv("DIRECT_NEWS_HTTP_TIMEOUT", "15")))
MAX_LINKS = max(40, int(os.getenv("DIRECT_NEWS_MAX_LINKS", "120")))
MAX_SEEN = max(10000, int(os.getenv("DIRECT_NEWS_MAX_SEEN", "30000")))
MAX_IDENTITIES = max(10000, int(os.getenv("DIRECT_NEWS_MAX_IDENTITIES", "30000")))
CONCURRENCY = max(4, int(os.getenv("DIRECT_NEWS_CONCURRENCY", "10")))
MAX_ARTICLE_AGE_HOURS = max(1, int(os.getenv("DIRECT_NEWS_MAX_AGE_HOURS", "6")))

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_V2", "").strip()
DELIVERY_ENABLED = os.getenv("DIRECT_NEWS_DELIVERY_ENABLED", "0").strip() == "1"
UA = "Mozilla/5.0 (compatible; PAODirectNewsFree/1.0; +https://github.com/andremoschos/pao-x-alert)"
TRACKING = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid","ref","source","output"}
EXCLUDED_PATHS = ("/tag/","/tags/","/category/","/author/","/search","/login","/register","/privacy","/terms","/contact","/feed","/rss","/wp-content/")
BAD_BLOCK_WORDS = ("related","recommended","recommend","suggested","read-more","readmore","also-read","more-news","sidebar","popular","trending","tags","social","newsletter","most-read","mostread","latest-news","other-news","more-articles","recommendations","advert","ads","promo","sponsored")
RELATED_TEXT_MARKERS = ("διαβαστε επισης","δειτε επισης","σχετικο αρθρο","σχετικα αρθρα","μπορει να σας ενδιαφερει","προτεινομενα","read also","also read","related article","related stories","see also","recommended")
STOP = asyncio.Event()
log = logging.getLogger("pao-direct-free")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

@dataclass(frozen=True)
class Source:
    name: str
    url: str
    team_specific: bool = False

@dataclass
class Item:
    source: str
    url: str
    title: str = ""
    published: str = ""
    context: str = ""

# Exact active base list from newspao_all_news_FINAL.py.
CORE_SOURCES = [
    Source("Monobala Latest V2","https://monobala.gr/roi-eidiseon/"),
    Source("Monobala Panathinaikos V2","https://monobala.gr/category/teams/sl1/panathinaikos/",True),
    Source("Monobala Tag V2","https://monobala.gr/tag/panathinaikos/",True),
    Source("SportFM Latest","https://www.sport-fm.gr/archive/latest/"),
    Source("Sportal","https://www.sportal.gr/athlitikanea"),
    Source("Gazzetta Latest","https://www.gazzetta.gr/latest-news"),
    Source("Sport24 Latest","https://www.sport24.gr/latest/"),
    Source("Athletiko Latest","https://www.athletiko.gr/latest-news"),
    Source("SDNA News","https://www.sdna.gr/news"),
    Source("TA NEA Sports","https://www.tanea.gr/category/sports/"),
    Source("in.gr inSports","https://www.in.gr/insports/"),
    Source("To10 Blog View","https://www.to10.gr/blog-view/"),
    Source("Pick and Roll","https://www.pickandroll.gr/category/eidiseis/"),
    Source("Sportdog Latest","https://www.sportdog.gr/latest"),
    Source("Onsports Latest","https://www.onsports.gr/latest-news"),
    Source("Eurohoops EL","https://www.eurohoops.net/el/"),
    Source("Panathinaikos24 Latest","https://panathinaikos24.gr/latest/",True),
    Source("Novasports Newsroom","https://www.novasports.gr/roi-enimerosis/"),
    Source("TransferFeed Panathinaikos","https://www.transferfeed.com/clubs/panathinaikos/53",True),
    Source("TransferFeed Global","https://www.transferfeed.com/"),
    Source("Filathlos","https://filathlos.gr/"),
    Source("Regista","https://regista.gr/"),
    Source("Agrinio24","https://agrinio24.gr/"),
    Source("ASTRATV","https://www.astratv.gr/"),
    Source("PAO Pantou","https://paopantou.gr/?post_type=post",True),
    Source("Gazzetta Panathinaikos","https://www.gazzetta.gr/teams/panathinaikos",True),
    Source("To10 Panathinaikos","https://www.to10.gr/team/panathinaikos/",True),
    Source("TA NEA Panathinaikos","https://www.tanea.gr/tag/%cf%80%ce%b1%ce%bd%ce%b1%ce%b8%ce%b7%ce%bd%ce%b1%cf%8a%ce%ba%cf%8c%cf%82/",True),
    Source("in.gr Panathinaikos","https://www.in.gr/tags/%cf%80%ce%b1%ce%bd%ce%b1%ce%b8%ce%b7%ce%bd%ce%b1%cf%8a%ce%ba%cf%8c%cf%82/",True),
    Source("Ole Panathinaikos","https://ole.gr/tag/panathinaikos/",True),
    Source("Trifilara","https://trifilara.gr/"),
    Source("OlaPrasina1908","https://olaprasina1908.gr/pao-nea/",True),
    Source("PAO FC Official","https://www.pao.gr/all-news/",True),
    Source("PAO BC Official","https://www.paobc.gr/en/news/",True),
    Source("EuroLeague Official","https://www.euroleaguebasketball.net/euroleague/news/"),
    Source("Contra","https://www.contra.gr/"),
    Source("Eurohoops EN","https://www.eurohoops.net/en/"),
    Source("BasketNews","https://basketnews.com/"),
    Source("beIN Panathinaikos","https://www.beinsports.com/en-mena/football/team/panathinaikos-fc",True),
]

# Exact restored direct list. TransferFeed is intentionally not duplicated.
RESTORED_SOURCES = [
    Source("NewsPao Home","https://newspao.gr/",True), Source("NewsPao Flow","https://newspao.gr/roi-eidiseon",True),
    Source("NewsPao Football","https://newspao.gr/podosfairo",True), Source("NewsPao Basket","https://newspao.gr/basket",True),
    Source("NewsPao Volei","https://newspao.gr/volei",True), Source("NewsPao Erasitechnis","https://newspao.gr/erasitexnis",True),
    Source("iNewsGR Sports","https://www.inewsgr.com/sport.htm"),
    Source("iNewsGR Panathinaikos","https://www.inewsgr.com/t/%CF%80%CE%B1%CE%BD%CE%B1%CE%B8%CE%B7%CE%BD%CE%B1%CE%B9%CE%BA%CE%BF%CF%82_panathinaikos.htm",True),
    Source("Betmarket","https://www.betmarket.gr/"), Source("Bet.gr","https://www.bet.gr/"), Source("Menshouse","https://menshouse.gr/"),
    Source("Foxbet Web","https://www.foxbet.gr/"), Source("Betarades Web","https://www.betarades.gr/"),
    Source("Betarades News","https://www.betarades.gr/athlitikes-eidiseis/"),
    Source("Betarades TV","https://www.betarades.gr/tileoptiko-programma-agonon-athlitikes-metadoseis-simera-kanalia-podosfairoy-mpasket/"),
    Source("StoiximaView News","https://stoiximaview.gr/category/eidiseis/"), Source("Newsbeast Sports","https://www.newsbeast.gr/sports"),
    Source("Radar Agent Stories","https://radar.gr/agent-stories"), Source("SDNA Panathinaikos","https://www.sdna.gr/teams/panathinaikos",True),
    Source("SDNA Panathinaikos Football","https://www.sdna.gr/teams/panathinaikos/podosfairo",True),
    Source("SDNA Panathinaikos Aktor","https://www.sdna.gr/teams/panathinaikos-aktor",True), Source("Prasinoforos","https://prasinoforos.gr/",True),
    Source("Metrosport Panathinaikos","https://www.metrosport.gr/panathinaikos",True),
    Source("ERT Sports Panathinaikos","https://www.ertsports.gr/category/omades/panathinaikos/",True),
    Source("Basketball Sphere Panathinaikos","https://basketballsphere.com/en/category/panathinaikos/",True),
    Source("Eurohoops Panathinaikos","https://www.eurohoops.net/basket/panathinaikos/?lang=en",True),
    Source("PAO 1908 Official","https://www.pao1908.com/latest/",True), Source("Parapolitika Sports","https://www.parapolitika.gr/sports/"),
    Source("News247 Sports","https://www.news247.gr/athlitika/"), Source("iefimerida Sports","https://www.iefimerida.gr/spor"),
    Source("CNN Greece Sports","https://www.cnn.gr/sports"), Source("SKAI Sports","https://www.skai.gr/news/sports"),
    Source("Naftemporiki Sports","https://www.naftemporiki.gr/sports/"), Source("To Vima Sports","https://www.tovima.gr/category/sports/"),
    Source("Sportklub BC Panathinaikos","https://sportklub.n1info.rs/tag/bc-panathinaikos/",True),
    Source("Sportklub Panathinaikos FC","https://sportklub.n1info.rs/tag/panathinaikos-f-c/",True),
    Source("Sportklub Panathinaikos","https://sportklub.n1info.rs/tag/panathinaikos/",True),
    Source("L'Équipe Panathinaikos","https://www.lequipe.fr/Football/FootballFicheClub433.html",True),
    Source("AS Panathinaikos","https://as.com/resultados/ficha/equipo/panathinaikos/335/",True),
]

INTL_PRIORITY_SOURCES = [
    Source("Mozzart Sport","https://www.mozzartsport.com/"), Source("Sportando","https://sportando.basketball/en/"),
    Source("Meridian Sport","https://meridiansport.rs/"), Source("Basketinside","https://www.basketinside.com/"),
    Source("Gigantes del Basket","https://www.gigantes.com/"), Source("Foot Mercato","https://www.footmercato.net/"),
    Source("RMC Sport","https://rmcsport.bfmtv.com/"), Source("Gazzetta dello Sport","https://www.gazzetta.it/"),
    Source("Tuttosport","https://www.tuttosport.com/"), Source("Fanatik","https://www.fanatik.com.tr/"),
    Source("Sporx","https://www.sporx.com/"), Source("Fotomac","https://www.fotomac.com.tr/"), Source("Sportal Bulgaria","https://sportal.bg/news"),
    Source("Gol.hr","https://gol.dnevnik.hr/"), Source("Sportske Novosti","https://sportske.jutarnji.hr/sn"),
    Source("Index Sport","https://www.index.hr/sport/"), Source("Panorama Sport","https://www.panorama.com.al/sport/"),
    Source("Dsport Bulgaria","https://dsport.bg/"), Source("Gong Bulgaria","https://gong.bg/"), Source("DZFoot","https://www.dzfoot.com/"),
    Source("Sportmedia MK","https://sportmedia.mk/"), Source("Nogomania","https://www.nogomania.com/"), Source("Kerkida","https://www.kerkida.net/"),
    Source("24Sports Cyprus","https://www.24sports.com.cy/"),
]

INTL_BROAD_SOURCES = [
    Source("Marca","https://www.marca.com/"), Source("ESPN","https://www.espn.com/"), Source("Mundo Deportivo","https://www.mundodeportivo.com/"),
    Source("Kicker","https://www.kicker.de/"), Source("Sport Bild","https://sportbild.bild.de/"), Source("A Bola","https://www.abola.pt/"),
    Source("Record Portugal","https://www.record.pt/"), Source("O Jogo","https://www.ojogo.pt/"), Source("Sky Sports","https://www.skysports.com/"),
    Source("BBC Sport","https://www.bbc.com/sport"), Source("The Athletic","https://www.nytimes.com/athletic/"),
    Source("The Guardian Football","https://www.theguardian.com/football"), Source("talkSPORT","https://talksport.com/football/"),
    Source("De Telegraaf Sport","https://www.telegraaf.nl/sport"), Source("Voetbal International","https://www.vi.nl/"),
    Source("RTBF Sport","https://www.rtbf.be/sport"), Source("Sporza","https://sporza.be/nl/"), Source("HLN Sport","https://www.hln.be/sport/"),
    Source("Hesport","https://www.hesport.com/"), Source("Le360 Sport","https://sport.le360.ma/"), Source("Globo Esporte","https://ge.globo.com/"),
    Source("Lance","https://www.lance.com.br/"), Source("Ole Argentina","https://www.ole.com.ar/"),
]

PROTOTHEMA_RSS = "https://www.protothema.gr/sports/rss/"
SPORT_FM_TV_GOOGLE_QUERY = ('("ΣΠΟΡ FM TV" OR "SPORT FM TV" OR "SPORT-FM TV" OR "SPORTFM TV" OR "ΣΠΟΡFM TV" OR "ΣΠΟΡ ΦΜ TV" OR "SPOR FM TV" OR "SPOR GM TV") when:1d')
SPORT_FM_TV_BING_QUERY = ('"ΣΠΟΡ FM TV" OR "SPORT FM TV" OR "SPORT-FM TV" OR "SPORTFM TV" OR "ΣΠΟΡFM TV" OR "ΣΠΟΡ ΦΜ TV" OR "SPOR FM TV" OR "SPOR GM TV"')

def now_iso(): return datetime.now(timezone.utc).isoformat()
def norm_text(s):
    s = unicodedata.normalize("NFD", (s or "").lower()); s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()
def relevant(text):
    t = norm_text(text); return "παναθηναικ" in t or "panathinaik" in t or "panathinaikos" in t or "paobc" in t

def normalize_url(url):
    try:
        p=urlparse(url); path=re.sub(r"/{2,}","/",p.path or "/"); path=path.rstrip("/") if path!="/" else path
        q=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k.lower() not in TRACKING and not k.lower().startswith("utm_")]
        return urlunparse(((p.scheme or "https").lower(),p.netloc.lower().removeprefix("www."),path,"",urlencode(q,doseq=True),""))
    except Exception: return url

def same_domain(a,b):
    da=urlparse(a).netloc.lower().removeprefix("www."); db=urlparse(b).netloc.lower().removeprefix("www.")
    return da==db or db.endswith("."+da)

def articleish(url,anchor=""):
    p=urlparse(url); path=(p.path or "").lower()
    if not p.scheme.startswith("http") or path in ("","/") or any(x in path for x in EXCLUDED_PATHS): return False
    if relevant(url) or re.search(r"/20\d{2}/\d{1,2}/",path) or re.search(r"/\d{5,}(?:/|$)",path): return True
    slug=path.strip("/")
    if "/" not in slug and len(slug)>=18 and "-" in slug: return True
    return (path.count("/")>=2 and len(path)>=15) or (len(anchor.strip())>=12 and len(path)>=10)

def parse_dt(value):
    value=(value or "").strip()
    if not value: return None
    try:
        dt=datetime.fromisoformat(value.replace("Z","+00:00")); dt=dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        return dt.astimezone(timezone.utc)
    except Exception: pass
    try:
        dt=parsedate_to_datetime(value); dt=dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt
        return dt.astimezone(timezone.utc) if dt else None
    except Exception: return None

def is_recent(value,hours=MAX_ARTICLE_AGE_HOURS,unknown_ok=True):
    dt=parse_dt(value)
    if dt is None: return unknown_ok
    age=datetime.now(timezone.utc)-dt
    return unknown_ok if age.total_seconds() < -3600 else age <= timedelta(hours=hours)

def looks_like_related(text): return any(m in norm_text(text)[:140] for m in RELATED_TEXT_MARKERS)

def clean_article_text(soup):
    for x in soup(["script","style","noscript","svg","nav","footer","header","form","aside"]):
        try: x.decompose()
        except Exception: pass
    candidates=[]; article=soup.find("article")
    if article: candidates.append(article)
    for selector in ("[itemprop='articleBody']",".article-body",".article-content",".entry-content",".post-content",".story-body",".content-body",".article__body",".articleBody",".article-main",".article-text"):
        try:
            node=soup.select_one(selector)
            if node and node not in candidates: candidates.append(node)
        except Exception: pass
    main=soup.find("main")
    if main and main not in candidates: candidates.append(main)
    for root in candidates or [soup]:
        for node in reversed(list(root.find_all(True))):
            attrs=getattr(node,"attrs",None)
            if not isinstance(attrs,dict): continue
            classes=attrs.get("class",[]) or []; classes=[classes] if isinstance(classes,str) else classes
            marker=(" ".join(map(str,classes))+" "+str(attrs.get("id",""))+" "+str(attrs.get("role",""))).lower()
            if any(w in marker for w in BAD_BLOCK_WORDS):
                try: node.decompose()
                except Exception: pass
        paras=[]
        for p in root.find_all("p"):
            try: txt=p.get_text(" ",strip=True)
            except Exception: continue
            if len(txt)>=20 and not looks_like_related(txt): paras.append(txt)
        text="\n".join(paras).strip()
        if len(text)>=80: return text[:50000]
    return ""

def body_hits(text):
    t=norm_text(text); return t.count("παναθηναικ")+t.count("panathinaik")+t.count("paobc")
def match_reason(source,item,body,body_ok):
    if relevant(item.title): return "title"
    if relevant(item.url): return "url"
    low=source.name.lower(); noisy=("inewsgr" in low or low.startswith("ta nea") or low=="astratv")
    if noisy:
        if low=="astratv" and body_hits(body)>=3 and body_hits(body[:10000])>=2: return "body-strong-astratv"
        if low!="astratv" and body_hits(body)>=2: return "body-strong"
        return ""
    if relevant(body): return "body"
    if source.team_specific and body_ok: return "team-page"
    return ""

def default_state(): return {"version":1,"initialized_sources":[],"seen":{},"identities":{},"recipients":{},"keyword_seen":{}}
def load_state():
    try:
        data=json.loads(STATE_PATH.read_text(encoding="utf-8")); base=default_state(); base.update(data); return base
    except Exception: return default_state()
def prune(d,limit):
    if len(d)<=limit: return d
    return dict(list(d.items())[-limit:])
def save_state(state):
    state["seen"]=prune(state.get("seen",{}),MAX_SEEN); state["identities"]=prune(state.get("identities",{}),MAX_IDENTITIES); state["keyword_seen"]=prune(state.get("keyword_seen",{}),5000)
    STATE_PATH.write_text(json.dumps(state,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
def seen_key(url): return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()
def identity_key(item):
    title=re.sub(r"\s+"," ",re.sub(r"[^a-z0-9α-ω]+"," ",norm_text(html.unescape(item.title or "")))).strip()
    domain=urlparse(item.url or "").netloc.lower().removeprefix("www.")
    return hashlib.sha256((domain+"\n"+title).encode("utf-8")).hexdigest() if domain and len(title)>=12 else ""
def is_seen(state,url): return seen_key(url) in state["seen"]
def mark_seen(state,item,url=None):
    u=normalize_url(url or item.url); state["seen"][seen_key(u)]={"url":u,"source":item.source,"at":now_iso()}
    ident=identity_key(item)
    if ident: state["identities"][ident]={"source":item.source,"at":now_iso()}

async def fetch(session,url):
    try:
        async with session.get(url,headers={"User-Agent":UA,"Accept-Language":"el-GR,el;q=0.9,en;q=0.8","Cache-Control":"no-cache, no-store, max-age=0"},timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),allow_redirects=True) as r:
            return ("",str(r.url),r.status) if r.status>=400 else (await r.text(errors="ignore"),str(r.url),r.status)
    except Exception: return "",url,0

def transferfeed_items(source,body,final):
    soup=BeautifulSoup(body,"html.parser"); out={}
    for a in soup.find_all("a",href=True):
        url=normalize_url(urljoin(final or source.url,a.get("href",""))); p=urlparse(url); host=(p.hostname or "").lower()
        if not (host=="transferfeed.com" or host.endswith(".transferfeed.com")) or not (p.path or "").lower().startswith("/transfers/"): continue
        node=a; context=" ".join(a.stripped_strings)
        for _ in range(6):
            node=getattr(node,"parent",None)
            if node is None: break
            text=re.sub(r"\s+"," "," ".join(node.stripped_strings)).strip()
            if relevant(text) and len(text)<=1200: context=text; break
            if 0<len(text)<=1600: context=text
            if len(text)>2200: break
        if not (relevant(context) or relevant(url)): continue
        out[url]=Item("TransferFeed Panathinaikos",url,re.sub(r"\s+"," ",context).strip()[:500] or "TransferFeed · Panathinaikos","",context)
    return list(out.values())[:MAX_LINKS]

def listing_items(source,body,final):
    if "transferfeed" in source.name.lower(): return transferfeed_items(source,body,final)
    soup=BeautifulSoup(body,"html.parser"); dedup={}
    for a in soup.find_all("a",href=True):
        href=(a.get("href") or "").strip()
        if not href or href.startswith(("#","javascript:","mailto:","tel:")): continue
        url=normalize_url(urljoin(final or source.url,href)); txt=re.sub(r"\s+"," "," ".join(a.stripped_strings)).strip()
        if same_domain(source.url,url) and articleish(url,txt) and (url not in dedup or len(txt)>len(dedup[url])): dedup[url]=txt
    return [Item(source.name,u,t) for u,t in list(dedup.items())[:MAX_LINKS]]

async def hydrate(session,item):
    body,final,status=await fetch(session,item.url)
    if not body: return item,"",False,status
    item.url=normalize_url(final); soup=BeautifulSoup(body,"html.parser")
    for attrs in ({"property":"og:title"},{"name":"twitter:title"}):
        m=soup.find("meta",attrs=attrs)
        if m and m.get("content"): item.title=m.get("content").strip(); break
    if not item.title and soup.title: item.title=soup.title.get_text(" ",strip=True)
    for attrs in ({"property":"article:published_time"},{"itemprop":"datePublished"},{"name":"date"},{"name":"pubdate"}):
        m=soup.find("meta",attrs=attrs)
        if m and m.get("content"): item.published=m.get("content")[:80]; break
    if not item.published:
        t=soup.find("time")
        if t: item.published=(t.get("datetime") or t.get_text(" ",strip=True))[:80]
    return item,clean_article_text(soup),True,status

async def discover_recipients(session,state):
    rec=state.get("recipients") or {}; primary=str(rec.get("primary","")).strip(); mirror=str(rec.get("mirror","")).strip()
    if primary and mirror and primary!=mirror: return primary,mirror
    if not TOKEN: return None
    try:
        async with session.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as r: payload=await r.json(content_type=None)
        starts=[]
        for upd in payload.get("result",[]):
            msg=upd.get("message") or upd.get("edited_message"); chat=(msg or {}).get("chat") or {}
            if msg and chat.get("type")=="private" and chat.get("id") is not None and str(msg.get("text","") or "").strip().casefold().startswith("/start"): starts.append((int(upd.get("update_id",0) or 0),str(chat["id"])))
        latest={}
        for uid,cid in sorted(starts): latest[cid]=uid
        ordered=sorted(latest,key=lambda cid:latest[cid])
        if len(ordered)>=2:
            primary,mirror=ordered[-1],ordered[-2]; state["recipients"]={"primary":primary,"mirror":mirror,"locked_at":now_iso()}; save_state(state); return primary,mirror
    except Exception as exc: log.warning("Telegram recipient discovery failed: %s",exc)
    return None

async def tg_post(session,chat_id,text):
    data={"chat_id":chat_id,"text":text,"parse_mode":"HTML","link_preview_options":json.dumps({"is_disabled":True})}
    async with session.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data=data,timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as r:
        payload=await r.json(content_type=None)
        if not payload.get("ok"): raise RuntimeError(payload)

async def send_alert(session,state,item,prefix="🚨 <b>ΝΕΟ ΓΙΑ ΠΑΝΑΘΗΝΑΪΚΟ</b>"):
    if not DELIVERY_ENABLED: log.info("SHADOW would send | %s | %s",item.source,(item.title or item.url)[:120]); return
    if not TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN_V2 missing")
    recipients=await discover_recipients(session,state)
    if not recipients: raise RuntimeError("Two Telegram /start recipients not discovered")
    src=html.escape((item.source or "Άγνωστη πηγή")[:80]); title=html.escape(re.sub(r"\s+"," ",item.title or "Νέα δημοσίευση").strip()[:500]); url=html.escape(item.url,quote=True); pub=html.escape((item.published or "τώρα")[:80])
    text=f"{prefix}\n🟢 <b>{src}</b>\n📰 {title}\n🕒 {pub}\n🔗 <a href=\"{url}\">Άνοιγμα άρθρου</a>"
    primary,mirror=recipients; await tg_post(session,primary,text)
    try: await tg_post(session,mirror,text)
    except Exception as exc: log.warning("Telegram mirror failed after primary: %s",exc)

async def process_source(session,state,source,health):
    started=time.time(); body,final,status=await fetch(session,source.url); h=health["sources"].setdefault(source.name,{})
    h.update({"last_attempt":now_iso(),"http_status":status})
    if not body: h.update({"status":"error","last_error":f"fetch failed HTTP {status}","duration":round(time.time()-started,2)}); return
    items=listing_items(source,body,final); previous_visible=int(h.get("visible_items",0) or 0)
    h.update({"status":"ok" if items or previous_visible==0 else "degraded","visible_items":len(items),"last_ok":now_iso(),"last_error":None if items or previous_visible==0 else "listing returned 0 items","duration":round(time.time()-started,2)})
    first=source.name not in state["initialized_sources"]; sent=0
    for raw in items:
        if is_seen(state,raw.url): continue
        original=raw.url; item,article_text,body_ok,_=await hydrate(session,raw)
        if is_seen(state,item.url): mark_seen(state,raw,original); continue
        ident=identity_key(item)
        if ident and ident in state["identities"]: mark_seen(state,item,original); mark_seen(state,item); continue
        reason=match_reason(source,item,article_text,body_ok)
        if first or not DELIVERY_ENABLED: mark_seen(state,item,original); mark_seen(state,item); continue
        if not body_ok and not reason: continue
        if not is_recent(item.published,unknown_ok=True): mark_seen(state,item,original); mark_seen(state,item); continue
        if not reason:
            if body_ok: mark_seen(state,item,original); mark_seen(state,item)
            continue
        await send_alert(session,state,item); mark_seen(state,item,original); mark_seen(state,item); sent+=1
    if first: state["initialized_sources"].append(source.name)
    h["sent"]=int(h.get("sent",0) or 0)+sent; save_state(state)

async def process_group(session,state,sources,health):
    sem=asyncio.Semaphore(CONCURRENCY)
    async def one(src):
        async with sem:
            try: await process_source(session,state,src,health)
            except Exception as exc:
                health["sources"].setdefault(src.name,{}).update({"status":"error","last_error":f"{type(exc).__name__}: {exc}","last_attempt":now_iso()}); log.warning("source failed %s: %s",src.name,exc)
    await asyncio.gather(*(one(s) for s in sources),return_exceptions=True)

async def protothema_cycle(session,state,health):
    body,_,status=await fetch(session,PROTOTHEMA_RSS); lane=health["lanes"].setdefault("protothema_rss",{}); lane.update({"last_attempt":now_iso(),"http_status":status})
    if not body: lane.update({"status":"error","last_error":f"feed HTTP {status}"}); return
    feed=feedparser.parse(body); items=[]
    for e in list(feed.entries)[:80]:
        url=normalize_url(getattr(e,"link","") or "")
        if url: items.append(Item("ProtoThema Sports RSS",url,getattr(e,"title","") or "",getattr(e,"published","") or getattr(e,"updated","") or ""," ".join([getattr(e,"title","") or "",getattr(e,"summary","") or "",getattr(e,"description","") or ""])))
    first="lane::protothema_rss" not in state["initialized_sources"]; sent=0
    for raw in items:
        if is_seen(state,raw.url): continue
        original=raw.url; item,article_text,body_ok,_=await hydrate(session,raw); reason="rss" if relevant(raw.context) else match_reason(Source("ProtoThema Sports RSS",PROTOTHEMA_RSS),item,article_text,body_ok)
        if first or not DELIVERY_ENABLED: mark_seen(state,item,original); mark_seen(state,item); continue
        if not body_ok and not reason: continue
        if not reason or not is_recent(item.published,unknown_ok=True): mark_seen(state,item,original); mark_seen(state,item); continue
        await send_alert(session,state,item); mark_seen(state,item,original); mark_seen(state,item); sent+=1
    if first: state["initialized_sources"].append("lane::protothema_rss")
    save_state(state); lane.update({"status":"ok","last_ok":now_iso(),"items":len(items),"sent":int(lane.get("sent",0) or 0)+sent,"last_error":None})

def sportfm_urls():
    return [("Google News · ΣΠΟΡ FM TV",f"https://news.google.com/rss/search?q={quote(SPORT_FM_TV_GOOGLE_QUERY)}&hl=el&gl=GR&ceid=GR:el"),("Bing News · ΣΠΟΡ FM TV","https://www.bing.com/news/search?"+urlencode({"q":SPORT_FM_TV_BING_QUERY,"format":"RSS","setlang":"el-GR"}))]
async def sportfm_cycle(session,state,health):
    lane=health["lanes"].setdefault("sport_fm_tv_keyword",{}); combined=[]; errors=[]
    for label,url in sportfm_urls():
        body,_,status=await fetch(session,url)
        if not body: errors.append(f"{label}:HTTP {status}"); continue
        feed=feedparser.parse(body)
        for e in list(feed.entries)[:80]:
            u=normalize_url(getattr(e,"link","") or "")
            if not u: continue
            publisher=""
            try: publisher=(e.source.get("title","") or "").strip()
            except Exception: pass
            combined.append(Item(publisher or label,u,getattr(e,"title","") or "",getattr(e,"published","") or getattr(e,"updated","") or ""))
    uniq={i.url:i for i in combined}; first="lane::sport_fm_tv" not in state["initialized_sources"]; sent=0
    for item in uniq.values():
        k=seen_key(item.url)
        if k in state["keyword_seen"]: continue
        if first or not DELIVERY_ENABLED: state["keyword_seen"][k]={"url":item.url,"at":now_iso()}; continue
        await send_alert(session,state,item,prefix="🔎 <b>KEYWORD ALERT · ΣΠΟΡ FM TV</b>"); state["keyword_seen"][k]={"url":item.url,"at":now_iso()}; sent+=1
    if first: state["initialized_sources"].append("lane::sport_fm_tv")
    save_state(state); lane.update({"status":"ok" if not errors else "degraded","last_ok":now_iso(),"items":len(uniq),"sent":int(lane.get("sent",0) or 0)+sent,"errors":errors})

def run_git(args,check=True): return subprocess.run(args,text=True,capture_output=True,check=check)
def checkpoint(state,health):
    save_state(state); health["checked_at"]=now_iso(); health["mode"]="production" if DELIVERY_ENABLED else "shadow"; health["telegram_secret_present"]=bool(TOKEN)
    health["source_inventory"]={"core":len(CORE_SOURCES),"restored":len(RESTORED_SOURCES),"international_priority":len(INTL_PRIORITY_SOURCES),"international_broad":len(INTL_BROAD_SOURCES),"special_lanes":2,"total_direct_pages":len(CORE_SOURCES)+len(RESTORED_SOURCES)+len(INTL_PRIORITY_SOURCES)+len(INTL_BROAD_SOURCES)}
    HEALTH_PATH.write_text(json.dumps(health,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    if not os.getenv("GITHUB_ACTIONS"): return
    try:
        run_git(["git","config","user.name","pao-direct-free"]); run_git(["git","config","user.email","actions@users.noreply.github.com"]); run_git(["git","add","--",str(STATE_PATH),str(HEALTH_PATH)])
        if subprocess.run(["git","diff","--cached","--quiet"]).returncode==0: return
        run_git(["git","commit","-m","Update free direct-news watcher state"])
        for attempt in range(1,6):
            pull=subprocess.run(["git","pull","--rebase","origin","main"],text=True,capture_output=True)
            if pull.returncode!=0: subprocess.run(["git","rebase","--abort"],capture_output=True); time.sleep(attempt); continue
            push=subprocess.run(["git","push","origin","HEAD:main"],text=True,capture_output=True)
            if push.returncode==0: return
            time.sleep(attempt)
        raise RuntimeError("state push failed after retries")
    except Exception as exc:
        health["state_push_error"]=f"{type(exc).__name__}: {exc}"; HEALTH_PATH.write_text(json.dumps(health,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8"); log.warning("checkpoint failed: %s",exc)

async def main():
    state=load_state(); health={"started_at":now_iso(),"runner_alive_at":now_iso(),"sources":{},"lanes":{}}; start=time.monotonic()
    last_core=last_ip=last_ib=last_proto=last_keyword=last_checkpoint=0.0; connector=aiohttp.TCPConnector(limit=CONCURRENCY+6,ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        if DELIVERY_ENABLED:
            if not TOKEN: raise SystemExit("Delivery enabled but TELEGRAM_BOT_TOKEN_V2 is missing")
            if not await discover_recipients(session,state): raise SystemExit("Delivery enabled but two Telegram /start recipients were not found")
        while not STOP.is_set() and time.monotonic()-start<MAX_RUNTIME_SECONDS:
            now_m=time.monotonic(); health["runner_alive_at"]=now_iso(); tasks=[]
            if not last_core or now_m-last_core>=POLL_SECONDS: tasks.append(process_group(session,state,CORE_SOURCES+RESTORED_SOURCES,health)); last_core=now_m
            if not last_ip or now_m-last_ip>=INTL_PRIORITY_SECONDS: tasks.append(process_group(session,state,INTL_PRIORITY_SOURCES,health)); last_ip=now_m
            if not last_ib or now_m-last_ib>=INTL_BROAD_SECONDS: tasks.append(process_group(session,state,INTL_BROAD_SOURCES,health)); last_ib=now_m
            if not last_proto or now_m-last_proto>=PROTOTHEMA_SECONDS: tasks.append(protothema_cycle(session,state,health)); last_proto=now_m
            if not last_keyword or now_m-last_keyword>=SPORT_FM_TV_SECONDS: tasks.append(sportfm_cycle(session,state,health)); last_keyword=now_m
            if tasks:
                cs=time.time(); await asyncio.gather(*tasks,return_exceptions=True); health["last_cycle_finished_at"]=now_iso(); health["last_cycle_duration_seconds"]=round(time.time()-cs,2)
            if not last_checkpoint or now_m-last_checkpoint>=CHECKPOINT_SECONDS: checkpoint(state,health); last_checkpoint=now_m
            try: await asyncio.wait_for(STOP.wait(),timeout=5)
            except asyncio.TimeoutError: pass
    checkpoint(state,health); log.info("handoff after %.1f minutes",(time.monotonic()-start)/60)

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: STOP.set()
