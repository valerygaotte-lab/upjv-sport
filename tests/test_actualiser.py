"""Tests hors ligne de actualiser.py : python3 -m unittest discover -s tests -v

Les pages simulées reprennent la structure réelle des sources (TheFootData et billetterie des Gothiques, relevée
le 13/10/2026 depuis GitHub Actions). Les dates sont calculées à partir d'aujourd'hui pour que les tests restent valables.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import actualiser as a  # noqa: E402

DAYS = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")
MONTHS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")
NOW = datetime.now(a.PARIS)


def fr_date(dt: datetime) -> str:  # « jeu 29 oct. 2026 », comme sur TheFootData
    return f"{DAYS[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1]} {dt.year}"


def at(days: int, hour: int, minute: int) -> datetime:
    return (NOW + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)


def footdata_match(home: str, away: str, when: datetime, venue: str = "Stade Credit Agricole la Licorne",
                   address: str = "25 Rue du Chapitre, 80000 Amiens") -> str:
    return f"""<html><head><title>{home} – {away} | TheFootData</title>
<meta name="description" content="{home} – {away}, Ligue 3 (ex-National), {fr_date(when)} : score, terrain et arbitres."></head>
<body><nav><a href="/">TheFootData</a></nav><h1><span>{home}</span><span>–</span><span>{away}</span></h1>
<p>Ligue 3 (ex-National) · Journée 11 · 2026-2027</p>
<div><span>{home}</span><div><span>{when:%H:%M}</span><span>{fr_date(when)}</span></div><span>{away}</span></div>
<h2>Terrain</h2><dl><dt>Nom</dt><dd>{venue}</dd><dt>Adresse</dt><dd>{address}</dd></dl>
<h2>Arbitres</h2><p>Désignations non publiées pour ce match.</p></body></html>"""


def gothiques_card(number: int, home: str, away: str, label: str) -> str:
    return f"""<div class="card"><div><span>{home}</span><span>{away}</span></div><p>SYNERGLACE LIGUE MAGNUS</p><p>-</p>
<p>Journée 10</p><p>{label}</p><p>Patinoire coliseum</p>
<a href="/fr/evenement/{number}">Réserver</a><a href="/fr/evenement/{number}/details">Voir les offres</a></div>"""


def gothiques_detail(when: datetime, place: str = "Patinoire coliseum") -> str:
    return f"""<html><head><title>Amiens</title></head><body><h1>Découvrez nos offres</h1>
