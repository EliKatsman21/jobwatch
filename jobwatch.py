#!/usr/bin/env python3
"""jobwatch - personal real-time job alert.

Polls public career-site APIs (Workday, Greenhouse, Lever, RSS), filters by
title/location, remembers what it already saw, and pushes new matches to your
phone (ntfy and/or Telegram) with a tap-to-open link.

Pure standard library. Usage:
    python jobwatch.py                 # normal run
    python jobwatch.py --dry-run       # print instead of sending
    python jobwatch.py --notify-existing   # first run: alert on everything currently open
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
UA = "Mozilla/5.0 (personal job alert script)"


# ---------------------------------------------------------------- http
def http(url, data=None, headers=None, retries=2, timeout=20):
    h = {"User-Agent": UA, "Accept": "application/json, text/xml, */*"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def job(company, title, location, url, jid, posted=""):
    return {"id": jid, "company": company, "title": (title or "").strip(),
            "location": (location or "").strip(), "url": url, "posted": posted}


# ---------------------------------------------------------------- sources
def fetch_workday(src, cfg):
    host, site = src["host"], src["site"]
    tenant = host.split(".")[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out, seen = [], set()
    for q in src.get("queries", ["Israel"]):
        offset = 0
        while offset < src.get("max_per_query", 100):
            d = json.loads(http(api, {"appliedFacets": {}, "limit": 20,
                                      "offset": offset, "searchText": q}))
            posts = d.get("jobPostings") or []
            if not posts:
                break
            for p in posts:
                path = p.get("externalPath", "")
                if not path or path in seen:
                    continue
                seen.add(path)
                out.append(job(src["name"], p.get("title"), p.get("locationsText"),
                               f"https://{host}/en-US/{site}{path}",
                               f"{src['name']}:{path}", p.get("postedOn", "")))
            offset += 20
    return out


def fetch_greenhouse(src, cfg):
    d = json.loads(http(f"https://boards-api.greenhouse.io/v1/boards/{src['board']}/jobs"))
    return [job(src["name"], j.get("title"), (j.get("location") or {}).get("name"),
                j.get("absolute_url"), f"{src['name']}:{j.get('id')}",
                j.get("updated_at", "")) for j in d.get("jobs", [])]


def fetch_lever(src, cfg):
    d = json.loads(http(f"https://api.lever.co/v0/postings/{src['company']}?mode=json"))
    return [job(src["name"], j.get("text"), (j.get("categories") or {}).get("location"),
                j.get("hostedUrl"), f"{src['name']}:{j.get('id')}") for j in d]


def fetch_rss(src, cfg):
    root = ET.fromstring(http(src["url"]))
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        out.append(job(src["name"], it.findtext("title"), "", link,
                       f"{src['name']}:{it.findtext('guid') or link}",
                       it.findtext("pubDate") or ""))
    return out


def fetch_ashby(src, cfg):
    d = json.loads(http(f"https://api.ashbyhq.com/posting-api/job-board/{src['board']}"))
    return [job(src["name"], j.get("title"), j.get("location") or j.get("address", {}).get("postalAddress", {}).get("addressLocality", ""),
                j.get("jobUrl"), f"{src['name']}:{j.get('id')}", j.get("publishedAt", ""))
            for j in d.get("jobs", [])]


def fetch_comeet(src, cfg):
    url = (f"https://www.comeet.co/careers-api/2.0/company/{src['uid']}/positions"
           f"?token={src['token']}&details=false")
    out = []
    for j in json.loads(http(url)):
        loc = (j.get("location") or {})
        where = ", ".join(x for x in (loc.get("city"), loc.get("name")) if x)
        out.append(job(src["name"], j.get("name"), where,
                       j.get("url_active_page") or j.get("url_comeet_hosted_page"),
                       f"{src['name']}:{j.get('uid')}", j.get("time_updated", "")))
    return out


def fetch_smartrecruiters(src, cfg):
    cid = src["company"]
    d = json.loads(http(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings?limit=100"
                        f"&country={src.get('country', 'il')}"))
    return [job(src["name"], j.get("name"), (j.get("location") or {}).get("city"),
                f"https://jobs.smartrecruiters.com/{cid}/{j.get('id')}",
                f"{src['name']}:{j.get('id')}", j.get("releasedDate", ""))
            for j in d.get("content", [])]


FETCH = {"workday": fetch_workday, "greenhouse": fetch_greenhouse,
         "lever": fetch_lever, "rss": fetch_rss, "ashby": fetch_ashby,
         "comeet": fetch_comeet, "smartrecruiters": fetch_smartrecruiters}


# ---------------------------------------------------------------- matching
def rx(patterns):
    return re.compile("|".join(patterns), re.I) if patterns else None


class Matcher:
    def __init__(self, cfg):
        f = cfg["filters"]
        self.include, self.exclude = rx(f["include"]), rx(f.get("exclude"))
        self.hot, self.loc = rx(f.get("hot")), rx(f.get("locations"))

    def verdict(self, j, src):
        t = j["title"]
        if not t or not self.include.search(t):
            return None
        if self.exclude and self.exclude.search(t):
            return None
        loc = j["location"]
        if self.loc and loc and not src.get("skip_location") and not self.loc.search(loc):
            return None
        return "hot" if self.hot and self.hot.search(t) else "ok"


# ---------------------------------------------------------------- notify
def fmt(j, v):
    mark = "HOT " if v == "hot" else ""
    where = f" | {j['location']}" if j["location"] else ""
    when = f" | {j['posted']}" if j["posted"] else ""
    return f"{mark}{j['title']} @ {j['company']}{where}{when}"


def send(title, message, url, hot, dry):
    if dry:
        print(f"[DRY] {title}\n      {message}\n      {url}\n")
        return
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        payload = {"topic": topic, "title": title, "message": message,
                   "priority": 5 if hot else 4,
                   "tags": ["rotating_light" if hot else "briefcase"]}
        if url:
            payload["click"] = url
        http(os.environ.get("NTFY_SERVER", "https://ntfy.sh"), payload)
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        text = f"{title}\n{message}\n{url or ''}"
        http(f"https://api.telegram.org/bot{tok}/sendMessage",
             {"chat_id": chat, "text": text, "disable_web_page_preview": False})


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--state", default=str(HERE / "seen.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--notify-existing", action="store_true")
    ap.add_argument("--replay", action="store_true",
                    help="test mode: alert on up to 5 CURRENTLY open matching jobs, save nothing")
    a = ap.parse_args()

    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    # Optional hand-written sources (sources_manual.json) - they never get overwritten
    # by the discovery tool, and win over a discovered source with the same name.
    mp = Path(a.config).with_name("sources_manual.json")
    if mp.exists():
        manual = json.loads(mp.read_text(encoding="utf-8"))
        names = {m["name"] for m in manual}
        cfg["sources"] = [x for x in cfg["sources"] if x["name"] not in names] + manual
    sp = Path(a.state)
    state = json.loads(sp.read_text()) if sp.exists() and not a.replay else {"jobs": {}}
    first_run = not sp.exists() and not a.replay
    matcher = Matcher(cfg)
    now = datetime.now(timezone.utc)

    found, errors = [], []
    for src in cfg["sources"]:
        if not src.get("enabled", True):
            continue
        try:
            jobs = FETCH[src["type"]](src, cfg)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{src['name']}: {e}")
            continue
        # A source we have never seen before is "seeded" silently: its currently-open
        # jobs are remembered but not announced, so adding companies never floods you.
        seeding = (not a.replay) and src["name"] not in state.setdefault("sources", [])
        for j in jobs:
            if j["id"] in state["jobs"]:
                continue
            state["jobs"][j["id"]] = now.isoformat()
            v = matcher.verdict(j, src)
            if v and not seeding:
                found.append((v, j))
        if seeding:
            state["sources"].append(src["name"])
            print(f"seeded {src['name']}: {len(jobs)} open jobs remembered")

    found.sort(key=lambda x: x[0] != "hot")
    muted = first_run and not a.notify_existing and not a.dry_run
    if muted:
        # Seed silently, send one "I'm listening" ping.
        send("jobwatch is live", f"Seeded {len(state['jobs'])} existing jobs "
             f"({len(found)} match your filters). New ones will ping you.", "", False, False)
    else:
        cap = 5 if a.replay else cfg.get("max_alerts_per_run", 15)
        for v, j in found[:cap]:
            send("New job: " + j["title"], fmt(j, v), j["url"], v == "hot", a.dry_run)
        if len(found) > cap:
            send("More new jobs", f"{len(found) - cap} more matches - check the list.", "", False, a.dry_run)

    # prune old entries so the state file stays small
    cutoff = (now - timedelta(days=cfg.get("keep_days", 120))).isoformat()
    state["jobs"] = {k: v for k, v in state["jobs"].items() if v >= cutoff}
    if not a.dry_run and not a.replay:
        sp.write_text(json.dumps(state, ensure_ascii=False, indent=0))

    ok = len([x for x in cfg["sources"] if x.get("enabled", True)]) - len(errors)
    print(f"sources ok: {ok}, failed: {len(errors)}, new matches: {len(found)}")
    for e in errors:
        print("ERROR", e, file=sys.stderr)


if __name__ == "__main__":
    main()
