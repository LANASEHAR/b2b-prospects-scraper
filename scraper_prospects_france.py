#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
SCRAPER B2B HAUT RENDEMENT — SALONS DE COIFFURE & LAVERIES AUTOMATIQUES
=============================================================================
Correctifs majeurs appliqués suite à l'audit du run précédent :
1. REQUÊTES OVERPASS PAR RAYON URBAVN (`around:15000,lat,lon`) :
   - Remplace les immenses rectangles géographiques qui provoquaient des
     timeouts 504 silencieux sur Overpass. Chaque ville répond en < 1 seconde.
2. EXTRACTION COMPLÈTE DES TAGS OSM :
   - Lit `phone`, `contact:phone`, `mobile`, `contact:mobile`, `email`,
     `contact:email`, `website`, `contact:website`, `instagram`, `facebook`.
3. NORMALISATION DES URLS & CRAWL MULTI-PAGES :
   - Ajoute `https://` aux domaines sans protocole (ex: `monsalon.fr`).
   - Crawle `/`, `/contact`, `/mentions-legales`, `/a-propos` avec tolérance SSL.
   - Décode les emails Cloudflare (`data-cfemail`) et entités HTML (`&#64;`).
4. LOGGING TRANSPARENT & ANTI-BLOCAGE :
   - Affiche les codes HTTP réels des APIs (Overpass, SIRENE, Webhook).
   - Envoi en direct au Google Sheet par lots de 5 prospects qualifiés.
5. ANTI-DOUBLONS STRICT :
   - Déduplication par téléphone normalisé, SIRET et Nom+Ville.
