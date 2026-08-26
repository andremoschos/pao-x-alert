from pathlib import Path

path = Path("google_monitor.py")
text = path.read_text(encoding="utf-8")

new_domains = [
    "mozzartsport.com",
    "marca.com",
    "espn.com",
    "sportando.basketball",
    "meridiansport.rs",
    "basketinside.com",
    "gigantes.com",
    "basketballsphere.com",
    "mundodeportivo.com",
    "footmercato.net",
    "rmcsport.bfmtv.com",
    "gazzetta.it",
    "tuttosport.com",
    "kicker.de",
    "sportbild.bild.de",
    "abola.pt",
    "record.pt",
    "ojogo.pt",
    "skysports.com",
    "bbc.com",
    "theguardian.com",
    "talksport.com",
    "telegraaf.nl",
    "fanatik.com.tr",
    "sporx.com",
    "fotomac.com.tr",
    "vi.nl",
    "rtbf.be",
    "sporza.be",
    "sportal.bg",
    "hln.be",
    "gol.dnevnik.hr",
    "sportske.jutarnji.hr",
    "index.hr",
    "panorama.com.al",
    "dsport.bg",
    "gong.bg",
    "dzfoot.com",
    "hesport.com",
    "sport.le360.ma",
    "ge.globo.com",
    "lance.com.br",
    "ole.com.ar",
    "sportmedia.mk",
    "nogomania.com",
    "kerkida.net",
    "24sports.com.cy",
]

missing = [d for d in new_domains if f'    "{d}",' not in text]
if not missing:
    print("All new direct domains already present")
    raise SystemExit(0)

anchor = '    "beinsports.com",\n'
if anchor not in text:
    raise SystemExit("Expected direct-domain anchor not found")

addition = "".join(f'    "{domain}",\n' for domain in missing)
text = text.replace(anchor, anchor + addition, 1)
path.write_text(text, encoding="utf-8")
print(f"Added {len(missing)} new direct domains")
