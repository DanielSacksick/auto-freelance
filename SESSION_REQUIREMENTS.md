# SESSION_REQUIREMENTS.md — état des sessions par plateforme

Généré le 01/09/2026 lors de la réparation des mappers de soumission
(Phase 2/3). Vérifié en conditions réelles avec Playwright headless contre
des offres réellement scannées (`pipeline.db`). Objectif : dire précisément,
plateforme par plateforme, ce qui bloque une soumission automatique
aujourd'hui, et comment le débloquer.

Rappel de sûreté ([CLAUDE.md](CLAUDE.md)) : un fichier de session est un
secret. Aucun contenu de cookie n'est reproduit ci-dessous, seulement leur
état (présent/absent, valide/expiré).

## Résumé

| Plateforme | Session locale | État constaté | Formulaire automatisable ? |
|---|---|---|---|
| freework | présente, 17j | **expirée** — mur de connexion au clic Postuler | Oui, une fois la session renouvelée |
| freelance-informatique | présente, 17j | **expirée** — redirige vers `/connexion?p=…` | Oui, une fois la session renouvelée |
| freelancermap | présente, 17j | **expirée** — redirige vers `/login` avant même la fiche | Oui, une fois la session renouvelée |
| linkedin | présente (convertie depuis cookies.txt, 01/09) | cookies valides (`/feed/` authentifié) **mais** `/jobs/*` renvoie systématiquement vers `/login` en automatisation headless — voir note ci-dessous | Partiellement — bloqué par une détection anti-bot spécifique aux pages Jobs, pas par l'expiration |
| indeed | absente | recherche OK avec mitigation anti-détection ; page offre (`/viewjob`) toujours bloquée (`from=bot-detection-anonymous`), probablement IP datacenter | Non vérifiable depuis cet environnement — nécessite IP résidentielle ou computer-use |
| wttj | absente | candidature strictement réservée aux connectés ; clic sans session ne fait rien d'observable (pas de modale de connexion) | Oui a priori, mais formulaire jamais observé (mur de connexion) |

## Détail par plateforme

### freework (free-work.com)

- Session existante : `/data/agents/hermes/.auto-freelance-sessions/freework.json` (17 jours, expirée).
- Constaté : la fiche mission s'affiche normalement sans session (contenu
  public), avec deux boutons « Postuler » (un visible, un caché). Sans
  session valide, cliquer ne fait rien d'observable — c'est **documenté et
  attendu** (voir `job_scanner/submission/forms/freework.py`), pas une
  régression.
- Sélecteurs vérifiés à jour, aucun changement de DOM détecté sur le bouton
  « Postuler » lui-même.
- **À faire** : réexporter la session —
  `python -m job_scanner.submission.playwright --export freework`
  (nécessite un navigateur avec affichage ; sur ce serveur, l'exporter
  depuis un poste avec accès X11/VNC puis copier le fichier JSON vers
  `~/.auto-freelance-sessions/freework.json` avec `chmod 600`).

### freelance-informatique (freelance-informatique.fr)

- Session existante : `/data/agents/hermes/.auto-freelance-sessions/freelance-informatique.json` (17 jours, expirée).
- **Changement de DOM réel détecté et corrigé** : le CTA « Postuler » n'est
  plus un `<a href="/candidature-…">` mais un
  `<span class="btn-postuler" data-obf="<base64 du chemin>">`. Le mapper
  décode maintenant `data-obf` et navigue directement vers l'URL de
  candidature (voir `job_scanner/submission/forms/freelance_info.py`).
  Vérifié bout en bout : sans session, le chemin décodé redirige bien vers
  `/connexion?p=/candidature-…`, comme avant le changement de DOM.
- **À faire** : réexporter la session —
  `python -m job_scanner.submission.playwright --export freelance-informatique`.

### freelancermap (freelancermap.com)

- Session existante : `/data/agents/hermes/.auto-freelance-sessions/freelancermap.json` (17 jours, expirée) — redirige désormais vers `/login` avant même d'atteindre la fiche projet.
- Sans session (contexte propre) : la fiche projet s'affiche normalement,
  le bouton « Apply now » est trouvé par le sélecteur existant, et cliquer
  ouvre bien la modale avec liens « Log in » / « Sign up » — **aucun
  changement de DOM**, le mapper fonctionne tel quel dès qu'une session
  valide existe.
- **À faire** : réexporter la session —
  `python -m job_scanner.submission.playwright --export freelancermap`.

### linkedin (linkedin.com)

- Session : convertie le 01/09/2026 depuis
  `/data/agents/hermes/backup-sandbox-home-20260813_234556/.agent-reach/cookies/linkedin_cookies.txt`
  (export Netscape) vers un storage_state Playwright, déposée à
  `/data/agents/hermes/.auto-freelance-sessions/linkedin.json` (chemin lu par
  le pipeline via `AUTOFREELANCE_SESSION_DIR`) et, à la demande, également à
  `/data/agents/hermes/home/.hermes/submission/linkedin.json` (note :
  `~/.hermes/submission/` sur `/home/deploy` est en lecture seule dans cet
  environnement — écrit à la place sous `/data/agents/hermes/home`, le seul
  HOME inscriptible ici).
