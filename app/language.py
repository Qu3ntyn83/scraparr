"""Sprachprüfung vorhandener NFOs: nicht deutsche Beschreibungen erkennen und zum Neu-Scrapen vormerken.

Erkennung ohne Zusatzpakete über häufige Funktionswörter (der/die/und … vs. the/and/of …). Geprüft wird die
Beschreibung (<plot>, sonst <outline>) – Titel nicht, weil Originaltitel oft absichtlich fremdsprachig sind.

Schutz vor Endlosschleifen: Stammt eine nicht deutsche NFO schon von Scraparr, gab es bei den Quellen keinen deutschen
Text. Dann wird erst nach `lang_recheck_days` erneut versucht (TMDB bekommt Übersetzungen oft später).
"""
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from . import config, db, paths

STOP = {
    "de": "der die das und ist nicht ein eine einen einem einer zu mit sich auf für von dem den des im sie er es wird "
          "werden als auch nach bei aus wie aber noch nur dass daß seine seiner ihre ihren sein sind hat haben wenn um "
          "über vor zum zur durch doch gegen jedoch muss soll will kann wieder schon immer zwischen während",
    "en": "the and is of to that it with for his her he she they was are on as by an be this from who their has have "
          "but when after into will which while what them him one about must can her when out up",
    "fr": "le la les et est un une des du dans qui que pour avec sur pas son sa ses il elle au aux ce cette leur mais",
    "es": "el la los las y es un una del que en con por para su sus se al lo como pero más cuando",
    "it": "il lo la gli le e è un una di che per con non del della dei nel sono suo sua come quando",
    "nl": "de het een en is van dat die in op met voor zijn niet hij zij aan ook maar naar wordt worden",
}
STOP = {k: set(v.split()) for k, v in STOP.items()}
WORD = re.compile(r"[a-zäöüßàâçéèêëîïôûùüÿñæœìíòóú]+", re.I)


def detect(text):
    """-> Sprachcode ("de", "en", …) oder None bei zu wenig Text."""
    words = [w.lower() for w in WORD.findall(text or "")]
    if len(words) < 6:
        return None
    score = {k: sum(w in s for w in words) for k, s in STOP.items()}
    score["de"] += 2 * len(re.findall(r"[äöüß]", text or "", re.I))
    best = max(score, key=score.get)
    if score[best] < 2:
        return None
    return best


def target():
    return (config.get("language") or "de-DE").split("-")[0].lower()


def nfo_info(path):
    """-> (Beschreibung, von_scraparr) aus einer NFO."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read(400_000)
    except OSError:
        return None, False
    ours = "erstellt von Scraparr" in raw[:400]
    try:
        root = ET.fromstring(re.sub(r"<\?xml.*?\?>|<!--.*?-->", "", raw, flags=re.S).strip())
        texts = [(root.findtext(t) or "").strip() for t in ("plot", "outline")]
    except ET.ParseError:
        texts = [m.group(1).strip() for m in re.finditer(r"<(?:plot|outline)>(.*?)</(?:plot|outline)>", raw, re.S)]
    text = next((t for t in texts if t), "")
    return text, ours


def _nfo_of(kind, r):
    if kind == "movies":
        return paths.movie_files(r["path"], bool(r["single_folder"]))["nfo"]
    if kind == "shows":
        return os.path.join(r["folder"], "tvshow.nfo")
    return paths.episode_files(r["path"])["nfo"]


def _mtime(p):
    try:
        return datetime.fromtimestamp(os.path.getmtime(p))
    except OSError:
        return None


def check_library(progress=None):
    """Alle NFOs prüfen, deren Inhalt sich seit der letzten Prüfung geändert hat. -> Anzahl vorgemerkter Einträge."""
    if not config.get("lang_check"):
        return 0
    want = target()
    days = max(1, int(config.get("lang_recheck_days") or 30))
    now = datetime.now()
    marked = 0
    queries = {
        "movies": "SELECT id, path, single_folder, status, lang, lang_checked, lang_retry FROM movies "
                  "WHERE has_nfo=1 AND missing=0 AND status IN ('gescrapt','neu')",
        "shows": "SELECT id, folder, status, lang, lang_checked, lang_retry FROM shows "
                 "WHERE has_nfo=1 AND missing=0 AND status IN ('gescrapt','neu')",
        "episodes": "SELECT id, show_id, path, status, lang, lang_checked, lang_retry FROM episodes "
                    "WHERE has_nfo=1 AND missing=0 AND season IS NOT NULL AND status IN ('gescrapt','neu')",
    }
    if progress:
        progress("Sprache der Beschreibungen wird geprüft …")
    for kind, q in queries.items():
        for r in db.query(q):
            nfo_path = _nfo_of(kind, r)
            mt = _mtime(nfo_path)
            if not mt:
                continue
            checked = datetime.fromisoformat(r["lang_checked"]) if r.get("lang_checked") else None
            retry = datetime.fromisoformat(r["lang_retry"]) if r.get("lang_retry") else None
            due_retry = retry is None or now - retry > timedelta(days=days)
            if checked and mt <= checked and (r.get("lang") in (None, want) or not due_retry):
                continue                                   # unverändert seit der letzten Prüfung
            text, ours = nfo_info(nfo_path)
            lang = detect(text)
            fields = {"lang": lang, "lang_checked": now.isoformat(timespec="seconds")}
            if lang is None or lang == want:
                db.update(kind, r["id"], **fields)
                continue
            if r["status"] == "neu":                       # wird ohnehin gescrapt
                db.update(kind, r["id"], **fields)
                continue
            if ours and not due_retry:
                db.update(kind, r["id"], **fields,
                          message=f"Keine Beschreibung auf {want.upper()} gefunden (vorhanden: {lang.upper()}) – "
                                  f"neuer Versuch nach {days} Tagen")
                continue
            # nicht deutsch -> neu scrapen
            fields.update(status="neu", lang_retry=now.isoformat(timespec="seconds"),
                          message=f"Beschreibung auf {lang.upper()} – wird neu gescrapt")
            db.update(kind, r["id"], **fields)
            if kind == "episodes":
                db.execute("UPDATE shows SET status='neu' WHERE id=? AND status='gescrapt'", (r["show_id"],))
            name = os.path.basename(r.get("path") or r.get("folder") or "")
            db.log("INFO", f"Sprachprüfung: {name} – Beschreibung {lang.upper()}, wird neu gescrapt")
            marked += 1
    if marked:
        db.log("INFO", f"Sprachprüfung: {marked} Einträge mit nicht {want.upper()}-sprachiger Beschreibung vorgemerkt")
    return marked
