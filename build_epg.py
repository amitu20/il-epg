#!/usr/bin/env python3
"""Build a merged XMLTV guide for the channels listed in channels.json.

Sources (fetched directly, no external grabber needed):
  freetv  - web.freetv.tv API      (Hebrew, ~7+ days ahead)
  yes     - svc.yes.co.il API      (Hebrew, ~7 days ahead)
  il1     - epgshare01 IL1 guide   (Hebrew, ~1.5 days ahead)
  x       - iptvx.one              (English, for HOT cinema etc.)
  prev    - the previously published epg.xml (keeps the guide filled if a
            source is temporarily down or short)

For every channel all matching sources are merged on a timeline: the highest
priority source wins, lower priority sources only fill the gaps and extend the
horizon. Channel display-names in the output equal the TiviMate channel names
so TiviMate can match them by name.
"""
import copy, difflib, gzip, json, re, subprocess, sys, time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

HERE = Path(__file__).parent
IPTVX_URL = "https://iptvx.one/EPG"
IL1_URL = "https://epgshare01.online/epgshare01/epg_ripper_IL1.xml.gz"
FREETV_API = "https://web.freetv.tv/api/products"
YES_API = "https://svc.yes.co.il/api/content/broadcast-schedule"
UA = "Mozilla/5.0 (Linux; Linux x86_64) AppleWebKit/600.3 (KHTML, like Gecko) Chrome/48.0.2544.291 Safari/600"
DAYS_AHEAD = 7            # how many days to fetch from the API sources
KEEP_PAST_HOURS = 12      # keep programmes that ended up to this long ago
SOURCE_PRIORITY = ["freetv", "yes", "il1", "x", "prev"]
OVERLAP_TOLERANCE = timedelta(minutes=3)
NOW = datetime.now(timezone.utc)
IL_TZ = timezone(timedelta(hours=2))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def norm(s):
    s = s.lower().replace("’", "'")
    s = re.sub(r"\((גיבוי|backup)[^)]*\)|-?\s*\(?גיבוי\s*\d*\)?|גיבוי", " ", s)
    s = re.sub(r"\b(hd|sd|4k|live|שידור חי|מונגש|ערוץ|channel|tv)\b", " ", s)
    s = re.sub(r"[^\w֐-׿+]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def http_get(url, headers=None, tries=3, timeout=60):
    hdrs = {"user-agent": UA, "accept-language": "he-IL"}
    hdrs.update(headers or {})
    err = None
    for i in range(tries):
        try:
            with urlopen(Request(url, headers=hdrs), timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            err = e
            if "CERTIFICATE_VERIFY_FAILED" in str(e):  # python without CA bundle: use curl
                r = subprocess.run(["curl", "-sfL", "-m", str(timeout)] + sum((["-H", f"{k}: {v}"] for k, v in hdrs.items()), []) + [url],
                                   capture_output=True)
                if r.returncode == 0:
                    return r.stdout
            time.sleep(2 * (i + 1))
    log(f"GET failed {url}: {err}")
    return None


def http_json(url, **kw):
    raw = http_get(url, **kw)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def fmt(dt):
    return dt.strftime("%Y%m%d%H%M%S %z")


def parse_xmltv_time(s):
    s = s.strip()
    if " " not in s:
        s += " +0000"
    return datetime.strptime(s, "%Y%m%d%H%M%S %z")


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def make_prog(start, stop, title, desc=None, icon=None, lang="he"):
    p = ET.Element("programme", {"start": fmt(start), "stop": fmt(stop)})
    ET.SubElement(p, "title", {"lang": lang}).text = title
    if desc:
        ET.SubElement(p, "desc", {"lang": lang}).text = desc
    if icon:
        ET.SubElement(p, "icon", {"src": icon})
    return p


def wanted_ids(sname, chans, channels, aliases):
    """Ids of a source's channels that can serve a channel in channels.json."""
    pool = {}
    for cid, name in chans.items():
        pool.setdefault(norm(name), cid)
    ids = set()
    for name in channels.values():
        base = re.sub(r"\s*-?\s*\(?גיבוי[^)]*\)?\s*-?\s*|^\(גיבוי\)\s*-\s*", "", name).strip()
        for s_, dn in aliases.get(name) or aliases.get(base) or []:
            if s_ == sname:
                ids.update(cid for cid, d in chans.items() if d == dn)
        n = norm(name)
        if n in pool:
            ids.add(pool[n])
        else:
            for m in difflib.get_close_matches(n, pool.keys(), n=1, cutoff=0.88):
                ids.add(pool[m])
    return ids


# ---------------------------------------------------------------- sources
# Every source loader returns (chans, progs):
#   chans: {id: display-name}
#   progs: {id: [programme Element, ...]}

def load_xmltv(data):
    root = ET.fromstring(data)
    chans = {c.get("id"): (c.findtext("display-name") or "") for c in root.findall("channel")}
    progs = {}
    for p in root.findall("programme"):
        progs.setdefault(p.get("channel"), []).append(p)
    return chans, progs


def load_il1():
    raw = http_get(IL1_URL, tries=3, timeout=120)
    if not raw:
        return {}, {}
    return load_xmltv(gzip.decompress(raw))


def load_iptvx(ids):
    """Stream the large iptvx.one guide and keep only the wanted channel ids."""
    if not ids:
        return {}, {}
    cmd = f"curl -sfL -m 300 --retry 3 {IPTVX_URL} | gunzip"
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
    chans, progs = {}, {}
    try:
        for _, el in ET.iterparse(proc.stdout, events=("end",)):
            if el.tag == "channel" and el.get("id") in ids:
                chans[el.get("id")] = el.findtext("display-name") or ""
            elif el.tag == "programme" and el.get("channel") in ids:
                progs.setdefault(el.get("channel"), []).append(el)
                continue
            if el.tag in ("channel", "programme"):
                el.clear()
    except ET.ParseError as e:
        log(f"iptvx parse error: {e}")
    return chans, progs


def load_freetv(channels, aliases):
    data = http_json(f"{FREETV_API}/lives?lang=HEB&platform=BROWSER&maxResults=200")
    if not data:
        return {}, {}
    chans = {str(i["id"]): i["title"].strip() for i in data.get("items", []) if i.get("title")}
    chans = {cid: n for cid, n in chans.items() if cid in wanted_ids("freetv", chans, channels, aliases)}
    today = NOW.astimezone(IL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    jobs = []
    for cid in chans:
        for d in range(DAYS_AHEAD + 1):
            since = today + timedelta(days=d)
            till = since + timedelta(days=1)
            q = f"liveId[]={cid}&since={since:%Y-%m-%dT%H:%M%z}&till={till:%Y-%m-%dT%H:%M%z}&lang=HEB&platform=BROWSER"
            jobs.append((cid, f"{FREETV_API}/lives/programmes?{q.replace('+', '%2B')}"))

    def fetch(job):
        cid, url = job
        items = http_json(url, tries=2, timeout=30) or []
        out = []
        for it in items if isinstance(items, list) else []:
            try:
                start, stop = parse_iso(it["since"]), parse_iso(it["till"])
            except (KeyError, ValueError):
                continue
            if not it.get("title") or stop <= start:
                continue
            img = ((it.get("images") or {}).get("16x9") or [{}])[0].get("url")
            out.append((cid, make_prog(start, stop, it["title"].strip(), it.get("description") or it.get("lead"),
                                       f"https:{img}" if img and img.startswith("//") else img)))
        return out

    progs = {}
    with ThreadPoolExecutor(6) as ex:
        for res in ex.map(fetch, jobs):
            for cid, p in res:
                progs.setdefault(cid, []).append(p)
    return chans, dedupe(progs)


def load_yes(channels, aliases):
    data = http_json(f"{YES_API}/channels?page=0&pageSize=1000")
    if not data:
        return {}, {}
    chans = {}
    for i in data.get("items", []):
        if i.get("channelId") and i.get("title"):
            chans.setdefault(i["channelId"], i["title"].strip())
    chans = {cid: n for cid, n in chans.items() if cid in wanted_ids("yes", chans, channels, aliases)}
    today = NOW.astimezone(IL_TZ).date()
    jobs = [(cid, f"{YES_API}/channels/{cid}?date={(today + timedelta(days=d)):%Y-%m-%d}&ignorePastItems=false")
            for cid in chans for d in range(DAYS_AHEAD + 1)]

    def fetch(job):
        cid, url = job
        data = http_json(url, tries=2, timeout=30) or {}
        out = []
        for it in data.get("items", []) if isinstance(data, dict) else []:
            try:
                start, stop = parse_iso(it["starts"]), parse_iso(it["ends"])
            except (KeyError, ValueError):
                continue
            if not it.get("title") or stop <= start:
                continue
            out.append((cid, make_prog(start, stop, it["title"].strip(), it.get("description"), it.get("imageUrl"))))
        return out

    progs = {}
    with ThreadPoolExecutor(3) as ex:  # gentle: the yes API sits behind a bot-protection WAF
        for res in ex.map(fetch, jobs):
            for cid, p in res:
                progs.setdefault(cid, []).append(p)
    return chans, dedupe(progs)


def load_prev():
    """Previously published guide, keyed by display name (future programmes only)."""
    path = HERE / "epg.xml"
    if not path.exists():
        return {}, {}
    try:
        chans, progs = load_xmltv(path.read_bytes())
    except ET.ParseError:
        return {}, {}
    cutoff = NOW - timedelta(hours=KEEP_PAST_HOURS)
    out_c, out_p = {}, {}
    for cid, name in chans.items():
        out_c[name] = name
        out_p[name] = [p for p in progs.get(cid, []) if parse_xmltv_time(p.get("stop")) > cutoff]
    return out_c, out_p


def dedupe(progs):
    for cid, lst in progs.items():
        seen, out = set(), []
        for p in sorted(lst, key=lambda p: p.get("start")):
            key = (p.get("start"), p.get("stop"))
            if key not in seen:
                seen.add(key)
                out.append(p)
        progs[cid] = out
    return progs


# ---------------------------------------------------------------- merging

def merge(candidates):
    """candidates: list of (source, cid, [programmes]) in priority order.
    Returns merged programme list (deep copies) and per-source counts."""
    taken = []  # (start, stop)
    out, used = [], {}

    def overlaps(s, e):
        for ts, te in taken:
            ov = min(e, te) - max(s, ts)
            if ov > OVERLAP_TOLERANCE or ov * 2 > min(e - s, te - ts):
                return True
        return False

    lo = NOW - timedelta(hours=KEEP_PAST_HOURS)
    hi = NOW + timedelta(days=DAYS_AHEAD + 1)
    for sname, cid, progs in candidates:
        n = 0
        for p in progs:
            try:
                s, e = parse_xmltv_time(p.get("start")), parse_xmltv_time(p.get("stop") or p.get("start"))
            except ValueError:
                continue
            if e <= lo or s >= hi or e <= s or overlaps(s, e):
                continue
            taken.append((s, e))
            q = copy.deepcopy(p)
            q.set("start", fmt(s.astimezone(timezone.utc)))
            q.set("stop", fmt(e.astimezone(timezone.utc)))
            out.append(q)
            n += 1
        if n:
            used[sname] = used.get(sname, 0) + n
    out.sort(key=lambda p: p.get("start"))
    return out, used


def parse_alias(v):
    items = v if isinstance(v, list) else [v]
    return [tuple(i.partition(":")[::2]) for i in items if ":" in i]


def main():
    channels = json.loads((HERE / "channels.json").read_text("utf8"))
    aliases = json.loads((HERE / "aliases.json").read_text("utf8")) if (HERE / "aliases.json").exists() else {}
    aliases = {k: parse_alias(v) for k, v in aliases.items()}
    xids = {cid for al in aliases.values() for s, cid in al if s == "x"}

    src = {}
    with ThreadPoolExecutor(4) as ex:
        futs = {"freetv": ex.submit(load_freetv, channels, aliases), "yes": ex.submit(load_yes, channels, aliases),
                "il1": ex.submit(load_il1), "x": ex.submit(load_iptvx, xids)}
        for k, f in futs.items():
            try:
                src[k] = f.result()
            except Exception as e:  # noqa: BLE001
                log(f"source {k} failed: {e}")
                src[k] = ({}, {})
    src["prev"] = load_prev()
    for k, (c, p) in src.items():
        log(f"source {k}: {len(c)} channels, {sum(len(v) for v in p.values())} programmes")

    # normalized name -> (source, id) per source, for automatic matching
    pools = {}
    for sname in SOURCE_PRIORITY:
        chans, progs = src.get(sname, ({}, {}))
        pool = {}
        for cid, name in chans.items():
            if len(progs.get(cid, [])) > 3:
                pool.setdefault(norm(name), cid)
        pools[sname] = pool

    out = ET.Element("tv", {"generator-info-name": "build_epg.py", "generator-info-url": "https://github.com/amitu20/il-epg"})
    progs_out, coverage, missing = [], {}, []
    for num, name in sorted(channels.items(), key=lambda kv: int(kv[0])):
        base = re.sub(r"\s*-?\s*\(?גיבוי[^)]*\)?\s*-?\s*|^\(גיבוי\)\s*-\s*", "", name).strip()
        cands, seen = [], set()

        def add(sname, cid):
            if cid and (sname, cid) not in seen and src.get(sname, ({}, {}))[1].get(cid):
                seen.add((sname, cid))
                cands.append((sname, cid, src[sname][1][cid]))

        # 1. explicit aliases, in the order given
        for sname, dn in aliases.get(name) or aliases.get(base) or []:
            chans = src.get(sname, ({}, {}))[0]
            if sname == "x":
                add("x", dn)
            else:
                add(sname, next((cid for cid, d in chans.items() if d == dn), None))
        # 2. automatic name matches, by source priority
        n = norm(name)
        for sname in SOURCE_PRIORITY:
            pool = pools[sname]
            if sname == "prev":
                add("prev", name if name in src["prev"][1] else None)
                continue
            if n in pool:
                add(sname, pool[n])
            else:
                m = difflib.get_close_matches(n, pool.keys(), n=1, cutoff=0.88)
                if m:
                    add(sname, pool[m[0]])

        merged, used = merge(cands)
        if not merged:
            missing.append(name)
            continue
        cid = f"tm{num}"
        ch = ET.SubElement(out, "channel", {"id": cid})
        ET.SubElement(ch, "display-name").text = name
        for p in merged:
            p.set("channel", cid)
            progs_out.append(p)
        coverage[name] = {
            "sources": used,
            "ids": {s: c for s, c, _ in cands if s in used},
            "from": merged[0].get("start")[:12],
            "until": merged[-1].get("stop")[:12],
        }
    for p in progs_out:
        out.append(p)
    ET.indent(out)
    ET.ElementTree(out).write(HERE / "epg.xml", encoding="utf-8", xml_declaration=True)
    with open(HERE / "epg.xml.gz", "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as g:
            g.write((HERE / "epg.xml").read_bytes())

    per_day, per_src = {}, {}
    for p in progs_out:
        d = p.get("start")[:8]
        per_day[d] = per_day.get(d, 0) + 1
    for c in coverage.values():
        for s, k in c["sources"].items():
            per_src[s] = per_src.get(s, 0) + k
    short = sorted(n for n, c in coverage.items() if parse_xmltv_time(c["until"] + "00 +0000") < NOW + timedelta(days=2))
    summary = {
        "generated": NOW.strftime("%Y-%m-%d %H:%M UTC"),
        "channels_total": len(channels), "matched": len(coverage), "missing": len(missing),
        "programmes": len(progs_out), "per_source": per_src, "per_day": dict(sorted(per_day.items())),
        "sources_fetched": {k: len(v[0]) for k, v in src.items()},
        "short_horizon_channels": short,
    }
    (HERE / "coverage.json").write_text(
        json.dumps({"summary": summary, "matched": coverage, "missing": missing}, ensure_ascii=False, indent=1), "utf8")
    print(f"matched {len(coverage)}/{len(channels)}, programmes {len(progs_out)}, per source {per_src}")
    if len(coverage) < len(channels) * 0.5:
        sys.exit("too few channels matched; refusing to publish")


if __name__ == "__main__":
    main()