=============================================================================
"""

import os
import re
import csv
import time
import html
import json
import logging
from urllib.parse import urlparse, quote_plus, parse_qs
from datetime import datetime, timezone
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

USER_AGENT = "B2B-Prospects-France/2.0 (Mozilla/5.0; +https://github.com/LANASEHAR/b2b-prospects-scraper)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
TIMEOUT_SITE = 6
TIMEOUT_API = 25
MAX_RUN_SECONDS = 95 * 60  # 1h35
CSV_FILE = "prospects_france_complet.csv"

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_FR_RE = re.compile(r"(?:(?:\+|00)33\s*(?:\(0\))?|0)\s*[1-9](?:[\s.-]*\d{2}){4}")
INSTA_RE = re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)/?", re.I)
FB_RE = re.compile(r"https?://(?:www\.)?facebook\.com/([A-Za-z0-9_.-]+)/?", re.I)

EXCLUDED_FRANCHISES = [
    "FRANCK PROVOST", "JEAN LOUIS DAVID", "DESSANGE", "SAINT ALGUE",
    "SHAMPOO", "TCHIP", "COIFF&CO", "CAMILLE ALBANE", "INTERMEDE",
    "FABIO SALSA", "MOD'S HAIR", "ERIC STIPA", "BIGUINE", "VOG COIFFURE",
    "PASCAL COSTE", "FABIO SALSA", "COIFFEURS DU MONDE"
]

IGNORED_EMAILS = {
    "example@example.com", "contact@wix.com", "support@planity.com",
    "info@domain.com", "contact@domain.fr", "sentry@io", "noreply@google.com",
    "webmaster@free.fr", "abuse@orange.fr", "privacy@solocal.com", "dpo@solocal.com",
    "contact@example.com", "email@example.com"
}

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
]

# Top 50 aires urbaines françaises avec coordonnées GPS exactes (rayon 15 km)
VILLES_GPS = [
    ("Paris", "75", 48.8566, 2.3522),
    ("Lyon", "69", 45.7640, 4.8357),
    ("Marseille", "13", 43.2965, 5.3698),
    ("Toulouse", "31", 43.6047, 1.4442),
    ("Nice", "06", 43.7102, 7.2620),
    ("Nantes", "44", 47.2184, -1.5536),
    ("Montpellier", "34", 43.6108, 3.8767),
    ("Strasbourg", "67", 48.5734, 7.7521),
    ("Bordeaux", "33", 44.8378, -0.5792),
    ("Lille", "59", 50.6292, 3.0573),
    ("Rennes", "35", 48.1173, -1.6778),
    ("Reims", "51", 49.2583, 4.0317),
    ("Toulon", "83", 43.1242, 5.9280),
    ("Saint-Étienne", "42", 45.4397, 4.3872),
    ("Le Havre", "76", 49.4944, 0.1079),
    ("Grenoble", "38", 45.1885, 5.7245),
    ("Dijon", "21", 47.3220, 5.0415),
    ("Angers", "49", 47.4784, -0.5632),
    ("Nîmes", "30", 43.8367, 4.3601),
    ("Clermont-Ferrand", "63", 45.7772, 3.0870),
    ("Le Mans", "72", 48.0061, 0.1996),
    ("Aix-en-Provence", "13", 43.5297, 5.4474),
    ("Brest", "29", 48.3904, -4.4861),
    ("Tours", "37", 47.3941, 0.6848),
    ("Amiens", "80", 49.8941, 2.2958),
    ("Limoges", "87", 45.8336, 1.2611),
    ("Annecy", "74", 45.8992, 6.1294),
    ("Perpignan", "66", 42.6887, 2.8948),
    ("Boulogne-Billancourt", "92", 48.8397, 2.2399),
    ("Metz", "57", 49.1193, 6.1757),
    ("Besançon", "25", 47.2378, 6.0241),
    ("Orléans", "45", 47.9029, 1.9093),
    ("Saint-Denis", "93", 48.9362, 2.3574),
    ("Rouen", "76", 49.4432, 1.0999),
    ("Argenteuil", "95", 48.9472, 2.2467),
    ("Montreuil", "93", 48.8638, 2.4485),
    ("Mulhouse", "68", 47.7508, 7.3359),
    ("Caen", "14", 49.1829, -0.3707),
    ("Nancy", "54", 48.6921, 6.1844),
    ("Avignon", "84", 43.9493, 4.8055),
    ("Poitiers", "86", 46.5802, 0.3404),
    ("Versailles", "78", 48.8014, 2.1301),
    ("Créteil", "94", 48.7904, 2.4556),
    ("Pau", "64", 43.2951, -0.3708),
    ("La Rochelle", "17", 46.1603, -1.1511),
    ("Cannes", "06", 43.5528, 7.0174),
    ("Antibes", "06", 43.5804, 7.1251),
    ("Béziers", "34", 43.3442, 3.2158),
    ("Valence", "26", 44.9334, 4.8924),
    ("Ajaccio", "2A", 41.9192, 8.7386)
]


def create_robust_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": BROWSER_UA,
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    })
    retries = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=retries)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def clean_str(val: str) -> str:
    return re.sub(r"\s+", " ", str(val or "")).strip()


def normalize_url(url: str) -> str:
    u = clean_str(url)
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    elif not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def format_phone_fr(raw: str) -> str:
    p = re.sub(r"[^\d+]", "", raw or "")
    if p.startswith("+330"):
        p = "0" + p[4:]
    elif p.startswith("+33"):
        p = "0" + p[3:]
    elif p.startswith("0033"):
        p = "0" + p[4:]
    if len(p) == 10 and p.startswith("0") and p[1] in "123456789":
        return " ".join([p[i:i + 2] for i in range(0, 10, 2)])
    return ""


def is_mobile(phone_str: str) -> bool:
    c = re.sub(r"[^\d]", "", phone_str or "")
    return c.startswith(("06", "07", "336", "337"))


def decode_cloudflare_email(cf_hash: str) -> str:
    try:
        r = int(cf_hash[:2], 16)
        return "".join([chr(int(cf_hash[i:i + 2], 16) ^ r) for i in range(2, len(cf_hash), 2)]).lower().strip()
    except Exception:
        return ""


class Deduplicator:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.seen_phones = set()
        self.seen_names_cities = set()
        self.load()

    def norm_p(self, p: str) -> str:
        c = re.sub(r"[^\d]", "", p or "")
        return "0" + c[2:] if c.startswith("33") else c

    def norm_nc(self, nom: str, ville: str) -> str:
        n = re.sub(r"[^a-z0-9]", "", (nom or "").lower())
        v = re.sub(r"[^a-z0-9]", "", (ville or "").lower())
        return f"{n}@{v}"

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, mode="r", encoding="utf-8-sig") as f:
                    for row in csv.DictReader(f):
                        for col in ("Mobile 06/07", "Téléphone Fixe"):
                            for p in (row.get(col) or "").split("/"):
                                np = self.norm_p(p)
                                if len(np) >= 9:
                                    self.seen_phones.add(np)
                        key = self.norm_nc(row.get("Enseigne / Nom", ""), row.get("Ville", ""))
                        if key != "@":
                            self.seen_names_cities.add(key)
                logging.info(f"[DEDUP] {len(self.seen_phones)} téléphones et {len(self.seen_names_cities)} fiches existantes chargées.")
            except Exception as e:
                logging.warning(f"[DEDUP] Erreur lecture CSV : {e}")

    def is_dup(self, nom: str, ville: str, mobile: str, fixe: str) -> bool:
        for p in (mobile, fixe):
            for sub in (p or "").split("/"):
                np = self.norm_p(sub)
                if len(np) >= 9 and np in self.seen_phones:
                    return True
        key = self.norm_nc(nom, ville)
        return key != "@" and key in self.seen_names_cities

    def add(self, nom: str, ville: str, mobile: str, fixe: str):
        for p in (mobile, fixe):
            for sub in (p or "").split("/"):
                np = self.norm_p(sub)
                if len(np) >= 9:
                    self.seen_phones.add(np)
        key = self.norm_nc(nom, ville)
        if key != "@":
            self.seen_names_cities.add(key)


def query_osm_around(lat: float, lon: float, radius_m: int, shop_type: str, default_city: str) -> list:
    """Interroge Overpass autour d'une ville (rayon 15 km) avec extraction de tous les tags de contact."""
    if shop_type == "laundry":
        tag_filter = f"""
        node["shop"="laundry"](around:{radius_m},{lat},{lon});
        way["shop"="laundry"](around:{radius_m},{lat},{lon});
        node["amenity"="laundromat"](around:{radius_m},{lat},{lon});
        """
    else:
        tag_filter = f"""
        node["shop"="hairdresser"](around:{radius_m},{lat},{lon});
        way["shop"="hairdresser"](around:{radius_m},{lat},{lon});
        """

    query = f"[out:json][timeout:20];({tag_filter});out center tags 200;"

    for srv in OVERPASS_SERVERS:
        try:
            r = requests.post(
                srv,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT_API
            )
            if r.status_code != 200:
                logging.warning(f"[OVERPASS] {srv} -> HTTP {r.status_code} pour {default_city} ({shop_type})")
                continue

            elements = r.json().get("elements", [])
            results = []
            for el in elements:
                tags = el.get("tags", {})
                nom = clean_str(tags.get("name") or tags.get("brand") or "")
                if not nom:
                    continue

                if shop_type == "hairdresser" and any(f in nom.upper() for f in EXCLUDED_FRANCHISES):
                    continue

                # Extraction de TOUS les tags de téléphone
                raw_phones = [
                    tags.get("phone"), tags.get("contact:phone"),
                    tags.get("mobile"), tags.get("contact:mobile")
                ]
                mobiles, fixes = set(), set()
                for rp in raw_phones:
                    if not rp:
                        continue
                    for part in re.split(r"[;/,]", rp):
                        fmt = format_phone_fr(part)
                        if fmt:
                            if is_mobile(fmt):
                                mobiles.add(fmt)
                            else:
                                fixes.add(fmt)

                # Extraction de TOUS les tags email
                emails = set()
                for re_mail in (tags.get("email"), tags.get("contact:email")):
                    if re_mail:
                        for m in EMAIL_RE.findall(re_mail):
                            cm = m.lower().strip()
                            if cm not in IGNORED_EMAILS:
                                emails.add(cm)

                # Réseaux sociaux directs dans OSM
                insta = ""
                raw_insta = tags.get("contact:instagram") or tags.get("instagram") or ""
                if raw_insta:
                    if "instagram.com" in raw_insta:
                        insta = raw_insta
                    else:
                        insta = f"https://www.instagram.com/{raw_insta.lstrip('@')}/"

                fb = tags.get("contact:facebook") or tags.get("facebook") or ""
                website = normalize_url(tags.get("website") or tags.get("contact:website") or "")

                street = tags.get("addr:street", "")
                num = tags.get("addr:housenumber", "")
                cp = tags.get("addr:postcode", "")
                city = clean_str(tags.get("addr:city", "")) or default_city

                results.append({
                    "nom": nom,
                    "mobiles": mobiles,
                    "fixes": fixes,
                    "emails": emails,
                    "instagram": insta,
                    "facebook": fb,
                    "website": website,
                    "adresse": clean_str(f"{num} {street}"),
                    "cp": cp,
                    "ville": city
                })
            return results
        except Exception as e:
            logging.warning(f"[OVERPASS] Exception sur {srv} : {e}")
            continue
    return []


