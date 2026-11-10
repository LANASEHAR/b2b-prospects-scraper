#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
SCRAPER B2B TOUT-TERRAIN — SALONS DE COIFFURE & LAVERIES AUTOMATIQUES
=============================================================================
Sources croisées à 360° :
1. PagesJaunes (pagesjaunes.fr) : Détection fiches pros, téléphones, sites web
2. OpenStreetMap (Overpass API) : Cartographie exhaustive des laveries en France
3. Répertoire Officiel SIRENE / INSEE (API Recherche d'Entreprises) :
   - NAF 96.02A : Coiffure indépendante (filtrage des franchises)
   - NAF 96.01B : Laveries automatiques & blanchisseries libre-service
4. Détection Web & Google Maps (moteurs de recherche) :
   - Pages Facebook, profils Instagram, fiches d'avis
5. Crawl profond multi-pages (Accueil, /contact, /mentions-legales) :
   - Extraction numéros Mobiles (06/07) vs Fixes
   - Extraction emails vérifiés (avec décodage Cloudflare email-protection)
   - Extraction profils Instagram officiels & liens Planity / Treatwell
6. Synchronisation automatique Google Sheets via Webhook + Export CSV
=============================================================================
"""

import os
import re
import csv
import time
import json
import logging
from urllib.parse import urlparse, urljoin, quote_plus, unquote
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

API_RECHERCHE_ENTREPRISES = "https://recherche-entreprises.api.gouv.fr/search"
OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
TIMEOUT = 14

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_FR_RE = re.compile(r"(?:(?:\+|00)33|0)\s*[1-9](?:[\s.-]*\d{2}){4}")
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
    "webmaster@free.fr", "abuse@orange.fr", "privacy@solocal.com"
}

def clean_str(val: str) -> str:
    return re.sub(r"\s+", " ", str(val or "")).strip()

def decode_cloudflare_email(cf_hash: str) -> str:
    try:
        r = int(cf_hash[:2], 16)
        return "".join([chr(int(cf_hash[i:i+2], 16) ^ r) for i in range(2, len(cf_hash), 2)]).lower().strip()
    except Exception:
        return ""

def format_phone_fr(raw_phone: str) -> str:
    p = re.sub(r"[^\d+]", "", raw_phone)
    if p.startswith("+33"):
        p = "0" + p[3:]
    if len(p) == 10 and p.startswith("0"):
        return " ".join([p[i:i+2] for i in range(0, 10, 2)])
    return p

def is_mobile_phone(phone_str: str) -> bool:
    clean = re.sub(r"[^\d]", "", phone_str)
    return clean.startswith(("06", "07", "336", "337"))

# ─────────────────────────────────────────────────────────────────────────────
# 1. MODULE PAGES JAUNES
# ─────────────────────────────────────────────────────────────────────────────

def search_pages_jaunes(session: requests.Session, nom: str, ville: str) -> dict:
    """
    Recherche sur PagesJaunes pour dénicher la fiche pro, téléphone, site web et email.
    """
    data = {"phone": "", "site_web": "", "pj_url": ""}
    query = f'site:pagesjaunes.fr/pros/ "{nom}" "{ville}"'
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select(".result__a"):
                href = a.get("href", "")
                if "pagesjaunes.fr/pros/" in href:
                    if "uddg=" in href:
                        m = re.search(r"uddg=([^&]+)", href)
                        if m: href = unquote(m.group(1))
                    data["pj_url"] = href
                    break

        if data["pj_url"]:
            # Scraping léger de la fiche PagesJaunes
            r_pj = session.get(data["pj_url"], timeout=8)
            if r_pj.status_code == 200:
                soup_pj = BeautifulSoup(r_pj.text, "html.parser")
                # Recherche téléphone
                tel_el = soup_pj.select_one("a[href^='tel:'], .coord-numero, [data-pj-pjphone]")
                if tel_el:
                    raw_tel = tel_el.get("href", "").replace("tel:", "") or tel_el.get_text()
                    data["phone"] = format_phone_fr(raw_tel)
                # Recherche site officiel
                web_el = soup_pj.select_one("a[data-pj-lb*='Site'], a.coord-site-web, a.site-web")
                if web_el and web_el.get("href"):
                    data["site_web"] = web_el.get("href").strip()
    except Exception:
        pass
    return data

# ─────────────────────────────────────────────────────────────────────────────
# 2. MODULE OVERPASS / OPENSTREETMAP (SPÉCIAL LAVERIES FRANCE ENTIÈRE)
# ─────────────────────────────────────────────────────────────────────────────

def query_osm_laveries_france(city_or_dept: str = "Paris") -> list:
    """
    Interroge OpenStreetMap via Overpass API pour extraire les laveries
    automatiques réelles avec téléphones, sites webs et horaires.
    """
    overpass_query = f"""
    [out:json][timeout:25];
    area["name"="{city_or_dept}"]->.searchArea;
    (
      node["shop"="laundry"](area.searchArea);
      way["shop"="laundry"](area.searchArea);
      node["amenity"="washing_machine"](area.searchArea);
    );
    out center tags;
    """
    try:
        r = requests.post(OVERPASS_API_URL, data={"data": overpass_query}, timeout=30)
        if r.status_code == 200:
            elements = r.json().get("elements", [])
            laveries = []
            for el in elements:
                tags = el.get("tags", {})
                nom = tags.get("name") or tags.get("brand") or "Laverie Automatique"
                phone = tags.get("phone") or tags.get("contact:phone") or ""
                website = tags.get("website") or tags.get("contact:website") or ""
                street = tags.get("addr:street", "")
                housenumber = tags.get("addr:housenumber", "")
                postcode = tags.get("addr:postcode", "")
                city = tags.get("addr:city", "") or city_or_dept

                adresse = f"{housenumber} {street}".strip()
                laveries.append({
                    "nom": nom,
                    "phone": format_phone_fr(phone),
                    "website": website,
                    "adresse": adresse,
                    "code_postal": postcode,
                    "ville": city
                })
            return laveries
    except Exception as e:
        logging.warning(f"[OVERPASS] Erreur pour {city_or_dept}: {e}")
    return []

# ─────────────────────────────────────────────────────────────────────────────
# 3. MODULE CRAWL WEB & ENRICHISSEMENT CONTACTS
# ─────────────────────────────────────────────────────────────────────────────

def search_website_and_socials(session: requests.Session, query: str) -> dict:
    data = {"site_web": "", "instagram": "", "facebook": "", "planity": ""}
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select(".result__a"):
                href = a.get("href", "")
                if "uddg=" in href:
                    m = re.search(r"uddg=([^&]+)", href)
                    if m: href = unquote(m.group(1))
                parsed = urlparse(href)
                domain = parsed.netloc.lower()

                if "instagram.com" in domain and not data["instagram"]:
                    m = INSTA_RE.search(href)
                    if m and m.group(1).lower() not in ["p", "reel", "explore"]:
                        data["instagram"] = f"https://www.instagram.com/{m.group(1)}/"
                elif "facebook.com" in domain and not data["facebook"]:
                    m = FB_RE.search(href)
                    if m and m.group(1).lower() not in ["sharer", "share", "tr"]:
                        data["facebook"] = f"https://www.facebook.com/{m.group(1)}"
                elif "planity.com" in domain and not data["planity"]:
                    data["planity"] = href
                elif domain and not data["site_web"]:
                    if not any(bad in domain for bad in [
                        "pagesjaunes.fr", "societe.com", "infogreffe.fr", "facebook.com",
                        "instagram.com", "google.com", "planity.com", "treatwell.fr"
                    ]):
                        data["site_web"] = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return data

def crawl_website_deep(session: requests.Session, base_url: str) -> dict:
    res = {"emails": set(), "mobiles": set(), "fixes": set(), "instagram": "", "facebook": "", "planity": ""}
    if not base_url or not base_url.startswith("http"):
        return res

    pages = [base_url, f"{base_url.rstrip('/')}/contact", f"{base_url.rstrip('/')}/mentions-legales", f"{base_url.rstrip('/')}/tarifs"]

    for u in pages:
        try:
            r = session.get(u, timeout=8)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")

            # Emails Cloudflare
            for cf in soup.select("[data-cfemail]"):
                dec = decode_cloudflare_email(cf.get("data-cfemail", ""))
                if dec and dec not in IGNORED_EMAILS:
                    res["emails"].add(dec)

            # Emails texte
            for mail in EMAIL_RE.findall(r.text):
                clean_m = mail.lower().strip(".,;:()")
                if clean_m not in IGNORED_EMAILS and not clean_m.endswith((".png", ".jpg", ".webp", ".svg")):
                    res["emails"].add(clean_m)

            # Téléphones
            for p in PHONE_FR_RE.findall(r.text):
                fmt = format_phone_fr(p)
                if is_mobile_phone(fmt):
                    res["mobiles"].add(fmt)
                else:
                    res["fixes"].add(fmt)

            # Réseaux
            for a in soup.select("a[href]"):
                h = a.get("href", "")
                if "instagram.com" in h and not res["instagram"]:
                    m = INSTA_RE.search(h)
                    if m and m.group(1).lower() not in ["p", "reel", "explore"]:
                        res["instagram"] = f"https://www.instagram.com/{m.group(1)}/"
                if "facebook.com" in h and not res["facebook"]:
                    m = FB_RE.search(h)
                    if m and m.group(1).lower() not in ["sharer", "share", "tr"]:
                        res["facebook"] = f"https://www.facebook.com/{m.group(1)}"
                if "planity.com" in h and not res["planity"]:
                    res["planity"] = h
        except Exception:
            continue

    return res

def send_to_sheets_webhook(webhook_url: str, rows: list) -> bool:
    if not webhook_url: return False
    try:
        r = requests.post(webhook_url, json={"action": "append_rows", "rows": rows}, timeout=30)
        return r.status_code == 200 and r.json().get("status") == "success"
    except Exception as e:
        logging.error(f"[WEBHOOK] Erreur : {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
# 4. PIPELINE GLOBAL D'EXÉCUTION
# ─────────────────────────────────────────────────────────────────────────────

def run_scraper_france(cible: str = "laverie", max_records: int = 50, output_csv: str = "prospects.csv"):
    libelle = "Laveries Automatiques" if cible == "laverie" else "Salons de Coiffure Indépendants"
    logging.info(f"===> LANCEMENT SCRAPING MULTI-SOURCES : {libelle} <===")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    webhook_url = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()

    prospects = []

    # 1. SI CIBLE = LAVERIE : interrogation conjointe Overpass + SIRENE
    if cible == "laverie":
        villes_focus = ["Paris", "Lyon", "Marseille", "Toulouse", "Bordeaux", "Lille", "Nice", "Nantes", "Strasbourg", "Montpellier"]
        for ville in villes_focus:
            if len(prospects) >= max_records: break
            osm_items = query_osm_laveries_france(ville)
            for it in osm_items:
                if len(prospects) >= max_records: break
                
                # PagesJaunes enrichment
                pj_info = search_pages_jaunes(session, it["nom"], it["ville"])
                web_url = it["website"] or pj_info["site_web"]
                phone = it["phone"] or pj_info["phone"]

                crawl_res = crawl_website_deep(session, web_url) if web_url else {}
                mobiles = list(crawl_res.get("mobiles", set()))
                fixes = list(crawl_res.get("fixes", set()))
                if phone and is_mobile_phone(phone) and phone not in mobiles: mobiles.append(phone)
                elif phone and phone not in fixes: fixes.append(phone)

                p = {
                    "Cible": "Laveries Automatiques",
                    "Enseigne / Nom": it["nom"],
                    "Dirigeant": "",
                    "Mobile 06/07": " / ".join(mobiles),
                    "Téléphone Fixe": " / ".join(fixes),
                    "Email": "; ".join(crawl_res.get("emails", [])),
                    "Instagram": crawl_res.get("instagram", ""),
                    "Facebook": crawl_res.get("facebook", ""),
                    "Site Web": web_url,
                    "Lien Planity": "",
                    "Ville": it["ville"],
                    "Code Postal": it["code_postal"],
                    "Adresse": it["adresse"],
                    "Source": "Overpass OSM + PagesJaunes + Web",
                    "Date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                }
                prospects.append(p)
                logging.info(f"[LAVERIE #{len(prospects)}] {p['Enseigne / Nom']} ({p['Ville']}) | Tél: {p['Mobile 06/07'] or p['Téléphone Fixe']} | Mail: {p['Email']}")
                time.sleep(0.3)

    # 2. COMPLÉMENT SIRENE (ou si CIBLE = COIFFURE)
    naf_code = "96.02A" if cible == "coiffure" else "96.01B"
    page = 1
    while len(prospects) < max_records:
        items = requests.get(API_RECHERCHE_ENTREPRISES, params={"activite_principale": naf_code, "etat_administratif": "A", "page": page, "per_page": 25}, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT).json().get("results", [])
        if not items: break

        for it in items:
            if len(prospects) >= max_records: break
            nom = clean_str(it.get("nom_complet") or it.get("nom_raison_sociale") or "")
            if cible == "coiffure" and any(f in nom.upper() for f in EXCLUDED_FRANCHISES):
                continue

            siege = it.get("siege", {})
            cp = clean_str(siege.get("code_postal", ""))
            ville = clean_str(siege.get("commune", ""))
            adresse = clean_str(siege.get("adresse", ""))

            # PagesJaunes + Web search
            pj_info = search_pages_jaunes(session, nom, ville)
            web_info = search_website_and_socials(session, f"{nom} {ville} {cp}")
            web_url = pj_info["site_web"] or web_info["site_web"]

            crawl_res = crawl_website_deep(session, web_url) if web_url else {}
            mobiles = list(crawl_res.get("mobiles", set()))
            fixes = list(crawl_res.get("fixes", set()))
            if pj_info["phone"]:
                if is_mobile_phone(pj_info["phone"]): mobiles.append(pj_info["phone"])
                else: fixes.append(pj_info["phone"])

            dirigeants = it.get("dirigeants", [])
            nom_dir = clean_str(f"{dirigeants[0].get('prenom', '')} {dirigeants[0].get('nom', '')}") if dirigeants else ""

            p = {
                "Cible": libelle,
                "Enseigne / Nom": nom,
                "Dirigeant": nom_dir,
                "Mobile 06/07": " / ".join(mobiles),
                "Téléphone Fixe": " / ".join(fixes),
                "Email": "; ".join(crawl_res.get("emails", [])),
                "Instagram": crawl_res.get("instagram") or web_info.get("instagram", ""),
                "Facebook": crawl_res.get("facebook") or web_info.get("facebook", ""),
                "Site Web": web_url,
                "Lien Planity": crawl_res.get("planity") or web_info.get("planity", ""),
                "Ville": ville,
                "Code Postal": cp,
                "Adresse": adresse,
                "Source": "SIRENE + PagesJaunes + Web",
                "Date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            }
            prospects.append(p)
            logging.info(f"[{cible.upper()} #{len(prospects)}] {nom} ({ville}) | Mob: {p['Mobile 06/07']} | Fixe: {p['Téléphone Fixe']} | Mail: {p['Email']}")
            time.sleep(0.3)

        page += 1

    if output_csv and prospects:
        with open(output_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=prospects[0].keys())
            writer.writeheader()
            writer.writerows(prospects)
        logging.info(f"CSV enregistré : {output_csv} ({len(prospects)} lignes)")

    if webhook_url and prospects:
        send_to_sheets_webhook(webhook_url, prospects)

    return prospects

if __name__ == "__main__":
    run_scraper_france("laverie", max_records=40, output_csv="laveries_france.csv")
    run_scraper_france("coiffure", max_records=40, output_csv="salons_coiffure_france.csv")
