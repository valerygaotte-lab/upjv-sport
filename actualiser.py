#!/usr/bin/env python3
"""
UPJV SPORT - récupération automatique des événements (Gothiques d'Amiens et Amiens SC) -> evenements.json

- Gothiques d'Amiens : lecture de la billetterie officielle (cartes « Amiens | adversaire | date » de la liste des
  événements, puis page de chaque événement pour la date exacte et la vente). Seuls les matchs À DOMICILE des Gothiques
  sont publiés (les autres événements vendus par la billetterie, ex. matchs internationaux au Coliseum, sont ignorés).
- Amiens SC (équipe première masculine, matchs À DOMICILE à la Licorne) : le site du club (amiensfootball.com)
  n'affiche aucun calendrier et sa billetterie interdit les robots (robots.txt) ; la FFF bloque aussi les robots.
  Le calendrier est donc lu sur TheFootData, qui reprend les données de l'API de la FFF : date, heure, adversaire et
  terrain de chaque match. Seuls les matchs dont le jour et l'heure sont programmés sont publiés (voir
  AMIENS_SC_ONLY_CONFIRMED) ; les autres sont seulement comptés. Rien n'est jamais deviné.
- Un ÉCHEC de récupération ne vide JAMAIS evenements.json : les événements déjà publiés et encore à venir sont
  conservés, et si toutes les sources échouent le fichier n'est pas modifié (le script se termine en erreur).
- Matchs impossibles à lire automatiquement : les ajouter dans evenements_manuels.json.
- Test de l'Amiens SC seul, sans modifier evenements.json : python3 actualiser.py --test-amiens
- Si aucun match n'est trouvé, les journaux expliquent pourquoi et les pages lues sont enregistrées dans debug/.
- Diagnostic (ce que renvoient réellement les sites, sans rien écrire) : python3 actualiser.py --diagnostic
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "evenements.json"
MANUAL = ROOT / "evenements_manuels.json"
DEBUG_DIR = ROOT / "debug"  # pages lues, gardées seulement pour diagnostiquer un échec (non publiées)
TIMEOUT = 25
HEADERS = {
    "User-Agent": "UPJVSportEventsBot/1.0 (student project; contact site administrator)"
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

GOTHIQUES_TICKETING = "https://billetterie.lesgothiques.com/fr"
AMIENS_SC_TICKETING = "https://billetterie.amiensfootball.com/"
AMIENS_SC_NEWS = "https://www.amiensfootball.com/actualites"

# --- Amiens SC : source du calendrier ---
PARIS = ZoneInfo("Europe/Paris")  # les heures de la source sont des heures de Paris
GOTHIQUES_CLUB = "Gothiques d'Amiens"
AMIENS_SC_CLUB = "Amiens SC"
AMIENS_SC_VENUE = "Stade Crédit Agricole la Licorne, Amiens"
FOOTDATA_BASE = "https://thefootdata.com"
FOOTDATA_TEAM = "358-SEM-1"  # Amiens SC, équipe première masculine (Senior Homme, équipe 1)
REQUEST_DELAY = 1.0          # secondes entre deux pages lues sur la source
# Lien de billetterie ajouté aux matchs de l'Amiens SC : la billetterie officielle du club. La source ne fournit pas de
# lien de vente par match : ce lien mène donc à la page d'accueil de la billetterie. Mettre "" pour ne publier aucun bouton.
AMIENS_SC_TICKET_LINK = AMIENS_SC_TICKETING
# Matchs dont la date n'est pas encore confirmée : la FFF publie tout le calendrier de la saison dès l'été, chaque journée
# à une date et une heure PAR DÉFAUT (en 2026-2027 : le samedi, ou le vendredi en fin de saison, à 14:45). Le jour et l'heure
# réels ne sont fixés que quelques semaines avant. L'heure par défaut est reconnue automatiquement : c'est l'heure commune
# à (presque) tous les matchs lointains, qui ne peuvent pas encore être programmés. Un match à cette heure-là n'est pas publié.
AMIENS_SC_ONLY_CONFIRMED = True
PROVISIONAL_HORIZON = timedelta(days=45)  # au-delà, un match n'est normalement pas encore programmé
AMIENS_SC_STATS: dict = {}                # informations sur la dernière lecture, recopiées dans evenements.json (sources)


def get_soup(url: str) -> BeautifulSoup | None:
    try:
        response = SESSION.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")
    except requests.RequestException as exc:
        logging.warning("Impossible de récupérer %s : %s", url, exc)
        return None


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def future_date(value: datetime | None) -> bool:
    return bool(value and value.replace(tzinfo=value.tzinfo or timezone.utc) > datetime.now(timezone.utc))


def parse_french_date(text: str) -> datetime | None:
    """Reconnaît les dates complètes françaises courantes, pas les dates ambiguës."""
    months = {
        "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
        "decembre": 12,
    }
    clean = normalize(text).lower()
    match = re.search(r"\b(\d{1,2})\s+(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)\s+(\d{4})\b", clean)
    if match:
        try:
            return datetime(int(match.group(3)), months[match.group(2)], int(match.group(1)), tzinfo=timezone.utc)
        except ValueError:
            return None
    # ISO dates, e.g. 2026-10-16 or 2026-10-16T20:15
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2}))?", clean)
    if match:
        try:
            hour = int(match.group(2) or 12)
            minute = int(match.group(3) or 0)
            return datetime.fromisoformat(match.group(1)).replace(hour=hour, minute=minute, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def is_closed(text: str) -> bool:
    t = normalize(text).lower()
    return any(term in t for term in (
        "vente terminée", "ventes terminées", "épuisé", "epuise",
        "indisponible", "événement passé", "evenement passe",
        "aucune offre disponible", "billetterie fermée", "billetterie fermee",
    ))


def has_purchase_signal(soup: BeautifulSoup, page_text: str) -> bool:
    if is_closed(page_text):
        return False
    purchase_words = ("réserver", "reserver", "acheter", "billetterie", "billets", "ticket")
    for a in soup.find_all("a", href=True):
        label = normalize(a.get_text(" ", strip=True)).lower()
        href = a["href"].lower()
        if any(word in label for word in purchase_words) and not any(
            bad in label for bad in ("abonnement", "boutique", "faq", "espace retrait")
        ):
            return True
        if "/evenement/" in href and ("details" in href or "checkout" in href):
            return True
    return False


def _gothiques_cards(soup: BeautifulSoup, base: str) -> dict[str, list[list[str]]]:
    """Pour chaque événement de la liste : lignes de texte de ses cartes (« Amiens | Marseille | … | Ven 16 Oct, 20h15 | … »).
    Une carte est le plus grand bloc autour d'un lien qui ne contient aucun lien vers un autre événement
    (un même événement peut avoir plusieurs cartes : « prochain évènement » et liste)."""
    def event_id(href: str) -> str | None:
        m = re.search(r"/fr/evenement/(\d+)(?:/|$|\?)", urljoin(base, href))
        return m.group(1) if m else None

    cards: dict[str, list[list[str]]] = {}
    seen = set()
    for a in soup.find_all("a", href=True):
        number = event_id(a["href"])
        if not number:
            continue
        url = f"{GOTHIQUES_TICKETING}/evenement/{number}/details"
        node = a
        while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
            others = {event_id(x["href"]) for x in node.parent.find_all("a", href=True)} - {None, number}
            if others:
                break
            node = node.parent
        if id(node) in seen:
            continue
        seen.add(id(node))
        cards.setdefault(url, []).append([normalize(x) for x in node.get_text("\n").split("\n") if normalize(x)])
    return cards


# Date d'une carte de la liste : « Ven 16 Oct, 20h15 »
GOTHIQUES_CARD_DATE_RE = re.compile(r"^(lun|mar|mer|jeu|ven|sam|dim)\w*\.?\s+\d{1,2}\s+\w+\.?,?\s+\d{1,2}h\d{2}$", re.I)


def gothiques_teams(cards: list[list[str]]) -> tuple[str, str] | None:
    """(équipe qui reçoit, équipe visiteuse) lues sur la carte « Amiens | Marseille | LIGUE MAGNUS | … | Ven 16 Oct, 20h15 | … »,
    ou None si aucune carte de l'événement n'a cette forme."""
    for lines in cards:
        if len(lines) >= 3 and any(GOTHIQUES_CARD_DATE_RE.match(line) for line in lines):
            return lines[0], lines[1]
    return None


def is_amiens(team: str) -> bool:
    folded = _fold(team)
    return "amiens" in folded or "gothiques" in folded


def scrape_gothiques(previous: dict[str, dict] | None = None) -> list[dict] | None:
    """Renvoie la liste des événements, ou None si la récupération a échoué (≠ liste vide : aucun événement)."""
    previous = previous or {}
    logging.info("Vérification de la billetterie des Gothiques…")
    listing = get_soup(GOTHIQUES_TICKETING)
    if not listing:
        return None  # échec : l'appelant conserve les événements déjà publiés

    # Chaque événement a une carte « Amiens | Marseille | … » dans la liste « Évènement(s) à venir ».
    # (La page /fr/calendrier est remplie en JavaScript : elle ne contient aucun événement lisible.)
    cards = _gothiques_cards(listing, GOTHIQUES_TICKETING)
    detail_urls = set(cards)

    if not detail_urls:
        had_events = any(
            e.get("club") == GOTHIQUES_CLUB and event_is_future(e) and not str(e.get("id", "")).startswith("manuel-")
            for e in previous.values()
        )
        if had_events:
            # La page répond mais aucun lien d'événement n'est reconnu alors qu'il y en avait : le site a
            # probablement changé de structure. On ne vide pas la liste, on signale un échec.
            logging.error("Gothiques : aucun lien d'événement reconnu alors que des événements étaient publiés "
                          "(structure du site modifiée ?). Données précédentes conservées.")
            return None
        logging.info("Gothiques : aucun événement affiché sur la billetterie.")
        return []

    events = []
    unreadable = []  # pages de détail injoignables : on garde la version précédente de l'événement
    with_teams = 0
    for url in sorted(detail_urls, key=lambda u: int(u.rstrip("/").split("/")[-2])):
        event_id = f"gothiques-{url.rstrip('/').split('/')[-2]}"
        teams = gothiques_teams(cards[url])
        if not teams:
            logging.info("Gothiques : événement %s ignoré (équipes non indiquées sur la billetterie).", url)
            continue
        with_teams += 1
        home, away = teams
        if not is_amiens(home):
            # Autres événements vendus par la billetterie (matchs internationaux au Coliseum, matchs à l'extérieur…) :
            # ce ne sont pas des matchs à domicile des Gothiques, on ne les publie pas sous leur nom.
            logging.info("Gothiques : « %s - %s » ignoré (pas un match à domicile des Gothiques).", home, away)
            continue
        soup = get_soup(url)
        if not soup:
            unreadable.append(event_id)
            continue
        page_text = normalize(soup.get_text(" ", strip=True))
        if is_closed(page_text) or not has_purchase_signal(soup, page_text):
            continue

        # Les pages officielles contiennent typiquement "Le 16/10/2026 à 20h15".
        date_match = re.search(r"\bLe\s+(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+à\s+(\d{1,2})h(\d{2})?)?", page_text, re.I)
        if not date_match:
            continue
        try:
            event_date = datetime(
                int(date_match.group(3)), int(date_match.group(2)), int(date_match.group(1)),
                int(date_match.group(4) or 12), int(date_match.group(5) or 0),
                tzinfo=PARIS  # les heures affichées par la billetterie sont des heures de Paris
            )
        except ValueError:
            continue
        if not future_date(event_date):
            continue

        # Les événements des Gothiques à domicile doivent mentionner Amiens et le Coliseum.
        if "amiens" not in page_text.lower() or "coliseum" not in page_text.lower():
            continue

        events.append({
            "id": event_id,
            "club": GOTHIQUES_CLUB,
            "match": f"{GOTHIQUES_CLUB} - {away}",
            "adversaire": away,
            "date": event_date.isoformat(),
            "lieu": "Coliseum, Amiens",
            "billetterie": url,
            "source": url,
            "statut": "billetterie détectée",
        })

    if not with_teams:
        # Des liens d'événements existent mais aucune carte n'est lisible : la mise en page a changé.
        logging.error("Gothiques : %d événement(s) en lien mais aucune carte « équipe | équipe | date » reconnue "
                      "(structure du site modifiée ?). Données précédentes conservées.", len(detail_urls))
        return None

    for event_id in unreadable:
        old = previous.get(event_id)
        if old and event_is_future(old):
            logging.warning("Gothiques : page %s illisible, version précédente conservée.", event_id)
            events.append({**old, "statut": "conservé : dernière vérification impossible"})
        else:
            logging.warning("Gothiques : page %s illisible et inconnue jusqu'ici, ignorée.", event_id)

    logging.info("Gothiques : %d événement(s) détecté(s)", len(events))
    return events


def _fold(text: str) -> str:
    """Minuscules sans accents ni ponctuation, pour comparer des noms."""
    text = unicodedata.normalize("NFD", text or "")
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in text if unicodedata.category(c) != "Mn").lower()).strip()


FR_MONTHS = {"janv": 1, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6, "juil": 7, "aout": 8,
             "sept": 9, "oct": 10, "nov": 11, "dec": 12}
FR_DAYS = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")  # indice = datetime.weekday()
FOOTDATA_DATE_RE = re.compile(r"\b(lun|mar|mer|jeu|ven|sam|dim)\.?\s+(\d{1,2})\s+([^\W\d_]+)\.?\s+(20\d{2})\b", re.I)
# Motifs de rejet qui trahissent une page illisible (et non un match légitimement écarté)
STRUCTURAL_REJECTS = {"titre de la page illisible", "date non publiée", "date invalide", "terrain non publié",
                      "page du match injoignable"}


# Sigles que la FFF écrit « Fc », « Sm », « Qrm »… : remis en majuscules pour l'affichage (le nom n'est pas modifié autrement).
CLUB_ACRONYMS = {"ac", "aj", "ajc", "as", "asm", "ca", "cs", "ea", "es", "esa", "fc", "fbbp", "ogc", "ol", "om", "psg", "qrm",
                 "rc", "sc", "scf", "sco", "sm", "us", "usc", "usco", "usl", "ust", "ffc", "afc", "cso", "sa", "ssa"}


def club_name(name: str) -> str:
    """« Sm Caen » -> « SM Caen », « Fc Rouen 1899 » -> « FC Rouen 1899 », « Qrm » -> « QRM »."""
    return " ".join(w.upper() if w.lower().strip(".") in CLUB_ACRONYMS else w for w in normalize(name).split(" "))


def parse_iso(value: object) -> datetime | None:
    """Lit une date ISO ; sans fuseau, on considère qu'il s'agit de l'heure de Paris."""
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=PARIS)


