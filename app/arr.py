"""Radarr / Sonarr: IDs und Episodennummern abgleichen, Webhooks verarbeiten."""
import os

from . import config, db, pathmap
from .http import SourceError, request


def enabled(system):
    return bool(config.get(f"{system}_url") and config.get(f"{system}_api_key"))


def _get(system, path, **params):
    url = config.get(f"{system}_url").rstrip("/") + "/api/v3" + path
    r = request("GET", url, params=params, headers={"X-Api-Key": config.get(f"{system}_api_key")},
                min_interval=0.05)
    if r.status_code in (401, 403):
        raise SourceError(f"{system.capitalize()}: API-Key ungültig")
    if r.status_code != 200:
        raise SourceError(f"{system.capitalize()}: HTTP {r.status_code} bei {path}")
    return r.json()


def _send(system, method, path, body=None, **params):
    url = config.get(f"{system}_url").rstrip("/") + "/api/v3" + path
    r = request(method, url, params=params, json=body, headers={"X-Api-Key": config.get(f"{system}_api_key")},
                min_interval=0.05, retries=2)
    if r.status_code in (401, 403):
        raise SourceError(f"{system.capitalize()}: API-Key ungültig")
    if r.status_code >= 400:
        try:
            detail = r.json()
            detail = "; ".join(x.get("errorMessage", "") for x in detail) if isinstance(detail, list) \
                else detail.get("message", "")
        except ValueError:
            detail = ""
        raise SourceError(f"{system.capitalize()}: HTTP {r.status_code} bei {path}" + (f" – {detail}" if detail else ""))
    try:
        return r.json()
    except ValueError:
        return None


def test(system):
    if not enabled(system):
        return {"ok": False, "msg": "URL oder API-Key fehlt"}
    try:
        s = _get(system, "/system/status")
        return {"ok": True, "msg": f"Verbunden: {s.get('appName', system)} {s.get('version', '')}"}
    except SourceError as e:
        return {"ok": False, "msg": str(e)}


# ---------------------------------------------------------------- Radarr

def sync_radarr():
    """Abgleich Filme. Offene Titel bekommen (optional) automatisch Radarrs TMDB-ID.
    -> (Bericht, IDs der Filme, die neu gescrapt werden sollen)"""
    movies = _get("radarr", "/movie")
    ours = {os.path.normpath(r["path"]): r for r in db.query("SELECT id, path, title, tmdb_id, status FROM movies")}
    issues, rescrape, seen, assigned = [], [], set(), 0
    with_file = [m for m in movies if m.get("hasFile") and (m.get("movieFile") or {}).get("path")]
    for m in with_file:
        local = os.path.normpath(pathmap.to_local("radarr", m["movieFile"]["path"]))
        r = ours.get(local)
        base = {"kind": "movie", "arr_title": f"{m.get('title')} ({m.get('year')})", "path": local,
                "tmdb_id": m.get("tmdbId")}
        if not r:
            issues.append({**base, "type": "fehlt",
                           "text": "Datei nicht in Scraparr – Pfadzuordnung prüfen oder scannen"})
            continue
        seen.add(local)
        base.update(id=r["id"], title=r["title"])
        tid = m.get("tmdbId")
        if not tid:
            continue
        if r["status"] != "gescrapt" or not r["tmdb_id"]:
            if config.get("arr_auto_ids"):
                db.update("movies", r["id"], tmdb_id=tid, imdb_id=m.get("imdbId") or None, status="neu",
                          message="TMDB-ID von Radarr übernommen")
                rescrape.append(r["id"])
                assigned += 1
            else:
                issues.append({**base, "type": "offen", "text": f"Radarr kennt TMDB {tid}", "action": "use_id"})
        elif int(r["tmdb_id"]) != int(tid):
            issues.append({**base, "type": "abweichung",
                           "text": f"Scraparr: TMDB {r['tmdb_id']} · Radarr: TMDB {tid}", "action": "use_id"})
    only_ours = [r for p, r in ours.items() if p not in seen]
    summary = {"radarr_filme": len(with_file), "ids_uebernommen": assigned,
               "abweichungen": sum(i["type"] == "abweichung" for i in issues),
               "fehlt_im_scraper": sum(i["type"] == "fehlt" for i in issues),
               "nur_im_scraper": len(only_ours)}
    return {"summary": summary, "issues": issues}, rescrape


