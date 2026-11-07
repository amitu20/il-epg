#!/usr/bin/env python3
"""Build a merged XMLTV guide for the channels listed in channels.json.

Sources: epgshare01 IL1 guide (Hebrew names) + yes.co.il (via iptv-org/epg).
Channel display-names in the output equal the TiviMate channel names so
TiviMate can match them by name.
"""
import copy, difflib, gzip, json, re, subprocess, sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).parent
IPTVX_URL = "https://iptvx.one/EPG"
IL1_URL = "https://epgshare01.online/epgshare01/epg_ripper_IL1.xml.gz"
YES_XML = HERE / "yes.xml"
ALIASES = {norm_k: v for norm_k, v in json.loads((HERE / "aliases.json").read_text("utf8")).items()} if (HERE / "aliases.json").exists() else {}


def norm(s):
    s = s.lower().replace("’", "'")
    s = re.sub(r"\((גיבוי|backup)[^)]*\)|-?\s*\(?גיבוי\s*\d*\)?|גיבוי", " ", s)
    s = re.sub(r"\b(hd|sd|4k|live|שידור חי|מונגש|ערוץ|channel|tv)\b", " ", s)
    s = re.sub(r"[^\w\u0590-\u05ff+]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load(path_or_bytes):
    root = ET.fromstring(path_or_bytes)
    chans = {c.get("id"): (c.findtext("display-name") or "") for c in root.findall("channel")}
    progs = {}
    for p in root.findall("programme"):
        progs.setdefault(p.get("channel"), []).append(p)
    return chans, progs


def load_iptvx(ids):
    """Stream the large iptvx.one guide and keep only the wanted channel ids."""
    cmd = f'curl -sfL -m 300 {IPTVX_URL} | gunzip'
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
    chans, progs = {}, {}
    for _, el in ET.iterparse(proc.stdout, events=("end",)):
        if el.tag == "channel" and el.get("id") in ids:
            chans[el.get("id")] = el.findtext("display-name") or ""
        elif el.tag == "programme":
            if el.get("channel") in ids:
                progs.setdefault(el.get("channel"), []).append(el)
                continue
        if el.tag in ("channel", "programme"):
            el.clear()
    return chans, progs


def main():
    channels = json.loads((HERE / "channels.json").read_text("utf8"))
    il1_raw = gzip.decompress(subprocess.run(["curl", "-sfL", "-m", "120", IL1_URL], capture_output=True, check=True).stdout)
    src = {"il1": load(il1_raw)}
    if YES_XML.exists():
        src["yes"] = load(YES_XML.read_bytes())

    xids = {v.split(":", 1)[1] for v in ALIASES.values() if v.startswith("x:")}
    if xids:
        src["x"] = load_iptvx(xids)

    pool = {}  # normalized name -> (source, id)
    for sname, (chans, progs) in src.items():
        for cid, name in chans.items():
            if len(progs.get(cid, [])) > 3:
                pool.setdefault(norm(name), (sname, cid))

    out = ET.Element("tv", {"generator-info-name": "build_epg.py"})
    progs_out = []
    matched, missing = {}, []
    for num, name in sorted(channels.items(), key=lambda kv: int(kv[0])):
        target = None
        base = re.sub(r"\s*-?\s*\(?גיבוי[^)]*\)?\s*-?\s*|^\(גיבוי\)\s*-\s*", "", name).strip()
        alias = ALIASES.get(name) or ALIASES.get(base)
        if alias:
            sname, _, dn = alias.partition(":")
            if sname == "x":
                target = ("x", dn) if src["x"][1].get(dn) else None
            else:
                target = next(((sname, cid) for cid, d in src.get(sname, ({}, {}))[0].items() if d == dn and src[sname][1].get(cid)), None)
        else:
            n = norm(name)
            if n in pool:
                target = pool[n]
            else:
                m = difflib.get_close_matches(n, pool.keys(), n=1, cutoff=0.88)
                if m:
                    target = pool[m[0]]
        if not target:
            missing.append(name)
            continue
        matched[name] = target
        cid = f"tm{num}"
        ch = ET.SubElement(out, "channel", {"id": cid})
        ET.SubElement(ch, "display-name").text = name
        for p in src[target[0]][1].get(target[1], []):
            q = copy.deepcopy(p)
            q.set("channel", cid)
            progs_out.append(q)
    for p in progs_out:
        out.append(p)
    ET.indent(out)
    ET.ElementTree(out).write(HERE / "epg.xml", encoding="utf-8", xml_declaration=True)
    with gzip.open(HERE / "epg.xml.gz", "wb") as f:
        f.write((HERE / "epg.xml").read_bytes())
    (HERE / "coverage.json").write_text(
        json.dumps({"matched": {k: list(v) for k, v in matched.items()}, "missing": missing}, ensure_ascii=False, indent=1), "utf8")
    print(f"matched {len(matched)}/{len(channels)}, programmes {len(progs_out)}")


if __name__ == "__main__":
    main()
