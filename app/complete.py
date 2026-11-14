"""Vollständigkeit von Serien: Welche bereits ausgestrahlten Folgen fehlen in der Bibliothek?

Die Soll-Liste kommt von TMDB (bzw. TVDB, wenn TVDB die Episoden-Quelle ist) und wird pro Serie zwischengespeichert:
laufende Serien werden nach 7 Tagen neu geladen, beendete nach 60. Nach jedem Scan wird nur lokal neu verglichen.
Specials (Staffel 0) und noch nicht ausgestrahlte Folgen zählen nicht. Serien mit Anime-Sonderzuordnung
(Ordner = einzelne Staffel mit Versatz) werden nicht geprüft – dort passt die Zählung nicht zur Soll-Liste.
"""
import json
from datetime import date, datetime, timedelta

from . import config, db, enrich, tmdb, tvdb

MAX_LIST = 400            # so viele fehlende Folgen werden einzeln gespeichert/angezeigt


def enabled():
    return bool(config.get("complete_check"))


def _tvdb_primary(show):
    hints = show.get("hints") or {}
    meta = show.get("meta") or {}
    return bool(meta.get("tvdb_id")) and tvdb.enabled() and (
        config.get("tv_episode_source") == "tvdb" or hints.get("numbering") == "tvdb")


def _expected(show):
    """-> ([[staffel, nr, datum, titel], …], Quelle) – alle regulären Folgen laut Quelle."""
    meta = show.get("meta") or {}
    if _tvdb_primary(show):
        eps = enrich.tvdb_episodes(meta, force_default=(show.get("hints") or {}).get("numbering") == "tvdb")
        if eps:
            return [[s, n, e.get("aired"), e.get("title") or ""] for (s, n), e in sorted(eps.items()) if s > 0], "TVDB"
    seasons = [s["season"] for s in meta.get("seasons") or [] if s.get("season")]
    eps = tmdb.aired_episodes(meta["tmdb_id"], seasons)
    return [[s, n, e["aired"], e["title"]] for (s, n), e in sorted(eps.items())], "TMDB"


def _stale(info, meta):
    if not info or not info.get("loaded") or "expected" not in info:
        return True
    ended = (meta.get("status") or "") in ("Ended", "Canceled")
    try:
        age = datetime.now() - datetime.fromisoformat(info["loaded"])
    except ValueError:
        return True
    return age > timedelta(days=60 if ended else 7)


def _compare(show_id, info):
    """Soll-Liste mit den vorhandenen Dateien vergleichen und das Ergebnis speichern. -> Anzahl fehlender Folgen"""
    today = date.today().isoformat()
    have = set()
    for e in db.query("SELECT season, episodes FROM episodes WHERE show_id=? AND season IS NOT NULL AND missing=0",
                      (show_id,)):
        for n in e["episodes"] or []:
            have.add((e["season"], n))
    aired = [x for x in info.get("expected", []) if x[2] and x[2] <= today]
    missing = [x for x in aired if (x[0], x[1]) not in have]
    seasons = {}
    for s, n, _, _ in aired:
        seasons.setdefault(str(s), {"total": 0, "missing": []})["total"] += 1
    for s, n, _, _ in missing:
        seasons[str(s)]["missing"].append(n)
    info.update(n=len(missing), total=len(aired), seasons=seasons, checked=db.now(),
                list=[{"season": s, "episode": n, "aired": d, "title": t} for s, n, d, t in missing[:MAX_LIST]])
    old = db.one("SELECT ep_missing FROM shows WHERE id=?", (show_id,))
    db.execute("UPDATE shows SET ep_missing=?, missing_eps=? WHERE id=?",
               (len(missing), json.dumps(info, ensure_ascii=False), show_id))
    return len(missing), (old or {}).get("ep_missing")


def check_show(show_id, force=False, local_only=False):
    """-> Anzahl fehlender Folgen oder None (nicht prüfbar / nicht gescrapt)."""
    show = db.one("SELECT id, title, status, meta, hints, mapping, missing_eps FROM shows WHERE id=?", (show_id,))
    if not show or show["status"] != "gescrapt" or not isinstance(show.get("meta"), dict):
        return None
    if show.get("mapping"):
        db.execute("UPDATE shows SET ep_missing=NULL, missing_eps=NULL WHERE id=?", (show_id,))
        return None
    info = show.get("missing_eps") if isinstance(show.get("missing_eps"), dict) else {}
    if not local_only and (force or _stale(info, show["meta"])):
        try:
            expected, src = _expected(show)
        except Exception as e:  # noqa – Prüfung darf Scrape/Scan nie scheitern lassen
            db.log("WARN", f"Vollständigkeit {show['title']}: {type(e).__name__}: {e}")
            return None
        info = {"expected": expected, "source": src, "loaded": db.now()}
    if "expected" not in info:
        return None
    n, before = _compare(show_id, info)
    if n and n != before:
        db.log("INFO", f"Unvollständig: {show['title']} – {n} von {info['total']} ausgestrahlten Folgen fehlen")
    return n


def check_all(ids=None, force=False, local_only=False, limit=None, progress=None, step=None):
    """Alle (oder ausgewählte) gescrapten Serien prüfen. limit begrenzt die Online-Abfragen pro Lauf."""
    if not enabled():
        return 0
    rows = db.query("SELECT id, title, meta, missing_eps FROM shows WHERE status='gescrapt' AND missing=0 "
                    "AND mapping IS NULL")
    if ids:
        rows = [r for r in rows if r["id"] in set(ids)]
    online = 0
    incomplete = 0
    for r in rows:
        info = r.get("missing_eps") if isinstance(r.get("missing_eps"), dict) else {}
        need_online = not local_only and (force or _stale(info, r.get("meta") or {}))
        if need_online and limit is not None and online >= limit:
            need_online = False
        if need_online:
            online += 1
            if progress:
                progress(f"Vollständigkeit: {r['title']}")
        n = check_show(r["id"], force=force and need_online, local_only=not need_online)
        incomplete += bool(n)
        if step:
            step()
    return incomplete