<p>Le {when:%d/%m/%Y} à {when:%H}h{when:%M}</p><p>SYNERGLACE LIGUE MAGNUS</p><p>{place}</p><p>Amiens</p>
<a href="/fr/evenement/7?populationOffers=1">Réserver</a></body></html>"""


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


class FootDataTests(unittest.TestCase):
    def test_home_match(self):
        when = at(16, 20, 45)
        info, why = a._parse_footdata_match(soup(footdata_match("Amiens Scf", "Sm Caen", when)))
        self.assertEqual(why, "")
        self.assertEqual(info["away"], "SM Caen")
        self.assertEqual(info["when"], when)
        self.assertTrue(info["time_known"])

    def test_away_match_rejected(self):
        info, why = a._parse_footdata_match(soup(footdata_match("Valenciennes Fc", "Amiens Scf", at(4, 14, 45))))
        self.assertIsNone(info)
        self.assertEqual(why, "match à l'extérieur")

    def test_relocated_match_rejected(self):
        page = footdata_match("Amiens Scf", "Fbbp", at(20, 20, 0), venue="Stade Paul Debresie", address="Saint-Quentin")
        self.assertEqual(a._parse_footdata_match(soup(page))[1], "match joué hors du stade de la Licorne")

    def test_default_kickoff_detected(self):
        far = [{"when": at(60 + 14 * i, 14, 45), "time_known": True} for i in range(8)]
        near = [{"when": at(16, 20, 45), "time_known": True}]
        self.assertEqual(a.default_kickoff(near + far, NOW), (14, 45))

    def test_no_default_kickoff_when_times_vary(self):
        varied = [{"when": at(60 + 7 * i, h, 0), "time_known": True} for i, h in enumerate((15, 18, 19, 20, 21))]
        with mock.patch.object(a, "_previous_sources", return_value={}):
            self.assertIsNone(a.default_kickoff(varied, NOW))

    def test_club_names(self):
        self.assertEqual(a.club_name("Fc Rouen 1899"), "FC Rouen 1899")
        self.assertEqual(a.club_name("Qrm"), "QRM")
        self.assertEqual(a.club_name("Thionville Lusitanos"), "Thionville Lusitanos")


class AmiensScraperTests(unittest.TestCase):
    """Calendrier réel 2026-2027 : 2 matchs programmés un jeudi à 20:45, le reste à l'heure par défaut 14:45."""

    def setUp(self):
        schedule = {  # id: (slug, domicile, adversaire, date)
            "1": ("valenciennes-fc-amiens-scf", False, "Valenciennes Fc", at(4, 14, 45)),
            "2": ("amiens-scf-thionville-lusitanos", True, "Thionville Lusitanos", at(16, 20, 45)),
            "3": ("amiens-scf-sm-caen", True, "Sm Caen", at(37, 20, 45)),
            "4": ("amiens-scf-paris-13-atletico", True, "Paris 13 Atletico", at(60, 14, 45)),
            "5": ("amiens-scf-fc-rouen-1899", True, "Fc Rouen 1899", at(102, 14, 45)),
            "6": ("amiens-scf-sc-bastia", True, "Sc Bastia", at(130, 14, 45)),
            "7": ("amiens-scf-qrm", True, "Qrm", at(151, 14, 45)),
        }
        links = '<a href="/matchs/9-amiens-scf-us-orleans-45">2 – 0</a>' + "".join(
            f'<a href="/matchs/{i}-{slug}">à jouer</a>' for i, (slug, *_rest) in schedule.items())
        self.pages = {"club": soup(f"<html><body>{links}</body></html>")}
        for i, (slug, home, other, when) in schedule.items():
            teams = ("Amiens Scf", other) if home else (other, "Amiens Scf")
            self.pages[f"{a.FOOTDATA_BASE}/matchs/{i}-{slug}"] = soup(footdata_match(*teams, when))

    def fake_get(self, url):
        return self.pages["club"] if "/clubs/" in url else self.pages.get(url)

    def run_scraper(self):
        with mock.patch.object(a, "get_soup", side_effect=self.fake_get), \
             mock.patch.object(a, "_robots_allows", return_value=True), \
             mock.patch.object(a, "_previous_sources", return_value={}), \
             mock.patch.object(a, "_previous_amiens_events", return_value=[]), \
             mock.patch.object(a, "REQUEST_DELAY", 0):
            return a.scrape_amiens_sc()

    def test_only_scheduled_home_matches(self):
        events = self.run_scraper()
        self.assertEqual([e["adversaire"] for e in events], ["Thionville Lusitanos", "SM Caen"])
        first = events[0]
        self.assertEqual(first["club"], "Amiens SC")
        self.assertEqual(first["match"], "Amiens SC - Thionville Lusitanos")
        self.assertEqual(first["lieu"], a.AMIENS_SC_VENUE)
        self.assertEqual(first["billetterie"], a.AMIENS_SC_TICKETING)
        self.assertEqual(a.parse_iso(first["date"]), at(16, 20, 45))
        self.assertEqual(a.AMIENS_SC_STATS["a_confirmer"], 4)
        self.assertEqual(a.AMIENS_SC_STATS["horaire_par_defaut"], "14:45")

    def test_robots_refusal_is_a_failure_not_an_empty_list(self):
        with mock.patch.object(a, "_robots_allows", return_value=False), \
             mock.patch.object(a, "_previous_amiens_events", return_value=[]):
            self.assertIsNone(a.scrape_amiens_sc())