# ---------------------------------------------------------------- Sonarr

def _sonarr_episode_map(series_id):
    """lokaler Dateipfad -> (staffel, [episoden]) laut Sonarr"""
    files = {f["id"]: os.path.normpath(pathmap.to_local("sonarr", f["path"]))
             for f in _get("sonarr", "/episodefile", seriesId=series_id) if f.get("path")}
    out = {}
    for e in _get("sonarr", "/episode", seriesId=series_id):
        fid = e.get("episodeFileId")
        if fid and fid in files:
            s, eps = out.setdefault(files[fid], (e["seasonNumber"], []))
            eps.append(e["episodeNumber"])
    return {p: (s, sorted(eps)) for p, (s, eps) in out.items()}


def apply_episode_numbers(show_id, series_id, only_unrecognized=True):
    """Sonarrs Nummerierung für (nicht erkannte) Episoden übernehmen. -> Anzahl, Abweichungen"""
    emap = _sonarr_episode_map(series_id)
    fixed, diffs = 0, []
    for e in db.query("SELECT id, path, season, episodes, status FROM episodes WHERE show_id=?", (show_id,)):
        s = emap.get(os.path.normpath(e["path"]))
        if not s:
            continue
        season, eps = s
        same = e["season"] == season and (e["episodes"] or []) == eps
        if same:
            continue
        unrecognized = e["season"] is None or e["status"] == "fehler"
        if unrecognized or not only_unrecognized:
            db.update("episodes", e["id"], season=season, episodes=eps, status="neu",
                      message="Nummer von Sonarr übernommen")
            fixed += 1
        else:
            diffs.append(f"{os.path.basename(e['path'])}: hier S{e['season']:02d}E{(e['episodes'] or [0])[0]:02d}, "
                         f"Sonarr S{season:02d}E{eps[0]:02d}")
    return fixed, diffs


def sync_sonarr():
    series = _get("sonarr", "/series")
    ours = {os.path.normpath(r["folder"]): r for r in db.query(
        "SELECT id, folder, title, tmdb_id, tvdb_id, status, hints FROM shows")}
    issues, rescrape, seen, assigned, ep_fixed = [], [], set(), 0, 0
    for s in series:
        local = os.path.normpath(pathmap.to_local("sonarr", s.get("path") or ""))
        r = ours.get(local)
        base = {"kind": "show", "arr_title": f"{s.get('title')} ({s.get('year')})", "path": local,
                "tmdb_id": s.get("tmdbId") or None, "tvdb_id": s.get("tvdbId") or None}
        if not r:
            if (s.get("statistics") or {}).get("episodeFileCount", 1):
                issues.append({**base, "type": "fehlt", "text": "Ordner nicht in Scraparr – Pfadzuordnung prüfen"})
            continue
        seen.add(local)
        base.update(id=r["id"], title=r["title"])
        hints = dict(r.get("hints") or {})
        changed_hints = False
        if s.get("tvdbId") and hints.get("tvdb_id") != s["tvdbId"]:
            hints["tvdb_id"] = s["tvdbId"]
            changed_hints = True
        if s.get("seriesType") == "anime" and hints.get("numbering") != "tvdb":
            hints["numbering"] = "tvdb"          # Sonarr zählt Anime nach TVDB
            changed_hints = True
        if changed_hints:
            db.update("shows", r["id"], hints=hints)
        tid = s.get("tmdbId") or None
        if r["status"] != "gescrapt" or not r["tmdb_id"]:
            if config.get("arr_auto_ids") and (tid or s.get("tvdbId")):
                db.update("shows", r["id"], tmdb_id=tid, status="neu",
                          message="ID von Sonarr übernommen")
                rescrape.append(r["id"])
                assigned += 1
            elif tid:
                issues.append({**base, "type": "offen", "text": f"Sonarr kennt TMDB {tid}", "action": "use_id"})
        elif tid and int(r["tmdb_id"]) != int(tid):
            issues.append({**base, "type": "abweichung",
                           "text": f"Scraparr: TMDB {r['tmdb_id']} · Sonarr: TMDB {tid}", "action": "use_id"})
        elif not tid and s.get("tvdbId") and r["tvdb_id"] and int(r["tvdb_id"]) != int(s["tvdbId"]):
            issues.append({**base, "type": "abweichung",
                           "text": f"Scraparr: TVDB {r['tvdb_id']} · Sonarr: TVDB {s['tvdbId']}", "action": "use_id"})
        # Episoden: nur bei Serien mit Problemen nachfragen (spart Anfragen)
        bad = db.one("SELECT COUNT(*) n FROM episodes WHERE show_id=? AND (season IS NULL OR status='fehler')",
                     (r["id"],))["n"]
        if bad and config.get("sonarr_episode_numbers"):
            fixed, diffs = apply_episode_numbers(r["id"], s["id"])
            ep_fixed += fixed
            if fixed and r["id"] not in rescrape:
                db.update("shows", r["id"], status="neu")
                rescrape.append(r["id"])
            for d in diffs[:20]:
                issues.append({**base, "type": "episode", "text": d, "series_id": s["id"], "action": "use_episodes"})
    only_ours = [r for p, r in ours.items() if p not in seen]
    summary = {"sonarr_serien": len(series), "ids_uebernommen": assigned, "episoden_korrigiert": ep_fixed,
               "abweichungen": sum(i["type"] in ("abweichung", "episode") for i in issues),
               "fehlt_im_scraper": sum(i["type"] == "fehlt" for i in issues),
               "nur_im_scraper": len(only_ours)}
    return {"summary": summary, "issues": issues}, rescrape


