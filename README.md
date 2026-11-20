# il-epg

XMLTV guide (לוח שידורים) for an Israeli IPTV channel list, rebuilt every 4 hours by GitHub Actions.

Guide URL (use this in TiviMate / the Xiaomi box):

    https://raw.githubusercontent.com/amitu20/il-epg/main/epg.xml.gz

## Sources (merged per channel, in this priority)

| source | what | horizon |
|---|---|---|
| `freetv` | web.freetv.tv API (Hebrew, with descriptions and images) | ~7 days |
| `yes` | svc.yes.co.il API (Hebrew) | ~7 days |
| `il1` | epgshare01 IL1 guide | ~1.5 days |
| `x` | iptvx.one (HOT cinema etc.) | ~2 days |
| `prev` | the previously published `epg.xml` | fallback only |

For every channel the highest-priority source wins; the others only fill gaps and extend the horizon.
If a source is down, the previous guide keeps the channel filled until the next successful run.

## Files

- `channels.json` – channel number → TiviMate channel name (output display-names match these).
- `aliases.json` – channel name → list of `source:name` candidates, in priority order. Names without an alias are matched automatically by normalized name.
- `coverage.json` – build report: `summary` (per source / per day counts, channels with a short horizon), `matched` (sources used per channel), `missing`.
- `build_epg.py` – the build; needs only Python 3 and curl. Run it locally with `python3 build_epg.py`.

The workflow can also be started by hand from the Actions tab (`update-epg` → Run workflow).