class GothiquesTests(unittest.TestCase):
    def setUp(self):
        self.dates = {7: at(3, 20, 15), 8: at(12, 18, 0), 36: at(23, 16, 0)}
        listing = (
            '<html><body><section><p>Prochain évènement</p><p>Le 16/10/2026 à 20h15</p>'
            '<a href="/fr/evenement/7">Réserver</a><a href="/fr/evenement/7/details">Voir les offres</a></section><section>'
            + gothiques_card(7, "Amiens", "Marseille", "Ven 16 Oct, 20h15")
            + gothiques_card(8, "Amiens", "Bordeaux", "Dim 25 Oct, 18h00")
            + gothiques_card(36, "Hungary", "Slovenia", "Jeu 5 Nov, 16h00")
            + "</section></body></html>")
        self.pages = {a.GOTHIQUES_TICKETING: soup(listing)}
        for number, when in self.dates.items():
            self.pages[f"{a.GOTHIQUES_TICKETING}/evenement/{number}/details"] = soup(gothiques_detail(when))
        self.requested = []

    def fake_get(self, url):
        self.requested.append(url)
        return self.pages.get(url)

    def test_home_matches_with_opponent(self):
        with mock.patch.object(a, "get_soup", side_effect=self.fake_get):
            events = a.scrape_gothiques({})
        self.assertEqual([e["adversaire"] for e in events], ["Marseille", "Bordeaux"])
        self.assertEqual(events[0]["match"], "Gothiques d'Amiens - Marseille")
        self.assertEqual(events[0]["billetterie"], f"{a.GOTHIQUES_TICKETING}/evenement/7/details")
        self.assertEqual(a.parse_iso(events[0]["date"]), self.dates[7])
        # Le match international n'est ni publié ni même ouvert.
        self.assertNotIn(f"{a.GOTHIQUES_TICKETING}/evenement/36/details", self.requested)

    def test_unrecognised_layout_keeps_previous_events(self):
        self.pages[a.GOTHIQUES_TICKETING] = soup('<a href="/fr/evenement/7/details">Voir</a>')
        with mock.patch.object(a, "get_soup", side_effect=self.fake_get):
            self.assertIsNone(a.scrape_gothiques({}))


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "evenements.json"
        self.patches = [mock.patch.object(a, "OUTPUT", self.output),
                        mock.patch.object(a, "MANUAL", Path(self.tmp.name) / "absent.json")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def event(club, ident, when):
        return {"id": ident, "club": club, "match": f"{club} - X", "adversaire": "X", "date": when.isoformat(),
                "lieu": "Amiens", "billetterie": "https://example.org", "source": "https://example.org", "statut": ""}

    def test_both_clubs_sorted_by_date(self):
        goth = [self.event(a.GOTHIQUES_CLUB, "gothiques-8", at(12, 18, 0)), self.event(a.GOTHIQUES_CLUB, "gothiques-7", at(3, 20, 15))]
        asc = [self.event(a.AMIENS_SC_CLUB, "amiens-sc-2", at(5, 20, 45))]
        with mock.patch.object(a, "scrape_gothiques", return_value=goth), \
             mock.patch.object(a, "scrape_amiens_sc", return_value=asc):
            self.assertEqual(a.main(), 0)
        data = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"updated_at", "sources", "avertissements", "evenements"})
        self.assertEqual([e["id"] for e in data["evenements"]], ["gothiques-7", "amiens-sc-2", "gothiques-8"])
        self.assertTrue(data["sources"][a.AMIENS_SC_CLUB]["ok"])

    def test_failed_source_keeps_previous_events(self):
        old = self.event(a.AMIENS_SC_CLUB, "amiens-sc-2", at(5, 20, 45))
        self.output.write_text(json.dumps({"evenements": [old]}), encoding="utf-8")
        with mock.patch.object(a, "scrape_gothiques", return_value=[]), \
             mock.patch.object(a, "scrape_amiens_sc", return_value=None):
            self.assertEqual(a.main(), 0)
        data = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual([e["id"] for e in data["evenements"]], ["amiens-sc-2"])
        self.assertFalse(data["sources"][a.AMIENS_SC_CLUB]["ok"])

    def test_all_sources_failed_leaves_file_untouched(self):
        self.output.write_text("{}", encoding="utf-8")
        with mock.patch.object(a, "scrape_gothiques", return_value=None), \
             mock.patch.object(a, "scrape_amiens_sc", return_value=None):
            self.assertEqual(a.main(), 1)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