# ---------------------------------------------------------------- Fehlendes an Radarr/Sonarr übergeben

def _profile(system):
    """Qualitätsprofil: Einstellung (Name oder ID), sonst das erste."""
    profiles = _get(system, "/qualityprofile")
    if not profiles:
        raise SourceError(f"{system.capitalize()}: kein Qualitätsprofil vorhanden")
    want = (config.get(f"{system}_quality_profile") or "").strip().lower()
    for p in profiles:
        if want and (str(p["id"]) == want or (p.get("name") or "").lower() == want):
            return p["id"]
    if want:
        raise SourceError(f"{system.capitalize()}: Qualitätsprofil „{want}“ nicht gefunden "
                          f"(vorhanden: {', '.join(p.get('name', '?') for p in profiles)})")
    return profiles[0]["id"]


def _roots(system):
    roots = [r["path"].rstrip("/\\") for r in _get(system, "/rootfolder") if r.get("path")]
    if not roots:
        raise SourceError(f"{system.capitalize()}: kein Root-Ordner eingerichtet")
    return roots


def _root(system, roots=None):
    roots = roots or _roots(system)
    want = (config.get(f"{system}_root_folder") or "").strip().rstrip("/\\")
    if want:
        if want not in roots:
            raise SourceError(f"{system.capitalize()}: Root-Ordner „{want}“ nicht gefunden (vorhanden: {', '.join(roots)})")
        return want
    return roots[0]


def options(system):
    """Root-Ordner und Qualitätsprofile für die Einstellungen."""
    return {"roots": _roots(system), "profiles": [p.get("name") for p in _get(system, "/qualityprofile")]}


def request_movies(tmdb_ids):
    """Fehlende Filme an Radarr übergeben: vorhandene überwachen (+ suchen), neue anlegen.
    -> {"added": [...], "searched": [...], "present": [...], "failed": [...]} (Titel)"""
    if not enabled("radarr"):
        raise SourceError("Radarr ist nicht eingerichtet (Einstellungen → Radarr & Sonarr)")
    search = bool(config.get("arr_search_on_request"))
    known = {m.get("tmdbId"): m for m in _get("radarr", "/movie")}
    res = {"added": [], "searched": [], "present": [], "failed": [], "search": search}
    profile = root = None
    search_ids = []
    for tid in dict.fromkeys(int(t) for t in tmdb_ids):
        m = known.get(tid)
        try:
            if m:
                title = m.get("title") or str(tid)
                if m.get("hasFile"):
                    res["present"].append(title)
                    continue
                if not m.get("monitored"):
                    _send("radarr", "PUT", f"/movie/{m['id']}", {**m, "monitored": True})
                search_ids.append(m["id"])
                res["searched"].append(title)
                continue
            look = _get("radarr", "/movie/lookup/tmdb", tmdbId=tid)
            look = look[0] if isinstance(look, list) and look else look
            if not look or not look.get("tmdbId"):
                res["failed"].append(f"TMDB {tid}: Radarr findet den Film nicht")
                continue
            profile = profile or _profile("radarr")
            root = root or _root("radarr")
            _send("radarr", "POST", "/movie", {**look, "qualityProfileId": profile, "rootFolderPath": root,
                                               "monitored": True, "minimumAvailability": "released",
                                               "addOptions": {"searchForMovie": search}})
            res["added"].append(look.get("title") or str(tid))
        except SourceError as e:
            res["failed"].append(str(e))
    if search and search_ids:
        _send("radarr", "POST", "/command", {"name": "MoviesSearch", "movieIds": search_ids})
    db.log("INFO", f"Radarr-Übergabe: {len(res['added'])} neu angelegt, {len(res['searched'])} zur Suche, "
                   f"{len(res['present'])} schon vorhanden, {len(res['failed'])} fehlgeschlagen")
    return res