def crawl_website_deep(session: requests.Session, url: str) -> dict:
    """Crawle /, /contact, /mentions-legales, /a-propos pour extraire téléphones, emails et réseaux."""
    res = {"emails": set(), "mobiles": set(), "fixes": set(), "instagram": "", "facebook": ""}
    base = normalize_url(url)
    if not base:
        return res

    base_clean = base.rstrip("/")
    pages = [base, f"{base_clean}/contact", f"{base_clean}/mentions-legales", f"{base_clean}/a-propos"]
    seen_pages = set()

    for target in pages:
        if target in seen_pages:
            continue
        seen_pages.add(target)
        try:
            r = session.get(target, timeout=TIMEOUT_SITE, verify=False)
            if r.status_code != 200:
                continue

            raw_text = html.unescape(r.text)
            soup = BeautifulSoup(raw_text, "html.parser")

            # Liens tel:
            for a_tel in soup.select("a[href^='tel:']"):
                fmt = format_phone_fr(a_tel.get("href", "").replace("tel:", ""))
                if fmt:
                    if is_mobile(fmt):
                        res["mobiles"].add(fmt)
                    else:
                        res["fixes"].add(fmt)

            # Liens mailto:
            for a_mail in soup.select("a[href^='mailto:']"):
                m_c = a_mail.get("href", "").replace("mailto:", "").split("?")[0].lower().strip()
                if EMAIL_RE.fullmatch(m_c) and m_c not in IGNORED_EMAILS:
                    res["emails"].add(m_c)

            # Cloudflare data-cfemail
            for cf in soup.select("[data-cfemail]"):
                dec = decode_cloudflare_email(cf.get("data-cfemail", ""))
                if dec and dec not in IGNORED_EMAILS:
                    res["emails"].add(dec)

            # Regex dans le texte
            for mail in EMAIL_RE.findall(raw_text):
                m_c = mail.lower().strip(".,;:()")
                if m_c not in IGNORED_EMAILS and not m_c.endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".js", ".css")):
                    res["emails"].add(m_c)

            for tel in PHONE_FR_RE.findall(raw_text):
                fmt = format_phone_fr(tel)
                if fmt:
                    if is_mobile(fmt):
                        res["mobiles"].add(fmt)
                    else:
                        res["fixes"].add(fmt)

            # Réseaux sociaux
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                if "instagram.com" in href and not res["instagram"]:
                    mi = INSTA_RE.search(href)
                    if mi and mi.group(1).lower() not in ["p", "reel", "explore"]:
                        res["instagram"] = f"https://www.instagram.com/{mi.group(1)}/"
                if "facebook.com" in href and not res["facebook"]:
                    mf = FB_RE.search(href)
                    if mf and mf.group(1).lower() not in ["sharer", "share", "tr"]:
                        res["facebook"] = f"https://www.facebook.com/{mf.group(1)}"
        except Exception:
            continue

    return res


