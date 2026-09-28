"""Hlídač nabídek: načte zdroje ze sources.yaml, vyfiltruje pozice a pošle upozornění."""
import json
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
import yaml

STATE = "state.json"
UA = {"User-Agent": "Mozilla/5.0 (osobni hlidac nabidek)"}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=30, **kw)
    r.raise_for_status()
    return r


# Konektory vrací seznam (id, název, lokalita, url).
def greenhouse(s):
    jobs = get(f"https://boards-api.greenhouse.io/v1/boards/{s['board']}/jobs").json()["jobs"]
    return [(str(j["id"]), j["title"], (j.get("location") or {}).get("name", ""), j["absolute_url"]) for j in jobs]


def lever(s):
    host = "api.eu.lever.co" if s.get("eu") else "api.lever.co"
    jobs = get(f"https://{host}/v0/postings/{s['company']}", params={"mode": "json"}).json()
    return [(j["id"], j["text"], (j.get("categories") or {}).get("location") or "", j["hostedUrl"]) for j in jobs]


def smartrecruiters(s):
    params = {"limit": 100}
    if s.get("query"):
        params["q"] = s["query"]
    jobs = get(f"https://api.smartrecruiters.com/v1/companies/{s['company']}/postings", params=params).json()["content"]
    return [(j["id"], j["name"], (j.get("location") or {}).get("city", ""),
             f"https://jobs.smartrecruiters.com/{s['company']}/{j['id']}") for j in jobs]


def workday(s):
    api = f"https://{s['host']}/wday/cxs/{s['tenant']}/{s['site']}/jobs"
    out = []
    for offset in (0, 20, 40):
        r = requests.post(api, headers=UA, timeout=30, json={
            "appliedFacets": {}, "limit": 20, "offset": offset, "searchText": s.get("query", "")})
        r.raise_for_status()
        posts = r.json().get("jobPostings", [])
        out += [(p["externalPath"], p["title"], p.get("locationsText", ""),
                 f"https://{s['host']}/{s['site']}{p['externalPath']}") for p in posts]
        if len(posts) < 20:
            break
    return out


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.href, self.txt = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href, self.txt = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self.href:
            self.txt.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.out.append((self.href, " ".join("".join(self.txt).split())))
            self.href = None


TRACKING = {"searchid", "rps", "refid", "trackingid", "fbclid", "gclid"}


def clean(url):
    """Odstraní sledovací parametry, aby se ID nabídky neměnilo při každém načtení."""
    u = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(u.query)
         if not k.lower().startswith("utm_") and k.lower() not in TRACKING]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))


_pw = {}


def rendered_html(url, wait):
    """Otevře stránku ve skutečném prohlížeči (Playwright), aby se provedl JavaScript."""
    if "b" not in _pw:
        from playwright.sync_api import sync_playwright
        _pw["p"] = sync_playwright().start()
        _pw["b"] = _pw["p"].chromium.launch()
    pg = _pw["b"].new_page(user_agent=UA["User-Agent"], locale="cs-CZ")
    try:
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            pg.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        pg.wait_for_timeout(int(wait * 1000))
        return pg.content()
    finally:
        pg.close()


def page(s):
    if s.get("render"):
        html = rendered_html(s["url"], s.get("wait", 5))
    else:
        html = get(s["url"]).content.decode("utf-8", "replace")
    p = Links()
    p.feed(html)
    out = []
    for u, txt in p.out:
        if len(txt) > 3:
            full = clean(urljoin(s["url"], u))
            out.append((full, txt, "", full))
    return out


def rss(s):
    out = []
    for e in ET.fromstring(get(s["url"]).content).iter():
        if e.tag.split("}")[-1] in ("item", "entry"):
            f = {c.tag.split("}")[-1]: c for c in e}
            link = f["link"].get("href") or f["link"].text or ""
            title = re.sub("<[^>]+>", "", "".join(f["title"].itertext()))
            out.append((link, title, "", link))
    return out


CONNECTORS = {f.__name__: f for f in (greenhouse, lever, smartrecruiters, workday, page, rss)}


def notify(title, body="", url=""):
    print(f"[UPOZORNĚNÍ] {title} | {body} | {url}")
    if os.getenv("NTFY_TOPIC"):
        msg = {"topic": os.environ["NTFY_TOPIC"], "title": title, "message": body or title,
               "priority": 4, "tags": ["briefcase"]}
        if url:
            msg["click"] = url
        requests.post("https://ntfy.sh", json=msg, timeout=30).raise_for_status()
    if os.getenv("TG_TOKEN") and os.getenv("TG_CHAT"):
        text = "\n".join(x for x in (title, body, url) if x)
        requests.post(f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
                      json={"chat_id": os.environ["TG_CHAT"], "text": text}, timeout=30).raise_for_status()


def main():
    cfg = yaml.safe_load(open("sources.yaml", encoding="utf-8"))
    rx = lambda items: [re.compile(norm(p)) for p in items or []]
    sources = cfg.get("sources") or []

    fresh = not os.path.exists(STATE)
    state = {} if fresh else json.load(open(STATE, encoding="utf-8"))
    if fresh:
        notify("Hlídač stáží běží", f"Sleduji {len(sources)} zdrojů. Upozornění budou chodit sem.")

    new = []
    for s in sources:
        try:
            jobs = CONNECTORS[s["type"]](s)
        except Exception as e:  # jeden rozbitý zdroj nesmí zastavit ostatní
            print(f"[CHYBA] {s['name']}: {e}")
            continue
        print(f"[OK] {s['name']}: načteno {len(jobs)} položek"
              + ("" if jobs else " (0 = stránka nejspíš vykresluje nabídky JavaScriptem, zkus jiný typ zdroje)"))
        first, seen = s["name"] not in state, set(state.get(s["name"], []))
        pick = lambda k: s[k] if k in s else cfg.get(k)  # nastavení u firmy přebije globální
        groups = [rx(g) for g in pick("filters") or []]
        excl, locs = rx(pick("exclude")), rx(pick("locations"))
        for jid, title, loc, url in jobs:
            if jid in seen:
                continue
            seen.add(jid)
            t = norm(title)
            ok = (all(any(p.search(t) for p in g) for g in groups)
                  and not any(p.search(t) for p in excl)
                  and (not loc or not locs or any(p.search(norm(loc)) for p in locs)))
            if ok:
                print(f"[{'AKTUÁLNĚ OTEVŘENO' if first else 'NOVÉ'}] {s['name']}: {title} ({loc}) {url}")
                if not first:
                    new.append((s["name"], title, loc, url))
        state[s["name"]] = sorted(seen)

    for name, title, loc, url in new[:10]:
        notify(f"{name}: {title[:100]}", loc or "Nová pozice", url)
    if len(new) > 10:
        notify("Další nové pozice", f"+{len(new) - 10} dalších, viz log v GitHub Actions")
    json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if "b" in _pw:
        _pw["b"].close()
        _pw["p"].stop()


if __name__ == "__main__":
    main()