def _find_series(show):
    local = os.path.normpath(show["folder"])
    for s in _get("sonarr", "/series"):
        if show.get("tvdb_id") and s.get("tvdbId") == int(show["tvdb_id"]):
            return s
        if show.get("tmdb_id") and s.get("tmdbId") == int(show["tmdb_id"]):
            return s
        if s.get("path") and os.path.normpath(pathmap.to_local("sonarr", s["path"])) == local:
            return s
    return None


def request_episodes(show_id):
    """Fehlende Folgen einer Serie an Sonarr übergeben. Kennt Sonarr die Serie, werden genau diese Folgen
    überwacht (+ gesucht); sonst wird die Serie mit „fehlende Folgen überwachen“ angelegt."""
    if not enabled("sonarr"):
        raise SourceError("Sonarr ist nicht eingerichtet (Einstellungen → Radarr & Sonarr)")
    show = db.one("SELECT id, folder, title, tmdb_id, tvdb_id, missing_eps FROM shows WHERE id=?", (show_id,))
    info = (show or {}).get("missing_eps")
    if not show or not isinstance(info, dict) or not info.get("n"):
        raise SourceError("Für diese Serie sind keine fehlenden Folgen bekannt")
    search = bool(config.get("arr_search_on_request"))
    wanted = {(int(s), n) for s, v in info.get("seasons", {}).items() for n in v.get("missing", [])}
    series = _find_series(show)
    if not series:
        term = f"tvdb:{show['tvdb_id']}" if show.get("tvdb_id") else f"tmdb:{show['tmdb_id']}"
        look = _get("sonarr", "/series/lookup", term=term)
        if not look:
            raise SourceError(f"Sonarr findet die Serie nicht ({term})")
        roots = _roots("sonarr")
        remote = (pathmap.to_remote("sonarr", show["folder"]) or "").rstrip("/\\")
        body = {**look[0], "qualityProfileId": _profile("sonarr"), "monitored": True, "seasonFolder": True,
                "addOptions": {"monitor": "missing", "searchForMissingEpisodes": search,
                               "searchForCutoffUnmetEpisodes": False}}
        if any(remote.startswith(r + "/") or remote.startswith(r + "\\") for r in roots):
            body["path"] = remote                      # vorhandenen Serienordner weiterverwenden
        else:
            body["rootFolderPath"] = _root("sonarr", roots)
        _send("sonarr", "POST", "/series", body)
        db.log("INFO", f"Sonarr-Übergabe: „{show['title']}“ neu angelegt, {len(wanted)} fehlende Folgen überwacht")
        return {"added": True, "episodes": len(wanted), "not_found": 0, "searched": search}
    eps = _get("sonarr", "/episode", seriesId=series["id"])
    ids = [e["id"] for e in eps if (e.get("seasonNumber"), e.get("episodeNumber")) in wanted and not e.get("hasFile")]
    known = {(e.get("seasonNumber"), e.get("episodeNumber")) for e in eps}
    not_found = len([w for w in wanted if w not in known])
    if not ids:
        raise SourceError("Sonarr führt keine der fehlenden Folgen als offen "
                          "(andere Zählung oder Dateien dort schon vorhanden)")
    seasons = {s for s, _ in wanted}
    changed = not series.get("monitored")
    for se in series.get("seasons", []):
        if se.get("seasonNumber") in seasons and not se.get("monitored"):
            se["monitored"] = True
            changed = True
    if changed:
        _send("sonarr", "PUT", f"/series/{series['id']}", {**series, "monitored": True})
    _send("sonarr", "PUT", "/episode/monitor", {"episodeIds": ids, "monitored": True})
    if search:
        _send("sonarr", "POST", "/command", {"name": "EpisodeSearch", "episodeIds": ids})
    db.log("INFO", f"Sonarr-Übergabe: „{show['title']}“ – {len(ids)} Folgen überwacht"
                   + (", Suche gestartet" if search else "") + (f", {not_found} bei Sonarr unbekannt" if not_found else ""))
    return {"added": False, "episodes": len(ids), "not_found": not_found, "searched": search}


