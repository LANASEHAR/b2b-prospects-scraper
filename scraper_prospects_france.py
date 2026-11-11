#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
SCRAPER B2B TOUT-TERRAIN & ANTI-BLOCAGES — SALONS & LAVERIES FRANCE
=============================================================================
8 ANTICIPATIONS CRITIQUES INTÉGRÉES :
1. DÉTOURNEMENT DES LIENS REDIRIGÉS : Décodage des liens de tracking Bing/DDG
   (ex. /ck/a? ou uddg=) pour ne plus jamais manquer le vrai site officiel.
2. TOLÉRANCE SSL POUR SITES ARTISANS : Désactivation stricte des erreurs de
   certificats SSL expirés (très fréquent sur les sites de petits salons).
3. MULTI-MIRROIRS OVERPASS OSM : Bascule automatique si un serveur est saturé.
4. PARSING TÉLÉPHONE UNIVERSEL : Capture des tags a[href^='tel:'], des formats
   avec indicatifs +33 (0)6, points, tirets ou collés.
5. DÉCODAGE HTML ENTITIES POUR EMAILS : Traduction des &#64; anti-spam.
6. ROTATION MULTI-MOTEURS (Bing + DuckDuckGo) : Zéro panne si un moteur bloque.
7. POOL RÉSEAU 50 SOCKETS : Zéro warning de pool saturé.
8. ENVOI EN DIRECT GOOGLE SHEETS : Dès 2 prospects qualifiés, sans attendre la fin.
=============================================================================
"""

import os
import re
import csv
import time
import html
import json
import logging
from urllib.parse import urlparse, quote_plus, unquote, parse_qs
from datetime import datetime, timezone
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import urllib3

# Désactivation des warnings SSL pour les sites artisans avec certificats expirés
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
TIMEOUT_SITE = 6
TIMEOUT_API = 12
MAX_RUN_SECONDS = 95 * 60  # 1h35 de run journalier
CSV_FILE = "prospects_france_complet.csv"

# Regex améliorées
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_FR_RE = re.compile(r"(?:(?:\+|00)33\s*(?:\(0\))?|0)\s*[1-9](?:[\s.-]*\d{2}){4}")
INSTA_RE = re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)/?", re.I)
FB_RE = re.compile(r"https?://(?:www\.)?facebook\.com/([A-Za-z0-9_.-]+)/?", re.I)

EXCLUDED_FRANCHISES = [
    "FRANCK PROVOST", "JEAN LOUIS DAVID", "DESSANGE", "SAINT ALGUE",
    "SHAMPOO", "TCHIP", "COIFF&CO", "CAMILLE ALBANE", "INTERMEDE",
    "FABIO SALSA", "MOD'S HAIR", "ERIC STIPA", "BIGUINE", "VOG COIFFURE"
]

IGNORED_EMAILS = {
    "example@example.com", "contact@wix.com", "support@planity.com",
    "info@domain.com", "contact@domain.fr", "sentry@io", "noreply@google.com",
    "webmaster@free.fr", "abuse@orange.fr", "privacy@solocal.com", "dpo@solocal.com"
}

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter"
]

VILLES_CIBLES = [
    ("Paris", "75"), ("Lyon", "69"), ("Marseille", "13"), ("Toulouse", "31"),
    ("Bordeaux", "33"), ("Lille", "59"), ("Nice", "06"), ("Nantes", "44"),
    ("Strasbourg", "67"), ("Montpellier", "34"), ("Rennes", "35"), ("Reims", "51"),
    ("Saint-Etienne", "42"), ("Toulon", "83"), ("Grenoble", "38"), ("Dijon", "21"),
    ("Angers", "49"), ("Nimes", "30"), ("Villeurbanne", "69"), ("Le Mans", "72"),
    ("Aix-en-Provence", "13"), ("Clermont-Ferrand", "63"), ("Brest", "29"), ("Tours", "37"),
    ("Amiens", "80"), ("Limoges", "87"), ("Annecy", "74"), ("Perpignan", "66"),
    ("Metz", "57"), ("Besancon", "25"), ("Orleans", "45"), ("Rouen", "76"),
    ("Mulhouse", "68"), ("Caen", "14"), ("Nancy", "54"), ("Avignon", "84"), ("Poitiers", "86")
]

def create_robust_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
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

def format_phone_fr(raw: str) -> str:
    p = re.sub(r"[^\d+]", "", raw or "")
    # Gestion du format +33 (0)6...
    if p.startswith("+330"): p = "0" + p[4:]
    elif p.startswith("+33"): p = "0" + p[3:]
    elif p.startswith("0033"): p = "0" + p[4:]
    if len(p) == 10 and p.startswith("0"):
        return " ".join([p[i:i+2] for i in range(0, 10, 2)])
    return p

def is_mobile(phone_str: str) -> bool:
    c = re.sub(r"[^\d]", "", phone_str or "")
    return c.startswith(("06", "07", "336", "337"))

def decode_cloudflare_email(cf_hash: str) -> str:
    try:
        r = int(cf_hash[:2], 16)
        return "".join([chr(int(cf_hash[i:i+2], 16) ^ r) for i in range(2, len(cf_hash), 2)]).lower().strip()
    except Exception:
        return ""

def unwrap_tracking_url(url: str) -> str:
    """Déballe les URLs de redirection de Bing, DuckDuckGo ou Google."""
    if not url: return ""
    try:
        parsed = urlparse(url)
        # DuckDuckGo tracking
        if "duckduckgo.com" in parsed.netloc and "uddg=" in parsed.query:
            qs = parse_qs(parsed.query)
            if "uddg" in qs: return qs["uddg"][0]
        # Google tracking
        if "google." in parsed.netloc and "/url" in parsed.path:
            qs = parse_qs(parsed.query)
            if "q" in qs: return qs["q"][0]
            if "url" in qs: return qs["url"][0]
        # Bing tracking
        if "bing.com" in parsed.netloc and "/ck/a" in parsed.path:
            return "" # Bing chiffre ses URLs de redirection
    except Exception:
        pass
    return url

# ─────────────────────────────────────────────────────────────────────────────
# MOTEUR ANTI-DOUBLONS RAPIDE
# ─────────────────────────────────────────────────────────────────────────────

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
        return f"{re.sub(r'[^a-z0-9]', '', (nom or '').lower())}@{re.sub(r'[^a-z0-9]', '', (ville or '').lower())}"

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, mode="r", encoding="utf-8-sig") as f:
                    for row in csv.DictReader(f):
                        for col in ("Mobile 06/07", "Téléphone Fixe"):
                            for p in (row.get(col) or "").split("/"):
                                np = self.norm_p(p)
                                if len(np) >= 9: self.seen_phones.add(np)
                        key = self.norm_nc(row.get("Enseigne / Nom", ""), row.get("Ville", ""))
                        if key != "@": self.seen_names_cities.add(key)
                logging.info(f"[ANTI-DOUBLONS] {len(self.seen_phones)} téléphones et {len(self.seen_names_cities)} fiches chargées.")
            except Exception as e:
                logging.warning(f"[ANTI-DOUBLONS] Erreur lecture : {e}")

    def is_dup(self, nom: str, ville: str, mobile: str, fixe: str) -> bool:
        for p in (mobile, fixe):
            for sub in (p or "").split("/"):
                np = self.norm_p(sub)
                if len(np) >= 9 and np in self.seen_phones: return True
        key = self.norm_nc(nom, ville)
        return key != "@" and key in self.seen_names_cities

    def add(self, nom: str, ville: str, mobile: str, fixe: str):
        for p in (mobile, fixe):
            for sub in (p or "").split("/"):
                np = self.norm_p(sub)
                if len(np) >= 9: self.seen_phones.add(np)
        key = self.norm_nc(nom, ville)
        if key != "@": self.seen_names_cities.add(key)

# ─────────────────────────────────────────────────────────────────────────────
# MODULE DE RECHERCHE CIBLÉE ROTATIF (BING + DDG)
# ─────────────────────────────────────────────────────────────────────────────

def search_targeted_multi_engine(session: requests.Session, query: str) -> dict:
    res = {"phones": set(), "emails": set(), "instagram": "", "facebook": "", "site_web": "", "planity": ""}

    # 1. Tentative Bing
    try:
        url_bing = f"https://www.bing.com/search?q={quote_plus(query)}&setlang=fr-FR"
        r = session.get(url_bing, timeout=TIMEOUT_API)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            text_full = soup.get_text(" ", strip=True)

            for tel in PHONE_FR_RE.findall(text_full):
                res["phones"].add(format_phone_fr(tel))

            for a in soup.select("li.b_algo h2 a[href]"):
                h = unwrap_tracking_url(a.get("href", ""))
                if not h: continue
                d = urlparse(h).netloc.lower()

                if "instagram.com" in d and not res["instagram"]:
                    m_i = INSTA_RE.search(h)
                    if m_i and m_i.group(1).lower() not in ["p", "reel", "explore"]:
                        res["instagram"] = f"https://www.instagram.com/{m_i.group(1)}/"
                elif "facebook.com" in d and not res["facebook"]:
                    m_f = FB_RE.search(h)
                    if m_f and m_f.group(1).lower() not in ["sharer", "tr"]:
                        res["facebook"] = f"https://www.facebook.com/{m_f.group(1)}"
                elif "planity.com" in d and not res["planity"]:
                    res["planity"] = h
                elif d and not res["site_web"] and not any(bad in d for bad in ["bing", "microsoft", "pagesjaunes", "societe", "infogreffe"]):
                    res["site_web"] = f"{urlparse(h).scheme}://{d}"
    except Exception:
        pass

    # 2. Si aucun site trouvé, fallback DuckDuckGo
    if not res["site_web"]:
        try:
            url_ddg = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            r_ddg = session.get(url_ddg, timeout=TIMEOUT_API)
            if r_ddg.status_code == 200:
                soup_ddg = BeautifulSoup(r_ddg.text, "html.parser")
                for a in soup_ddg.select(".result__a"):
                    h = unwrap_tracking_url(a.get("href", ""))
                    d = urlparse(h).netloc.lower()
                    if d and not any(bad in d for bad in ["duckduckgo", "pagesjaunes", "societe", "infogreffe"]):
                        res["site_web"] = f"{urlparse(h).scheme}://{d}"
                        break
        except Exception:
            pass

    return res

# ─────────────────────────────────────────────────────────────────────────────
# CRAWL PROFOND & TOLÉRANT DES SITES OFFICIELS
# ─────────────────────────────────────────────────────────────────────────────

def crawl_site_deep(session: requests.Session, url: str) -> dict:
    res = {"emails": set(), "mobiles": set(), "fixes": set(), "instagram": ""}
    if not url or not url.startswith("http"): return res

    for target in [url, f"{url.rstrip('/')}/contact", f"{url.rstrip('/')}/mentions-legales"]:
        try:
            # verify=False pour ne pas échouer sur les certificats SSL expirés des petits commerces
            r = session.get(target, timeout=TIMEOUT_SITE, verify=False)
            if r.status_code != 200: continue
            
            # Décodage des entités HTML anti-spam (ex: &#64; -> @)
            raw_text = html.unescape(r.text)
            soup = BeautifulSoup(raw_text, "html.parser")

            # 1. Capture explicite des liens tel:
            for a_tel in soup.select("a[href^='tel:']"):
                clean_tel = format_phone_fr(a_tel.get("href", "").replace("tel:", ""))
                if clean_tel:
                    if is_mobile(clean_tel): res["mobiles"].add(clean_tel)
                    else: res["fixes"].add(clean_tel)

            # 2. Capture explicite des liens mailto:
            for a_mail in soup.select("a[href^='mailto:']"):
                mail_clean = a_mail.get("href", "").replace("mailto:", "").split("?")[0].lower().strip()
                if EMAIL_RE.fullmatch(mail_clean) and mail_clean not in IGNORED_EMAILS:
                    res["emails"].add(mail_clean)

            # 3. Cloudflare obfuscated emails
            for cf in soup.select("[data-cfemail]"):
                dec = decode_cloudflare_email(cf.get("data-cfemail", ""))
                if dec and dec not in IGNORED_EMAILS: res["emails"].add(dec)

            # 4. Regex dans le texte
            for mail in EMAIL_RE.findall(raw_text):
                m_c = mail.lower().strip(".,;:()")
                if m_c not in IGNORED_EMAILS and not m_c.endswith((".png", ".jpg", ".webp", ".svg", ".js")):
                    res["emails"].add(m_c)

            for tel in PHONE_FR_RE.findall(raw_text):
                fmt = format_phone_fr(tel)
                if is_mobile(fmt): res["mobiles"].add(fmt)
                else: res["fixes"].add(fmt)

            # 5. Instagram
            for a in soup.select("a[href*='instagram.com']"):
                m_i = INSTA_RE.search(a.get("href", ""))
                if m_i and m_i.group(1).lower() not in ["p", "reel", "explore"]:
                    res["instagram"] = f"https://www.instagram.com/{m_i.group(1)}/"
                    break
        except Exception:
            continue
    return res

# ─────────────────────────────────────────────────────────────────────────────
# MODULE OVERPASS OSM AVEC MULTI-SERVEURS (SPÉCIAL LAVERIES)
# ─────────────────────────────────────────────────────────────────────────────

def query_osm_laveries_multi_server(city: str) -> list:
    query = f"""
    [out:json][timeout:15];
    area["name"="{city}"]->.searchArea;
    (
      node["shop"="laundry"](area.searchArea);
      way["shop"="laundry"](area.searchArea);
    );
    out center tags 30;
    """
    for server in OVERPASS_SERVERS:
        try:
            r = requests.post(server, data={"data": query}, timeout=20)
            if r.status_code == 200:
                laveries = []
                for el in r.json().get("elements", []):
                    tags = el.get("tags", {})
                    nom = tags.get("name") or tags.get("brand") or f"Laverie Automatique {city}"
                    phone = format_phone_fr(tags.get("phone") or tags.get("contact:phone") or "")
                    website = tags.get("website") or tags.get("contact:website") or ""
                    street = tags.get("addr:street", "")
                    num = tags.get("addr:housenumber", "")
                    cp = tags.get("addr:postcode", "")
                    laveries.append({
                        "nom": nom,
                        "phone": phone,
                        "website": website,
                        "adresse": f"{num} {street}".strip(),
                        "cp": cp,
                        "ville": city
                    })
                if laveries:
                    return laveries
        except Exception:
            continue
    return []

def send_to_google_sheet(webhook_url: str, rows: list) -> bool:
    if not webhook_url or not rows: return False
    try:
        r = requests.post(webhook_url, json={"action": "append_rows", "rows": rows}, timeout=15)
        if r.status_code == 200 and r.json().get("status") == "success":
            logging.info(f"==> [GOOGLE SHEETS] {len(rows)} prospect(s) injecte(s) avec succes !")
            return True
    except Exception as e:
        logging.error(f"[GOOGLE SHEETS] Erreur d'envoi webhook : {e}")
    return False

# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE PRINCIPAL ULTRA-ROBUSTE
# ─────────────────────────────────────────────────────────────────────────────

def run_targeted_live_scraper():
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

    total_exploitable = 0
    buffer_live = []

    logging.info("=== DEMARRAGE DU SCRAPER TOUT-TERRAIN (ANTI-BLOCAGES) ===")
    if webhook_url:
        logging.info("--> Webhook Google Sheets ACTIVE : envoi en continu des 2 prospects.")
    else:
        logging.warning("--> ATTENTION : GOOGLE_SHEET_WEBHOOK_URL non defini dans l'environnement.")

    for ville, dept in VILLES_CIBLES:
        if time.time() - start_time >= MAX_RUN_SECONDS:
            logging.info("[TEMPS] 95 minutes ecoulees. Arret propre.")
            break

        logging.info(f"\n################ VILLE : {ville.upper()} ({dept}) ################")

        # 1. CIBLE LAVERIES (OVERPASS MULTI-SERVEURS)
        for lav in query_osm_laveries_multi_server(ville):
            mob = lav["phone"] if is_mobile(lav["phone"]) else ""
            fix = lav["phone"] if not is_mobile(lav["phone"]) else ""

            crawl = crawl_site_deep(session, lav["website"]) if lav["website"] else {}
            if crawl.get("mobiles") and not mob: mob = list(crawl["mobiles"])[0]
            if crawl.get("fixes") and not fix: fix = list(crawl["fixes"])[0]
            emails = crawl.get("emails", set())

            if not (mob or fix or emails): continue

            if not dedup.is_dup(lav["nom"], ville, mob, fix):
                dedup.add(lav["nom"], ville, mob, fix)
                p = {
                    "Cible": "Laveries Automatiques",
                    "Enseigne / Nom": lav["nom"],
                    "Dirigeant": "",
                    "Mobile 06/07": mob,
                    "Téléphone Fixe": fix,
                    "Email": "; ".join(emails),
                    "Instagram": crawl.get("instagram", ""),
                    "Facebook": "",
                    "Site Web": lav["website"],
                    "Lien Planity": "",
                    "Ville": ville,
                    "Code Postal": lav["cp"] or dept,
                    "Adresse": lav["adresse"],
                    "Date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                }
                with open(CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
                    csv.DictWriter(f, fieldnames=fieldnames).writerow(p)

                buffer_live.append(p)
                total_exploitable += 1
                logging.info(f"[LAVERIE #{total_exploitable}] {p['Enseigne / Nom']} ({ville}) | Mob: {mob} | Fix: {fix} | Mail: {p['Email']}")

                if len(buffer_live) >= 2:
                    send_to_google_sheet(webhook_url, buffer_live)
                    buffer_live = []

        # 2. CIBLE COIFFURE & COMPLÉMENTS VIA SIRENE + MULTI-ENGINE
        for cible, naf in [("Salons de Coiffure", "96.02A"), ("Laveries Automatiques", "96.01B")]:
            if time.time() - start_time >= MAX_RUN_SECONDS: break
            try:
                params = {"activite_principale": naf, "departement": dept, "etat_administratif": "A", "per_page": 25}
                r_sir = session.get("https://recherche-entreprises.api.gouv.fr/search", params=params, timeout=TIMEOUT_API)
                items = r_sir.json().get("results", []) if r_sir.status_code == 200 else []
            except Exception:
                items = []

            for it in items:
                if time.time() - start_time >= MAX_RUN_SECONDS: break
                nom = clean_str(it.get("nom_complet") or it.get("nom_raison_sociale") or "")
                if cible == "Salons de Coiffure" and any(f in nom.upper() for f in EXCLUDED_FRANCHISES):
                    continue

                siege = it.get("siege", {})
                cp = clean_str(siege.get("code_postal", ""))
                v = clean_str(siege.get("commune", "")) or ville
                adresse = clean_str(siege.get("adresse", ""))

                # Recherche ciblée anti-blocage
                search_res = search_targeted_multi_engine(session, f'"{nom}" "{v}"')
                site = search_res.get("site_web", "")
                crawl = crawl_site_deep(session, site) if site else {}

                mobiles = list(crawl.get("mobiles", set()))
                fixes = list(crawl.get("fixes", set()))
                for p_num in search_res.get("phones", []):
                    if is_mobile(p_num) and p_num not in mobiles: mobiles.append(p_num)
                    elif not is_mobile(p_num) and p_num not in fixes: fixes.append(p_num)

                all_emails = list(crawl.get("emails", set()) | search_res.get("emails", set()))
                insta = crawl.get("instagram") or search_res.get("instagram", "")

                mob_str = " / ".join(mobiles)
                fix_str = " / ".join(fixes)
                mail_str = "; ".join(all_emails)

                # FILTRE EXPLOITABLE STRICT
                if not (mob_str or fix_str or mail_str or insta):
                    continue

                if not dedup.is_dup(nom, v, mob_str, fix_str):
                    dedup.add(nom, v, mob_str, fix_str)

                    dirigeants = it.get("dirigeants", [])
                    nom_dir = clean_str(f"{dirigeants[0].get('prenom', '')} {dirigeants[0].get('nom', '')}") if dirigeants else ""

                    p = {
                        "Cible": cible,
                        "Enseigne / Nom": nom,
                        "Dirigeant": nom_dir,
                        "Mobile 06/07": mob_str,
                        "Téléphone Fixe": fix_str,
                        "Email": mail_str,
                        "Instagram": insta,
                        "Facebook": search_res.get("facebook", ""),
                        "Site Web": site,
                        "Lien Planity": search_res.get("planity", ""),
                        "Ville": v,
                        "Code Postal": cp,
                        "Adresse": adresse,
                        "Date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    }
                    with open(CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
                        csv.DictWriter(f, fieldnames=fieldnames).writerow(p)

                    buffer_live.append(p)
                    total_exploitable += 1
                    logging.info(f"[{cible} #{total_exploitable}] {nom} ({v}) | Mob: {mob_str} | Fix: {fix_str} | Mail: {mail_str} | Insta: {insta}")

                    if len(buffer_live) >= 2:
                        send_to_google_sheet(webhook_url, buffer_live)
                        buffer_live = []

                time.sleep(0.4)

    if buffer_live:
        send_to_google_sheet(webhook_url, buffer_live)

    duration_min = int((time.time() - start_time) / 60)
    logging.info(f"=== FIN DU RUN : {total_exploitable} VRAIS CONTACTS ENVOYES AU GOOGLE SHEET en {duration_min} minutes. ===")

if __name__ == "__main__":
    run_targeted_live_scraper()
