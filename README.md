# B2B Prospects France — Salons de Coiffure & Laveries Automatiques

Dépôt automatisé de prospection commerciale B2B pour le marché français.

## 🎯 Cibles & Codes NAF Officiels
- **Salons de coiffure indépendants** (Code NAF `96.02A`, exclusion systématique des franchises).
- **Laveries automatiques & blanchisseries libre-service** (Code NAF `96.01B`).

## 📁 Architecture du Dépôt
```
b2b-prospects-scraper/
├── .github/
│   └── workflows/
│       └── b2b_prospects_scraper.yml    # Pipeline GitHub Actions (cron 2x/semaine)
├── scraper_prospects_france.py         # Script Python de scraping et d'enrichissement
├── Code_B2B_Prospects.gs               # Webhook Google Apps Script pour Google Sheets
├── requirements.txt                    # Dépendances Python
└── README.md                           # Documentation
```

## 🚀 Mise en Service Rapide

### 1. Liaison GitHub
```bash
git init
git add .
git commit -m "feat: initial commit b2b scraper"
git branch -M main
git remote add origin https://github.com/<VOTRE_COMPTE>/b2b-prospects-scraper.git
git push -u origin main
```

### 2. Configuration du Webhook Google Sheets
1. Ouvrir votre Google Sheet : `PROSPECTION_B2B_LAVERIES_ET_SALONS_FRANCE`
2. Aller dans `Extensions > Apps Script` et coller le code de `Code_B2B_Prospects.gs`.
3. Cliquer sur **Déployer > Nouveau déploiement > Application Web**.
   - *Exécuter en tant que :* Moi
   - *Qui a accès :* Tout le monde
4. Copier l'URL du webhook `/exec` et l'ajouter dans les Secrets de votre repo GitHub :
   - Nom : `GOOGLE_SHEET_WEBHOOK_URL`
   - Valeur : `https://script.google.com/macros/s/.../exec`