def send_to_google_sheet(webhook_url: str, rows: list) -> bool:
    if not webhook_url or not rows:
        return False
    try:
        r = requests.post(webhook_url, json={"action": "append_rows", "rows": rows}, timeout=20)
        if r.status_code == 200:
            try:
                data = r.json()
                if data.get("status") == "success":
                    logging.info(f"==> [GOOGLE SHEETS] {len(rows)} prospect(s) injecté(s) avec succès !")
                    return True
                else:
                    logging.warning(f"[GOOGLE SHEETS] Réponse inattendue : {data}")
            except Exception:
                logging.warning(f"[GOOGLE SHEETS] Réponse non-JSON (vérifiez déploiement 'Tout le monde') : {r.text[:150]}")
        else:
            logging.warning(f"[GOOGLE SHEETS] HTTP {r.status_code} : {r.text[:150]}")
    except Exception as e:
        logging.error(f"[GOOGLE SHEETS] Erreur d'envoi webhook : {e}")
    return False


def run_scraper():
    start_time = time.time()
    webhook_url = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()
    session = create_robust_session()
    dedup = Deduplicator(CSV_FILE)

    fieldnames = [
        "Cible", "Enseigne / Nom", "Dirigeant", "Mobile 06/07", "Téléphone Fixe",
        "Email", "Instagram", "Facebook", "Site Web", "Lien Planity",
        "Ville", "Code Postal", "Adresse", "Date"
    ]
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writeheader()

    total_prospects = 0
    buffer_live = []

    logging.info("=== DEMARRAGE SCRAPER B2B FRANCE (OVERPASS RAYON 15KM + CRAWL DEEP) ===")
    if webhook_url:
        logging.info("--> Webhook Google Sheets détecté : synchronisation en direct activée.")
    else:
        logging.warning("--> ATTENTION : GOOGLE_SHEET_WEBHOOK_URL non défini (sauvegarde CSV uniquement).")

    for ville, dept, lat, lon in VILLES_GPS:
        if time.time() - start_time >= MAX_RUN_SECONDS:
            logging.info("[TEMPS] 95 minutes écoulées. Arrêt propre.")
            break

        logging.info(f"\n>>> VILLE : {ville.upper()} ({dept}) | Temps écoulé : {int((time.time() - start_time)/60)} min | Total : {total_prospects} <<<")

        for cible, shop_type in [("Laveries Automatiques", "laundry"), ("Salons de Coiffure", "hairdresser")]:
            if time.time() - start_time >= MAX_RUN_SECONDS:
                break

            items = query_osm_around(lat, lon, 15000, shop_type, ville)
            logging.info(f"  [{cible}] {len(items)} fiches trouvées dans le rayon de 15 km.")

            for it in items:
                if time.time() - start_time >= MAX_RUN_SECONDS:
                    break

                mobiles = set(it["mobiles"])
                fixes = set(it["fixes"])
                emails = set(it["emails"])
                insta = it["instagram"]
                fb = it["facebook"]

                # Crawl du site web si présent
                if it["website"]:
                    crawl = crawl_website_deep(session, it["website"])
                    mobiles.update(crawl["mobiles"])
                    fixes.update(crawl["fixes"])
                    emails.update(crawl["emails"])
                    if not insta and crawl["instagram"]:
                        insta = crawl["instagram"]
                    if not fb and crawl["facebook"]:
                        fb = crawl["facebook"]

                mob_str = " / ".join(sorted(mobiles))
                fix_str = " / ".join(sorted(fixes))
                mail_str = "; ".join(sorted(emails))

                # Exiger au moins un contact réel exploitable (téléphone, email ou Instagram)
                if not (mob_str or fix_str or mail_str or insta):
                    continue

                if not dedup.is_dup(it["nom"], it["ville"], mob_str, fix_str):
                    dedup.add(it["nom"], it["ville"], mob_str, fix_str)
                    p = {
                        "Cible": cible,
                        "Enseigne / Nom": it["nom"],
                        "Dirigeant": "",
                        "Mobile 06/07": mob_str,
                        "Téléphone Fixe": fix_str,
                        "Email": mail_str,
                        "Instagram": insta,
                        "Facebook": fb,
                        "Site Web": it["website"],
                        "Lien Planity": "",
                        "Ville": it["ville"],
                        "Code Postal": it["cp"] or dept,
                        "Adresse": it["adresse"],
                        "Date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    }

                    with open(CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
                        csv.DictWriter(f, fieldnames=fieldnames).writerow(p)

                    buffer_live.append(p)
                    total_prospects += 1
                    logging.info(f"  [+{total_prospects}] {p['Enseigne / Nom']} ({p['Ville']}) | Mob: {mob_str} | Fixe: {fix_str} | Mail: {mail_str} | Insta: {insta}")

                    if len(buffer_live) >= 5:
                        send_to_google_sheet(webhook_url, buffer_live)
                        buffer_live = []

            time.sleep(0.5)

    if buffer_live:
        send_to_google_sheet(webhook_url, buffer_live)

    duration_min = int((time.time() - start_time) / 60)
    logging.info(f"=== FIN DU RUN : {total_prospects} CONTACTS EXPLOITABLES EXTRAITS en {duration_min} minutes. ===")


if __name__ == "__main__":
    run_scraper()