# ---------------------------------------------------------------- Aktionen aus dem Bericht

def apply(system, issues):
    """-> IDs (Filme bzw. Serien), die neu gescrapt werden müssen"""
    todo = []
    for i in issues:
        if not i.get("id"):
            continue
        if i.get("action") == "use_id":
            if i["kind"] == "movie":
                db.update("movies", i["id"], tmdb_id=i["tmdb_id"], status="neu", message="ID von Radarr übernommen")
            else:
                fields = {"status": "neu", "message": "ID von Sonarr übernommen"}
                if i.get("tmdb_id"):
                    fields["tmdb_id"] = i["tmdb_id"]
                else:
                    fields["tmdb_id"] = None
                    show = db.one("SELECT hints FROM shows WHERE id=?", (i["id"],))
                    fields["hints"] = {**(show.get("hints") or {}), "tvdb_id": i["tvdb_id"]}
                db.update("shows", i["id"], **fields)
            todo.append(i["id"])
        elif i.get("action") == "use_episodes" and i.get("series_id"):
            apply_episode_numbers(i["id"], i["series_id"], only_unrecognized=False)
            db.update("shows", i["id"], status="neu")
            todo.append(i["id"])
    return list(dict.fromkeys(todo))


# ---------------------------------------------------------------- Webhooks

def parse_webhook(system, payload):
    """Radarr-/Sonarr-Webhook -> Liste von Aufgaben für die Warteschlange."""
    ev = payload.get("eventType")
    tasks = []
    if system == "radarr":
        movie = payload.get("movie") or {}
        if ev == "Download" and (payload.get("movieFile") or {}).get("path"):
            new = pathmap.to_local("radarr", payload["movieFile"]["path"])
            for old in payload.get("deletedFiles") or []:       # Upgrade: Begleitdateien umhängen
                if old.get("path"):
                    tasks.append({"type": "rename", "old": pathmap.to_local("radarr", old["path"]), "new": new})
            tasks.append({"type": "movie", "path": new, "tmdb_id": movie.get("tmdbId") or None,
                          "imdb_id": movie.get("imdbId") or None})
        elif ev == "Rename":
            for f in payload.get("renamedMovieFiles") or []:
                tasks.append({"type": "rename", "old": pathmap.to_local("radarr", f.get("previousPath")),
                              "new": pathmap.to_local("radarr", f.get("path"))})
    else:
        series = payload.get("series") or {}
        folder = pathmap.to_local("sonarr", series.get("path")) if series.get("path") else None
        if ev == "Download" and (payload.get("episodeFile") or {}).get("path"):
            new = pathmap.to_local("sonarr", payload["episodeFile"]["path"])
            for old in payload.get("deletedFiles") or []:
                if old.get("path"):
                    tasks.append({"type": "rename", "old": pathmap.to_local("sonarr", old["path"]), "new": new})
            eps = payload.get("episodes") or []
            tasks.append({"type": "episode", "path": new, "folder": folder,
                          "season": eps[0]["seasonNumber"] if eps else None,
                          "episodes": sorted(e["episodeNumber"] for e in eps),
                          "tmdb_id": series.get("tmdbId") or None, "tvdb_id": series.get("tvdbId") or None,
                          "anime": series.get("type") == "anime"})
        elif ev == "Rename":
            for f in payload.get("renamedEpisodeFiles") or []:
                tasks.append({"type": "rename", "old": pathmap.to_local("sonarr", f.get("previousPath")),
                              "new": pathmap.to_local("sonarr", f.get("path"))})
    return ev, tasks