def event_is_future(event: dict) -> bool:
    return future_date(parse_iso(event.get("date")))


def load_previous() -> list[dict]:
    """Événements déjà publiés dans evenements.json (liste vide si le fichier est absent ou illisible)."""
    try:
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logging.warning("%s précédent illisible ou absent (%s) : on repart de zéro.", OUTPUT.name, exc)
        return []
    items = data.get("evenements", []) if isinstance(data, dict) else []
    return [e for e in items if isinstance(e, dict)]


def _previous_amiens_events() -> list[dict]:
    return [e for e in load_previous()
            if e.get("club") == AMIENS_SC_CLUB and str(e.get("id", "")).startswith("amiens-sc-") and event_is_future(e)]


def _save_debug(name: str, soup: BeautifulSoup) -> None:
    """Enregistre une page lue (dossier debug/, téléversé par le workflow en cas de problème, jamais publié)."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / name).write_text(str(soup)[:400000], encoding="utf-8")
        logging.info("Page enregistrée pour diagnostic : %s/%s", DEBUG_DIR.name, name)
    except OSError as exc:
        logging.warning("Impossible d'enregistrer %s : %s", name, exc)


def _previous_sources() -> dict:
    try:
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
        return data.get("sources", {}) if isinstance(data, dict) and isinstance(data.get("sources"), dict) else {}
    except (OSError, ValueError):
        return {}


def default_kickoff(candidates: list[dict], now: datetime) -> tuple[int, int] | None:
    """Heure « par défaut » de la FFF : celle d'au moins 75 % des matchs à plus de 45 jours (au moins 3 matchs).
    Sinon, celle reconnue lors de la lecture précédente (fin de saison : trop peu de matchs lointains pour la reconnaître)."""
    far = [(c["when"].hour, c["when"].minute) for c in candidates if c["time_known"] and c["when"] - now > PROVISIONAL_HORIZON]
    if len(far) >= 3:
        (hour_minute, count), = Counter(far).most_common(1)
        if count / len(far) >= 0.75:
            return hour_minute
    saved = str(_previous_sources().get(AMIENS_SC_CLUB, {}).get("horaire_par_defaut", ""))
    found = re.fullmatch(r"(\d{2}):(\d{2})", saved)
    return (int(found.group(1)), int(found.group(2))) if found else None


def _amiens_failure(reason: str) -> None:
    """Échec de récupération : on explique pourquoi (main() conserve les matchs déjà publiés)."""
    kept = len(_previous_amiens_events())
    logging.warning("Amiens SC : AUCUN match mis à jour, car %s.", reason)
    logging.warning("Amiens SC : %d match(s) précédent(s) encore à venir seront conservés dans evenements.json.", kept)
    return None


def _robots_allows(url: str) -> bool | None:
    """True/False selon robots.txt ; None si robots.txt est inaccessible (5xx, réseau) : dans le doute on ne lit pas."""
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    try:
        response = SESSION.get(base + "/robots.txt", timeout=TIMEOUT)
    except requests.RequestException as exc:
        logging.warning("robots.txt de %s injoignable : %s", base, exc)
        return None
    status = getattr(response, "status_code", 200)
    parser = robotparser.RobotFileParser()
    if 400 <= status < 500:
        parser.parse([])  # pas de robots.txt : aucune restriction (RFC 9309)
    elif status >= 500:
        logging.warning("robots.txt de %s : erreur serveur %s", base, status)
        return None
    else:
        parser.parse(response.text.splitlines())
    return parser.can_fetch(HEADERS["User-Agent"], url)


def _parse_footdata_match(soup: BeautifulSoup) -> tuple[dict | None, str]:
    """Lit une page de match TheFootData. Renvoie (infos, "") ou (None, raison du rejet)."""
    title = normalize(soup.title.get_text()) if soup.title else ""
    heading = title.split("|")[0].strip()  # « Amiens Scf – Thionville Lusitanos | TheFootData »
    teams = re.split(r"\s+[–—-]\s+", heading, maxsplit=1)
    if len(teams) != 2:
        return None, "titre de la page illisible"
    home, away = normalize(teams[0]), club_name(teams[1])
    if not _fold(home).startswith("amiens"):
        return None, "match à l'extérieur"
    text = soup.get_text("\n")
    lines = [normalize(line) for line in text.split("\n") if normalize(line)]
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = normalize(meta.get("content", "")) if meta else ""
    competition = re.split(r"[,·]", description[len(heading) + 1:])[0].strip() if description.startswith(heading + ",") else ""
    if not competition:  # ligne « Ligue 3 (ex-National) · Journée 11 · 2026-2027 »
        for line in lines:
            if "·" in line and re.search(r"\b20\d{2}-20\d{2}\b", line):
                competition = line.split("·")[0].strip()
                break
    if any(word in _fold(competition) for word in ("amica", "mafff")):
        return None, "match amical"
    body_date = FOOTDATA_DATE_RE.search(text)
    date_match = FOOTDATA_DATE_RE.search(description) or body_date
    if not date_match:
        return None, "date non publiée"
    day_name, day, month_name, year = date_match.groups()
    folded_month = _fold(month_name)
    month = next((number for prefix, number in FR_MONTHS.items() if folded_month.startswith(prefix)), None)
    try:
        game_day = datetime(int(year), month, int(day), tzinfo=PARIS)
    except (TypeError, ValueError):
        return None, "date invalide"
    if FR_DAYS[game_day.weekday()] != day_name.lower()[:3]:
        return None, "jour de la semaine incohérent avec la date"
    # Heure du coup d'envoi : juste avant ou juste après la date (« 20:45 » / « jeu 29 oct. 2026 »).
    # Absente ou 00:00 (valeur par défaut d'une heure non fixée) = « horaire à confirmer ».
    hour = minute = 0
    time_known = False
    if body_date:
        before = text[max(0, body_date.start() - 30):body_date.start()].rstrip()
        after = text[body_date.end():body_date.end() + 30].lstrip()
        found = re.search(r"(\d{1,2})[:h](\d{2})$", before) or re.match(r"(\d{1,2})[:h](\d{2})\b", after)
        if found and 0 <= int(found.group(1)) < 24 and 0 <= int(found.group(2)) < 60 and (int(found.group(1)), int(found.group(2))) != (0, 0):
            hour, minute, time_known = int(found.group(1)), int(found.group(2)), True
    # Terrain : on ne garde que les rencontres au stade de la Licorne, à Amiens.
    try:
        start = next(i for i, line in enumerate(lines) if _fold(line) == "terrain")
    except StopIteration:
        return None, "terrain non publié"
    end = next((i for i in range(start + 1, len(lines)) if _fold(lines[i]) in ("arbitres", "feuille de match")), start + 8)
    venue_block = _fold(" ".join(lines[start:end]))
    if "licorne" not in venue_block or "amiens" not in venue_block:
        return None, "match joué hors du stade de la Licorne"
    return {"away": away, "competition": competition,
            "when": game_day.replace(hour=hour, minute=minute), "time_known": time_known}, ""


def scrape_amiens_sc() -> list[dict] | None:
    """
    Matchs À DOMICILE à venir de l'équipe première masculine de l'Amiens SC, au stade de la Licorne.
    Renvoie None si la récupération a échoué (les matchs déjà publiés sont alors conservés par main()),
    ou la liste des matchs (éventuellement vide : aucun match à domicile à jouer d'après la source).

    Sources examinées : amiensfootball.com (aucun calendrier ; page « équipe » chargée en JavaScript) et
    billetterie.amiensfootball.com (robots.txt interdit les robots). Le calendrier est donc lu sur TheFootData
    (données de l'API de la FFF) : page de l'équipe, puis une page par match à domicile à jouer.
    """
    logging.info("Vérification du calendrier de l'Amiens SC (TheFootData, données FFF)…")
    now = datetime.now(PARIS)
    season = now.year if now.month >= 7 else now.year - 1
    club_url = f"{FOOTDATA_BASE}/clubs/amiens-sporting-club-football?equipe={FOOTDATA_TEAM}&saison={season}"

    allowed = _robots_allows(club_url)
    if allowed is None:
        return _amiens_failure("le robots.txt de TheFootData n'a pas pu être lu (serveur en erreur ou injoignable)")
    if not allowed:
        return _amiens_failure("le robots.txt de TheFootData interdit la lecture automatique de cette page")
    club_page = get_soup(club_url)
    if not club_page:
        return _amiens_failure(f"la page {club_url} est injoignable ou renvoie une erreur (voir l'avertissement ci-dessus)")

    # Matchs à jouer : liens /matchs/<id>-<slug> dont le texte est « à jouer » ; domicile = slug qui commence par « amiens-… ».
    all_links, to_play, away_count = 0, {}, 0
    for a in club_page.find_all("a", href=True):
        match = re.search(r"/matchs/(\d+)-([a-z0-9-]+)", a["href"])
        if not match:
            continue
        all_links += 1
        if _fold(a.get_text()) != "a jouer":
            continue
        if match.group(2).startswith("amiens-"):
            to_play[match.group(1)] = urljoin(FOOTDATA_BASE, a["href"].split("?")[0])
        else:
            away_count += 1
    logging.info("TheFootData : %d lien(s) de match, %d à jouer à domicile, %d à jouer à l'extérieur (ignorés)",
                 all_links, len(to_play), away_count)
    if not all_links:
        _save_debug("amiens-equipe.html", club_page)
        return _amiens_failure("aucun lien de match n'a été reconnu sur la page de l'équipe (mise en page modifiée, ou page vide)")

    previous = {e.get("id"): e for e in _previous_amiens_events()}
    events, candidates, rejected, saved_debug = [], [], Counter(), False
    for match_id, url in sorted(to_play.items()):
        time.sleep(REQUEST_DELAY)
        if _robots_allows(url) is False:
            rejected["lecture interdite par robots.txt"] += 1
            continue
        soup = get_soup(url)
        event_id = f"amiens-sc-{match_id}"
        if not soup:
            if event_id in previous:
                logging.warning("Amiens SC : page %s illisible, version précédente conservée.", url)
                events.append({**previous[event_id], "statut": "conservé : dernière vérification impossible"})
            else:
                rejected["page du match injoignable"] += 1
            continue
        info, why = _parse_footdata_match(soup)
        if not info:
            rejected[why] += 1
            logging.info("Amiens SC : match ignoré (%s) : %s", why, url)
            if why in STRUCTURAL_REJECTS and not saved_debug:
                _save_debug(f"amiens-match-{match_id}.html", soup)
                saved_debug = True
            continue
        if not future_date(info["when"]):
            rejected["match déjà passé"] += 1
            continue
        candidates.append({**info, "id": event_id, "url": url})

    # Date confirmée = heure de coup d'envoi publiée et différente de l'heure par défaut de la FFF.
    default = default_kickoff(candidates, now)
    if default:
        logging.info("Amiens SC : heure par défaut de la FFF reconnue : %02d:%02d (matchs pas encore programmés).", *default)
    provisional = []
    for info in candidates:
        confirmed = info["time_known"] and (info["when"].hour, info["when"].minute) != default
        if not confirmed and AMIENS_SC_ONLY_CONFIRMED:
            provisional.append(info)
            continue
        label = "TheFootData (données FFF)" + (f" · {info['competition']}" if info["competition"] else "")
        events.append({
            "id": info["id"],
            "club": AMIENS_SC_CLUB,
            "match": f"Amiens SC - {info['away']}",
            "adversaire": info["away"],
            "date": info["when"].isoformat(),
            "lieu": AMIENS_SC_VENUE,
            "billetterie": AMIENS_SC_TICKET_LINK,
            "source": info["url"],
            "statut": label + ("" if confirmed else " · date et horaire à confirmer")
                      + (" · billetterie : page d'accueil du club" if AMIENS_SC_TICKET_LINK else ""),
        })
    if provisional:
        logging.info("Amiens SC : %d match(s) à domicile au calendrier mais pas encore programmé(s), non publiés : %s",
                     len(provisional), ", ".join(f"{p['when']:%d/%m} {p['away']}" for p in provisional))
    AMIENS_SC_STATS.clear()
    AMIENS_SC_STATS.update({
        "a_confirmer": len(provisional),
        "prochain_a_confirmer": min((p["when"] for p in provisional), default=None),
        "horaire_par_defaut": f"{default[0]:02d}:{default[1]:02d}" if default else "",
    })
    if AMIENS_SC_STATS["prochain_a_confirmer"]:
        AMIENS_SC_STATS["prochain_a_confirmer"] = AMIENS_SC_STATS["prochain_a_confirmer"].date().isoformat()

    if rejected:
        logging.info("Amiens SC : %d match(s) à domicile écarté(s) : %s", sum(rejected.values()),
                     ", ".join(f"{n} × {why}" for why, n in rejected.items()))
    logging.info("Amiens SC : %d match(s) à domicile à date confirmée détecté(s)", len(events))
    if not events and provisional:
        logging.warning("Amiens SC : aucun match à domicile programmé pour l'instant ; %d au calendrier, dates à confirmer.", len(provisional))
    elif not events and to_play:
        if any(why in STRUCTURAL_REJECTS for why in rejected):
            return _amiens_failure("des matchs à jouer ont été trouvés mais leurs pages n'ont pas pu être lues "
                                   "(mise en page de TheFootData modifiée ?) : " + ", ".join(rejected))
        logging.warning("Amiens SC : %d match(s) à domicile à jouer trouvé(s), mais aucun retenu : voir les motifs ci-dessus.", len(to_play))
    elif not events:
        logging.warning("Amiens SC : la source n'affiche aucun match à domicile à jouer (fin de saison, ou calendrier non publié).")
    return events


def load_manual_events() -> list[dict]:
    """Matchs saisis à la main dans evenements_manuels.json (solution fiable quand la source automatique manque)."""
    if not MANUAL.exists():
        return []
    try:
        data = json.loads(MANUAL.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logging.error("%s illisible, ignoré : %s", MANUAL.name, exc)
        return []
    items = data.get("evenements", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        logging.error("%s : la clé « evenements » doit contenir une liste.", MANUAL.name)
        return []
    events = []
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            logging.warning("Événement manuel n°%d ignoré : entrée invalide.", number)
            continue
        club = normalize(str(item.get("club", "")))
        match = normalize(str(item.get("match", "")))
        when = parse_iso(item.get("date", ""))
        if not club or not match or not when:
            logging.warning("Événement manuel n°%d ignoré : « club », « match » et « date » (AAAA-MM-JJTHH:MM) sont obligatoires.", number)
            continue
        if not future_date(when):
            continue  # match passé : non publié
        link = str(item.get("billetterie", "")).strip()
        if link and not link.startswith("https://"):
            logging.warning("Événement manuel n°%d : lien de billetterie ignoré (il doit commencer par https://).", number)
            link = ""
        slug = re.sub(r"[^a-z0-9]+", "-", f"{club}-{when:%Y%m%d%H%M}-{match}".lower()).strip("-")
        events.append({
            "id": f"manuel-{slug}", "club": club, "match": match, "date": when.isoformat(),
            "lieu": normalize(str(item.get("lieu", ""))), "billetterie": link,
            "source": link or "saisie manuelle", "statut": "saisi manuellement",
        })
    logging.info("Événements manuels : %d à venir", len(events))
    return events


def main() -> int:
    previous_events = load_previous()
    previous_by_id = {e["id"]: e for e in previous_events if e.get("id")}
    scrapers = ((GOTHIQUES_CLUB, lambda: scrape_gothiques(previous_by_id)), (AMIENS_SC_CLUB, scrape_amiens_sc))

    events, sources, warnings = [], {}, []
    for club, scrape in scrapers:
        try:
            found = scrape()
        except Exception:  # une source en panne ne doit jamais empêcher l'autre d'être mise à jour
            logging.exception("%s : erreur inattendue pendant la récupération", club)
            found = None
        if found is None:
            # ÉCHEC de récupération (et non « aucun événement ») : on conserve ce qui était valide.
            kept = [e for e in previous_events
                    if e.get("club") == club and event_is_future(e) and not str(e.get("id", "")).startswith("manuel-")]
            events.extend(kept)
            sources[club] = {"ok": False, "trouves": 0, "conserves": len(kept)}
            warnings.append(f"{club} : lecture impossible, données précédentes conservées ({len(kept)})")
            logging.warning("%s : récupération impossible, %d événement(s) précédent(s) conservé(s).", club, len(kept))
            print(f"::warning title=Source en échec::{club} : lecture impossible, {len(kept)} événement(s) précédent(s) conservé(s)")
        else:
            events.extend(found)
            sources[club] = {"ok": True, "trouves": len(found)}
            if club == AMIENS_SC_CLUB:
                sources[club].update(AMIENS_SC_STATS)

    if not any(info["ok"] for info in sources.values()):
        logging.error("Toutes les sources ont échoué : %s n'est PAS modifié.", OUTPUT.name)
        return 1

    manual = load_manual_events()
    # Un match saisi à la main corrige l'automatique du même club et du même jour.
    manual_days = {(e["club"], e["date"][:10]) for e in manual}
    events = [e for e in events if (e["club"], e["date"][:10]) not in manual_days] + manual

    # Déduplication (même identifiant, ou même club à la même heure exacte) puis tri par date réelle.
    unique, seen_slots = {}, set()
    for event in events:
        slot = (event["club"], parse_iso(event.get("date")))
        if event["id"] in unique or slot in seen_slots:
            continue
        unique[event["id"]] = event
        seen_slots.add(slot)
    ordered = sorted(unique.values(), key=lambda e: parse_iso(e.get("date")) or datetime.max.replace(tzinfo=timezone.utc))

    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "avertissements": warnings,
        "evenements": ordered,
    }
    # Écriture atomique : un plantage ne laisse jamais un fichier à moitié écrit.
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, OUTPUT)

    counts = Counter(e["club"] for e in ordered)
    logging.info("Fichier écrit : %s (%d événement(s) : %s)", OUTPUT.name, len(ordered),
                 ", ".join(f"{club} {n}" for club, n in counts.items()) or "aucun")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:  # résumé affiché en haut de la page du workflow sur GitHub
        lines = ["### Événements UPJV SPORT", "", "| Club | Source | Événements publiés |", "|---|---|---|"]
        for club, info in sources.items():
            state = "lue" if info["ok"] else "**en échec** (données précédentes conservées)"
            lines.append(f"| {club} | {state} | {counts.get(club, 0)} |")
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


def diagnostic() -> int:
    """Affiche ce que renvoient réellement les sources (statut HTTP, robots.txt, liens utiles, extraits) sans rien écrire."""
    def show(url: str, link_words: tuple[str, ...] = ()) -> BeautifulSoup | None:
        print(f"\n===== {url}")
        print(f"robots.txt autorise : {_robots_allows(url)}")
        try:
            response = SESSION.get(url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            print(f"ERREUR réseau : {exc}")
            return None
        print(f"HTTP {response.status_code} · {len(response.text)} caractères · URL finale {response.url}")
        print("En-têtes : " + ", ".join(f"{k}={response.headers.get(k)}" for k in ("content-type", "server", "x-powered-by") if response.headers.get(k)))
        soup = BeautifulSoup(response.text, "html.parser")
        print(f"<title> : {normalize(soup.title.get_text()) if soup.title else '-'}")
        meta = soup.find("meta", attrs={"name": "description"})
        print(f"description : {normalize(meta.get('content', '')) if meta else '-'}")
        links = [(normalize(a.get_text(' ', strip=True)), a["href"]) for a in soup.find_all("a", href=True)]
        print(f"{len(links)} lien(s)")
        shown = 0
        for label, href in links:
            if link_words and not any(w in (_fold(label) + " " + href.lower()) for w in link_words):
                continue
            print(f"  lien : {label[:90]!r} -> {href[:160]}")
            shown += 1
            if shown >= 60:
                break
        for tag in soup.find_all("script", src=False)[:40]:
            content = tag.string or ""
            if any(w in content for w in ("__NEXT_DATA__", "match", "fixture", "rencontre")) or tag.get("type") in ("application/ld+json", "application/json"):
                print(f"  script {tag.get('type') or ''} {tag.get('id') or ''} : {normalize(content)[:600]}")
        text = soup.get_text("\n")
        lines = [normalize(line) for line in text.split("\n") if normalize(line)]
        print("Texte (début) : " + " | ".join(lines[:120])[:3000])
        return soup

    now = datetime.now(PARIS)
    season = now.year if now.month >= 7 else now.year - 1
    team_url = f"{FOOTDATA_BASE}/clubs/amiens-sporting-club-football?equipe={FOOTDATA_TEAM}&saison={season}"
    team = get_soup(team_url)
    if team:
        print("\n===== Horaires publiés (tous les matchs de la saison)")
        for a in team.find_all("a", href=True):
            m = re.search(r"/matchs/(\d+)-([a-z0-9-]+)", a["href"])
            if not m:
                continue
            time.sleep(0.5)
            page = get_soup(urljoin(FOOTDATA_BASE, a["href"]))
            if not page:
                continue
            text = page.get_text("\n")
            lines = [normalize(x) for x in text.split("\n") if normalize(x)]
            i = next((k for k, x in enumerate(lines) if re.search(r"\b20\d{2}-20\d{2}\b", x) and "·" in x), None)
            excerpt = " | ".join(lines[i:i + 6]) if i is not None else "?"
            print(f"  {_fold(a.get_text())[:8]:8} {m.group(2)[:40]:40} {excerpt[:160]}")
    show("https://www.amiensfootball.com/", ("match", "calendrier", "billet", "ticket", "equipe"))
    show(AMIENS_SC_TICKETING, ("match", "billet", "event", "evenement", "manifestation", "achat"))
    for url in (GOTHIQUES_TICKETING, GOTHIQUES_TICKETING + "/calendrier"):
        page = show(url, ("evenement", "calendrier"))
        if page:
            for card_url, found in _gothiques_cards(page, url).items():
                for lines in found:
                    print(f"  carte {card_url} : {' | '.join(lines)[:300]}")
    detail = show(GOTHIQUES_TICKETING + "/evenement/36/details")
    if detail:
        print("h1/h2 : " + " / ".join(normalize(h.get_text(" ", strip=True)) for h in detail.find_all(["h1", "h2", "h3"])[:10]))
    return 0


if __name__ == "__main__":
    if "--diagnostic" in sys.argv:
        sys.exit(diagnostic())
    if "--test-amiens" in sys.argv:
        # Test de la récupération de l'Amiens SC seule : affiche les matchs détectés, ne modifie pas evenements.json.
        found = scrape_amiens_sc()
        if found is None:
            print("=> ÉCHEC : la récupération des matchs de l'Amiens SC a échoué (voir les avertissements ci-dessus)")
            sys.exit(1)
        for event in found:
            print(f"{event['date'][:16]} | {event['match']} | {event['lieu']} | {event['statut']}")
        print(f"=> {len(found)} match(s) de l'Amiens SC à date confirmée détecté(s) ; "
              f"{AMIENS_SC_STATS.get('a_confirmer', 0)} autre(s) au calendrier, date à confirmer (non publiés)")
    else:
        sys.exit(main())
