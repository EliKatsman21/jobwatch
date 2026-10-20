#!/usr/bin/env python3
"""find_ats - discover which job system each company in config.json "roster" uses.

For every roster entry it opens the careers page (plus up to 3 jobs-looking links
on it), looks for Greenhouse / Lever / Ashby / Workday / Comeet / SmartRecruiters
fingerprints, and adds a ready source to config.json "sources" (enabled).
A human-readable summary is written to discovery_report.md.

Run in GitHub Actions (workflow "discover") or locally:  python find_ats.py
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

HERE = Path(__file__).parent
CFG = HERE / "config.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PATTERNS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)"),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    "workday": re.compile(r"([A-Za-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"),
    "comeet": re.compile(r"comeet\.(?:co|com)/jobs/([A-Za-z0-9_-]+)/([A-Za-z0-9.]+)"),
    "smartrecruiters": re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
}
BAD_SLUGS = {"embed", "v1", "jobs", "js", "static", "assets", "api"}
TOKEN = re.compile(r"token[\"']?\s*[:=]\s*[\"']([A-Fa-f0-9]{8,})[\"']")
LINK = re.compile(r"href=[\"']([^\"'#]+)[\"']", re.I)
JOBLINK = re.compile(r"job|career|position|opening|join|hiring|vacanc|משר|קריר", re.I)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "ignore")


def detect(html, name):
    found = []
    for kind, pat in PATTERNS.items():
        for m in pat.finditer(html):
            g = m.groups()
            if kind in ("greenhouse", "lever", "ashby", "smartrecruiters"):
                if g[0].lower() in BAD_SLUGS:
                    continue
                key = {"greenhouse": "board", "lever": "company", "ashby": "board",
                       "smartrecruiters": "company"}[kind]
                found.append({"type": kind, "name": name, key: g[0]})
            elif kind == "workday":
                if g[1].lower() in BAD_SLUGS:
                    continue
                found.append({"type": "workday", "name": name, "host": g[0], "site": g[1],
                              "queries": [], "max_per_query": 100})
            elif kind == "comeet":
                t = TOKEN.search(html)
                if t:
                    found.append({"type": "comeet", "name": name, "uid": g[1], "token": t.group(1)})
                else:
                    found.append({"type": "comeet-notoken", "name": name, "uid": g[1]})
    return found


def discover(entry):
    name, url = entry["name"], entry["careers_url"]
    pages, found = [url], detect(url, name)   # the address itself may already name the system
    seen = set()
    if found:
        return found, ""
    while pages and len(seen) < 4:
        u = pages.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            html = get(u)
        except Exception as e:  # noqa: BLE001
            if u == url:
                return [], f"could not open page ({e})"
            continue
        found += detect(html, name)
        if found:
            break
        if len(seen) == 1:
            host = urlparse(url).netloc
            for href in LINK.findall(html):
                full = urljoin(u, href)
                if JOBLINK.search(href) and urlparse(full).netloc.endswith(host.split(".", 1)[-1]):
                    pages.append(full)
                if len(pages) >= 3:
                    break
        time.sleep(0.5)
    return found, ("" if found else "no known job system found (page may be JavaScript-rendered)")


def main():
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    have = {(s["name"]) for s in cfg["sources"]}
    report = ["# Discovery report", ""]
    added = 0
    for entry in cfg.get("roster", []):
        name = entry["name"]
        if name in have:
            report.append(f"- {name}: already configured")
            continue
        found, err = discover(entry)
        good = [f for f in found if f["type"] != "comeet-notoken"]
        if good:
            src = good[0]
            if src["type"] == "workday":
                src["queries"] = entry.get("queries", ["Israel", "Haifa", "Yokneam"])
            cfg["sources"].append(src)
            added += 1
            report.append(f"- {name}: OK -> {src['type']}")
        elif found:
            report.append(f"- {name}: Comeet found (uid {found[0]['uid']}) but no token in HTML; add manually")
        else:
            report.append(f"- {name}: NOT DETECTED - {err}. Open {entry['careers_url']} and send the "
                          "address of the real job list to be added by hand")
        print(report[-1])
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (HERE / "discovery_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"added {added} sources")


if __name__ == "__main__":
    sys.exit(main())
