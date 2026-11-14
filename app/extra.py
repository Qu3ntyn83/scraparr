"""Zusatzquellen ohne API-Key für Serien, Dokumentationen und Anime.

  TVmaze            Episodenlisten (Titel, Inhalt, Datum, Bild) – englisch; füllt Folgen, die TMDB/TVDB gar nicht führen
  Kitsu             Anime-Episoden (Titel, Inhalt, Bild) über die Kitsu-ID aus der Anime-Zuordnungsliste – englisch
  Wikipedia (de)    deutsche Einleitung des Artikels als Beschreibung, wenn es sonst nur englische Texte gibt
                    (Artikel wird über den Wikidata-Eintrag gefunden – keine Titelsuche, keine Verwechslung)

Alle drei sind reine Lückenfüller: Sie werden nur gefragt, wenn die Hauptquellen nichts (Deutsches) liefern.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import config, db, wikidata
from .http import UA, SourceError, _throttle, request

TVMAZE = "https://api.tvmaze.com"
KITSU = "https://kitsu.io/api/edge"
DEWIKI = "https://de.wikipedia.org/w/api.php"


def _strip_html(s):
    s = re.sub(r"<br\s*/?>|</p>\s*<p>", "\n", s or "")
    return re.sub(r"[ \t]+", " ", re.sub(r"<[^>]+>", "", s)).strip()


# ---------------------------------------------------------------- TVmaze

def tvmaze_enabled():
    return bool(config.get("tvmaze_enabled"))


def tvmaze_show(tvdb_id=None, imdb_id=None):
    """-> TVmaze-ID über TVDB- oder IMDb-ID, sonst None."""
    for key, val in (("thetvdb", tvdb_id), ("imdb", imdb_id)):
        if not val:
            continue
        r = request("GET", f"{TVMAZE}/lookup/shows", params={key: val}, min_interval=0.6, ok404=True)
        if r is not None and r.status_code == 200:
            try:
                return (r.json() or {}).get("id")
            except ValueError:
                pass
    return None


def tvmaze_episodes(show_id):
    """{(staffel, nr): {...}} – nur reguläre Folgen mit Nummer."""
    r = request("GET", f"{TVMAZE}/shows/{int(show_id)}/episodes", min_interval=0.6, ok404=True)
    if r is None or r.status_code != 200:
        return {}
    out = {}
    for e in r.json() or []:
        if e.get("season") is None or e.get("number") is None:
            continue
        out[(int(e["season"]), int(e["number"]))] = {
            "title": e.get("name") or "", "plot": _strip_html(e.get("summary")), "aired": e.get("airdate") or None,
            "runtime": e.get("runtime"), "still_url": (e.get("image") or {}).get("original"),
        }
    return out


# ---------------------------------------------------------------- Kitsu

def kitsu_enabled():
    return bool(config.get("kitsu_enabled"))


def kitsu_episodes(kitsu_id):
    """{nr: {...}} – Zählung innerhalb des Anime-Eintrags (ab 1)."""
    out, offset = {}, 0
    while offset < 2000:
        r = request("GET", f"{KITSU}/anime/{int(kitsu_id)}/episodes",
                    params={"page[limit]": 20, "page[offset]": offset},
                    headers={"Accept": "application/vnd.api+json"}, min_interval=0.5, ok404=True)
        if r is None or r.status_code != 200:
            break
        d = r.json() or {}
        rows = d.get("data") or []
        for row in rows:
            a = row.get("attributes") or {}
            n = a.get("relativeNumber") or a.get("number")
            if n is None:
                continue
            t = a.get("titles") or {}
            out[int(n)] = {
                "title": t.get("en_us") or t.get("en") or a.get("canonicalTitle") or t.get("en_jp") or "",
                "plot": (a.get("synopsis") or a.get("description") or "").strip(),
                "aired": a.get("airdate"), "runtime": a.get("length"),
                "still_url": (a.get("thumbnail") or {}).get("original"),
            }
        if len(rows) < 20 or not (d.get("links") or {}).get("next"):
            break
        offset += 20
    return out


# ---------------------------------------------------------------- Wikipedia (de)

def wikipedia_enabled():
    return bool(config.get("wikipedia_enabled")) and wikidata.enabled()


def _wiki_get(params):
    # Wikimedia blockt httpx-Clients (403) – urllib wird akzeptiert
    url = DEWIKI + "?" + urllib.parse.urlencode({**params, "format": "json"})
    _throttle("de.wikipedia.org", 1.0)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=25) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SourceError(f"Wikipedia: HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise SourceError(f"Wikipedia: {e}")


def wikipedia_plot(qid):
    """Deutsche Artikeleinleitung zum Wikidata-Eintrag – None, wenn es keinen deutschen Artikel gibt."""
    if not qid:
        return None
    try:
        title = wikidata.sitelink(qid, "dewiki")
        if not title:
            return None
        d = _wiki_get({"action": "query", "prop": "extracts|pageprops", "exintro": 1, "explaintext": 1,
                       "redirects": 1, "titles": title})
    except SourceError as e:
        if "pausiert" not in str(e):
            db.log("WARN", str(e))
        return None
    for page in ((d.get("query") or {}).get("pages") or {}).values():
        if "disambiguation" in (page.get("pageprops") or {}):
            continue
        text = re.sub(r"\[\d+\]", "", (page.get("extract") or "")).strip()
        text = re.sub(r"\n{2,}", "\n", text)
        if len(text) >= 80:
            return text[:2500]
    return None


def test():
    out = []
    try:
        sid = tvmaze_show(tvdb_id=81189) if tvmaze_enabled() else None
        if tvmaze_enabled():
            out.append(f"TVmaze {'OK' if sid else 'ohne Treffer'}")
        if kitsu_enabled():
            out.append(f"Kitsu {'OK' if kitsu_episodes(1) else 'ohne Treffer'}")
        if wikipedia_enabled():
            out.append(f"Wikipedia {'OK' if wikipedia_plot('Q1079') else 'ohne Treffer'}")
    except SourceError as e:
        return {"ok": False, "msg": str(e)}
    if not out:
        return {"ok": False, "msg": "Zusatzquellen sind deaktiviert"}
    return {"ok": all("OK" in o for o in out), "msg": " · ".join(out)}
