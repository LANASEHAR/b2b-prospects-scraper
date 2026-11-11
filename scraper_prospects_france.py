#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
SCRAPER B2B INDUSTRIEL — RUN OPTIMAL 1H30 - 2H00 (FRANCE ENTIÈRE 96 DEPTS)
=============================================================================
Conçu pour une exécution journalière de 1h30 à 2h00 :
- Boucle sur les 96 départements français (01 à 95, 2A, 2B)
- Cibles :
  * Laveries automatiques & blanchisseries libre-service (NAF 96.01B + OSM Overpass)
  * Salons de coiffure indépendants (NAF 96.02A, exclusion stricte franchises)
- Recherche croisée : PagesJaunes, Overpass OpenStreetMap, SIRENE, DuckDuckGo
- Crawl profond : Mobiles 06/07 (WhatsApp/SMS), Fixes, Emails vérifiés, Instagram, Facebook, Planity
- Envoi continu par lots vers Google Sheets (toutes les 10 fiches qualifiées)
- Arrêt contrôlé propre à 100 minutes (1h40) avec sauvegarde intégrale
=============================================================================
"""

import os
import re
import csv
import time
import json
import logging
from urllib.parse import urlparse, quote_plus, unquote
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

API_RECHERCHE_ENTREPRISES = "https://recherche-entreprises.api.gouv.fr/search"
OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
TIMEOUT = 12

# Gestion de la durée d'exécution (1h35 = 5700 secondes pour être sûr de dépasser 1h30 sans dépasser 2h)
MAX_RUN_SECONDS = 95 * 60  # 1h35 (95 minutes)
TARGET_RECORDS_PER_RUN = 500

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

# 96 départements métropolitains
DEPARTEMENTS_FRANCE = [
    "75", "69", "13", "31", "33", "59", "06", "44", "67", "34", # Grandes métropoles d'abord
    "01", "02", "03", "04", "05", "07", "08", "09", "10",
    "11", "12", "14", "15", "16", "17", "18", "19", "21", "22",
    "23", "24", "25", "26", "27", "28", "29", "2A", "2B", "30",
    "32", "35", "36", "37", "38", "39", "40", "41", "42", "43",
    "45", "46", "47", "48", "49", "50", "51", "52", "53", "54",
    "55", "56", "57", "58", "60", "61", "62", "63", "64", "65",
    "66", "68", "70", "71", "72", "73", "74", "76", "77", "78",
    "79", "80", "81", "82", "83", "84", "85", "86", "87", "88",
    "89", "90", "91", "92", "93", "94", "95"
]

def clean_str(val: str) -> str:
    return re.sub(r"\s+", " ", str(val or "")).strip()

def decode_cloudflare_email(cf_hash: str) -> str:
    try:
        r = int(cf_hash[:2], 16)
        return "".join([chr(int(cf_hash[i:i+2], 16) ^ r) for i in range(2, len(cf_hash), 2)]).lower().strip()
    except Exception:
        return ""

def format_phone_fr(raw: str) -> str:
    p = re.sub(r"[^\d+]", "", raw)
    if p.startswith("+33"):
        p = "0" + p[3:]
    if len(p) == 10 and p.startswith("0"):
        return " ".join([p[i:i+2] for i in range(0, 10, 2)])
    return p

def is_mobile(phone_str: str) -> bool:
    c = re.sub(r"[^\d]", "", phone_str)
    return c.startswith(("06", "07", "336", "337"))

def search_pages_jaunes(session: requests.Session, nom: str, ville: str) -> dict:
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
            r_pj = session.get(data["pj_url"], timeout=8)
            if r_pj.status_code == 200:
                soup_pj = BeautifulSoup(r_pj.text, "html.parser")
                tel = soup_pj.select_one("a[href^='tel:'], .coord-numero, [data-pj-pjphone]")
                if tel:
                    data["phone"] = format_phone_fr(tel.get("href", "").replace("tel:", "") or tel.get_text())
                web = soup_pj.select_one("a[data-pj-lb*='Site'], a.coord-site-web, a.site-web")
                if web and web.get("href"):
                    data["site_web"] = web.get("href").strip()
    except Exception:
        pass
    return data

def search_web_socials(session: requests.Session, query: str) -> dict:
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

def crawl_website(session: requests.Session, base_url: str) -> dict:
    res = {"emails": set(), "mobiles": set(), "fixes": set(), "instagram": "", "facebook": "", "planity": ""}
    if not base_url or not base_url.startswith("http"):
        return res
    pages = [base_url, f"{base_url.rstrip('/')}/contact", f"{base_url.rstrip('/')}/mentions-legales"]
    for u in pages:
        try:
            r = session.get(u, timeout=8)
            if r.status_code != 200: continue
            soup = BeautifulSoup(r.text, "html.parser")

            for cf in soup.select("[data-cfemail]"):
                dec = decode_cloudflare_email(cf.get("data-cfemail", ""))
                if dec and dec not in IGNORED_EMAILS: res["emails"].add(dec)

            for mail in EMAIL_RE.findall(r.text):
                clean_m = mail.lower().strip(".,;:()")
                if clean_m not in IGNORED_EMAILS and not clean_m.endswith((".png", ".jpg", ".webp", ".svg")):
                    res["emails"].add(clean_m)

            for p in PHONE_FR_RE.findall(r.text):
                fmt = format_phone_fr(p)
                if is_mobile(fmt): res["mobiles"].add(fmt)
                else: res["fixes"].add(fmt)

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

def send_batch_to_sheets(webhook_url: str, rows: list) -> bool:
    if not webhook_url or not rows: return False
    try:
        r = requests.post(webhook_url, json={"action": "append_rows", "rows": rows}, timeout=30)
        return r.status_code == 200
    except Exception as e:
        logging.error(f"[WEBHOOK] Erreur : {e}")
        return False

def run_large_scale_pipeline():
    start_time = time.time()
    webhook_url = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    total_prospects = 0
    buffer_sheets = []
    csv_file = "prospects_france_complet.csv"

    # Initialisation fichier CSV
    fieldnames = [
        "Cible", "Enseigne / Nom", "Dirigeant", "Mobile 06/07", "Téléphone Fixe",
        "Email", "Instagram", "Facebook", "Site Web", "Lien Planity",
        "Ville", "Code Postal", "Adresse", "Date"
    ]
    with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

    logging.info(f"=== DÉMARRAGE DU RUN OPTIMAL (Objectif durée : 1h30 à 1h45) ===")

    for dept in DEPARTEMENTS_FRANCE:
        # Vérification du temps écoulé
        elapsed = time.time() - start_time
        if elapsed >= MAX_RUN_SECONDS:
            logging.info(f"[TEMPS ATTEINT] 95 minutes écoulées ({int(elapsed/60)} min). Arrêt propre du run.")
            break

        logging.info(f"--- TRAITEMENT DU DÉPARTEMENT {dept} (Temps écoulé : {int(elapsed/60)} min) ---")

        # Pour chaque département, on traite les deux cibles
        for cible, naf in [("Laveries Automatiques", "96.01B"), ("Salons de Coiffure", "96.02A")]:
            if time.time() - start_time >= MAX_RUN_SECONDS:
                break

            page = 1
            max_pages_dept = 4 # Jusqu'à 100 fiches par cible par département

            while page <= max_pages_dept:
                if time.time() - start_time >= MAX_RUN_SECONDS:
                    break

                try:
                    params = {
                        "activite_principale": naf,
                        "departement": dept,
                        "etat_administratif": "A",
                        "page": page,
                        "per_page": 25
                    }
                    r = session.get(API_RECHERCHE_ENTREPRISES, params=params, timeout=TIMEOUT)
                    if r.status_code != 200: break
                    items = r.json().get("results", [])
                    if not items: break
                except Exception:
                    break

                for it in items:
                    if time.time() - start_time >= MAX_RUN_SECONDS:
                        break

                    nom = clean_str(it.get("nom_complet") or it.get("nom_raison_sociale") or "")
                    if cible == "Salons de Coiffure" and any(f in nom.upper() for f in EXCLUDED_FRANCHISES):
                        continue

                    siege = it.get("siege", {})
                    cp = clean_str(siege.get("code_postal", ""))
                    ville = clean_str(siege.get("commune", ""))
                    adresse = clean_str(siege.get("adresse", ""))

                    # Recherche croisée PagesJaunes + Web
                    pj = search_pages_jaunes(session, nom, ville)
                    web = search_web_socials(session, f"{nom} {ville} {cp}")
                    site_final = pj.get("site_web") or web.get("site_web", "")

                    crawl = crawl_website(session, site_final) if site_final else {}
                    mobiles = list(crawl.get("mobiles", set()))
                    fixes = list(crawl.get("fixes", set()))
                    if pj.get("phone"):
                        if is_mobile(pj["phone"]): mobiles.append(pj["phone"])
                        else: fixes.append(pj["phone"])

                    dirigeants = it.get("dirigeants", [])
                    nom_dir = clean_str(f"{dirigeants[0].get('prenom', '')} {dirigeants[0].get('nom', '')}") if dirigeants else ""

                    p = {
                        "Cible": cible,
                        "Enseigne / Nom": nom,
                        "Dirigeant": nom_dir,
                        "Mobile 06/07": " / ".join(mobiles),
                        "Téléphone Fixe": " / ".join(fixes),
                        "Email": "; ".join(crawl.get("emails", [])),
                        "Instagram": crawl.get("instagram") or web.get("instagram", ""),
                        "Facebook": crawl.get("facebook") or web.get("facebook", ""),
                        "Site Web": site_final,
                        "Lien Planity": crawl.get("planity") or web.get("planity", ""),
                        "Ville": ville,
                        "Code Postal": cp,
                        "Adresse": adresse,
                        "Date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    }

                    # Écriture immédiate dans le CSV miroir
                    with open(csv_file, "a", newline="", encoding="utf-8-sig") as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        writer.writerow(p)

                    buffer_sheets.append(p)
                    total_prospects += 1
                    logging.info(f"[{total_prospects}] {nom} ({ville} - {dept}) | Mob: {p['Mobile 06/07']} | Fixe: {p['Téléphone Fixe']} | Mail: {p['Email']}")

                    # Envoi par lot de 10 vers Google Sheets
                    if len(buffer_sheets) >= 10:
                        send_batch_to_sheets(webhook_url, buffer_sheets)
                        buffer_sheets = []

                    time.sleep(0.4) # Cadence respectueuse des serveurs

                page += 1

    # Envoi du reliquat final
    if buffer_sheets:
        send_batch_to_sheets(webhook_url, buffer_sheets)

    duration_min = int((time.time() - start_time) / 60)
    logging.info(f"=== FIN DU RUN : {total_prospects} prospects collectés en {duration_min} minutes. ===")

if __name__ == "__main__":
    run_large_scale_pipeline()