- Cookies non expirés (li_at valide ~345 jours) et **authentification
  confirmée** sur `linkedin.com/feed/` (contenu du fil, nom du compte
  visible).
- **Constat important** : toute navigation vers `linkedin.com/jobs/*`
  (recherche, collections, ou une fiche `/jobs/view/<id>` précise) redirige
  systématiquement vers `/login`, y compris juste après avoir chargé `/feed/`
  authentifié dans la même session. Ce n'est pas une expiration de cookie —
  c'est très probablement une détection anti-bot ciblée sur la section Jobs
  (fingerprint headless Chromium), plus stricte que sur le reste du site.
- **Conséquence** : le mapper `linkedin.py` (Easy Apply) est correct dans sa
  logique, mais ne peut pas être validé de bout en bout dans cet
  environnement headless. En pratique, `submit()` détectera le mur de
  connexion via `_on_login_page()` et abandonnera proprement
  (`session_required`) — pas de risque de soumission cassée, juste
  d'auto-submit qui ne se déclenche jamais tant que ce blocage existe.
- **Pistes pour débloquer** : exporter/soumettre depuis un navigateur non
  headless (`--visible`), ou un environnement moins fingerprintable (résidu
  d'IP datacenter très probablement un facteur). À réévaluer avant de
  compter sur l'auto-submit LinkedIn en production.

### indeed (indeed.com)

- Aucune session.
- **Constat** : Indeed bloque l'accès anonyme automatisé dès la simple
  lecture d'une fiche mission (`secure.indeed.com/auth;jsessionid=…&from=
  bot-detection-anonymous`), avant même toute tentative de candidature.
  Impossible d'observer le DOM réel du formulaire (interne « Indeed Apply »
  ou externe) dans cet environnement.
- **Mitigation anti-détection testée le 02/09/2026** (`--disable-blink-
  features=AutomationControlled` + user-agent Chrome 131 desktop réaliste,
  voir `job_scanner/submission/playwright.py::submit()`) : **partiellement
  efficace**. Une recherche (`fr.indeed.com/jobs?...`) charge normalement,
  sans mur — le fingerprint navigateur n'est donc plus l'obstacle sur cette
  page. Mais une fiche mission précise (`/viewjob?jk=…`) redirige toujours
  systématiquement vers `secure.indeed.com/auth;…&from=bot-detection-
  anonymous`, flags anti-détection actifs et user-agent identique.
- **Cause probable** : l'IP sortante de cet environnement appartient à un
  hébergeur (Hostinger/AS47583, vérifié via `ipinfo.io` le 02/09/2026), pas
  à un FAI résidentiel — Indeed bloque très probablement par plage IP
  datacenter sur les pages fiche mission, indépendamment du fingerprint
  navigateur. Les flags Chromium ne peuvent rien contre un blocage réseau.
- Le mapper `job_scanner/submission/forms/indeed.py` a été écrit à partir de
  la structure documentée/publique du widget Indeed Apply
  (`#indeedApplyButton` → iframe `#indeedapply-modal-iframe`), **jamais
  vérifié en conditions réelles** — impossible tant que la fiche mission
  reste bloquée dans cet environnement. Volontairement **pas** ajouté à
  `config.AUTO_SUBMIT_PLATFORMS`.
- **À faire avant tout usage** : soit exporter une session ET lancer le run
  Playwright depuis une IP résidentielle (poste local de Dan, pas ce
  serveur), soit passer par un flux computer-use (navigateur réel piloté
  interactivement, capable de résoudre un éventuel mur restant) plutôt que
  Playwright headless pur. Tant que ni l'un ni l'autre n'est en place,
  Indeed reste hors de portée de l'auto-submit depuis cet environnement.

### wttj (welcometothejungle.com)

- Aucune session.
- Constaté : le CTA « Postuler » (bouton JS, sans `href`) ne déclenche rien
  d'observable sans session — ni navigation, ni modale de connexion. Comme
  FreeWork, la candidature est strictement réservée aux connectés.
- Le formulaire réel (une fois connecté) n'a pas pu être observé. Le mapper
  `job_scanner/submission/forms/wttj.py` retombe donc sur la recherche par
  intention générique du moteur (`FormMapper` de base) pour le champ message
  et le bouton d'envoi, faute de sélecteurs exacts observés.
- **À faire avant tout usage** : exporter une session (`--export wttj`),
  puis valider et ajuster les sélecteurs de message/envoi avec un
  `--dry-run` sur une offre réelle.

## Comment exporter une session

```
python -m job_scanner.submission.playwright --export <source>
```

Nécessite un navigateur avec affichage (le script ouvre une fenêtre
Chromium, attend une connexion manuelle, puis écrit le storage_state). Sur
un serveur sans affichage, exporter depuis un poste local puis copier le
fichier JSON vers `AUTOFREELANCE_SESSION_DIR` (`chmod 600`, jamais commité —
voir `.gitignore` et [CLAUDE.md](CLAUDE.md)).
