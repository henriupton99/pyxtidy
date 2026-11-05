# pyxtidy

Import de photos et vidéos vers une destination triée par `AAAA/MM`, avec
renommage chronologique, détection de doublons et **rapport d'import** à
chaque exécution.

L'outil ne déplace ni ne modifie jamais la source : il **copie** uniquement.

## 1. Installation

Gestionnaire de dépendances [uv](https://docs.astral.sh/uv/) (Python ≥ 3.13) :

```bash
pip install uv
```

```bash
git clone https://github.com/henriupton99/pyxtidy && cd pyxtidy && uv sync
```

## 2. Usage

Toujours commencer par une simulation : rien n'est écrit, mais le rapport
complet est produit.

```bash
uv run pyxtidy <SRC_DIR> <DEST_DIR> --dry-run
```

Puis l'import réel :

```bash
uv run pyxtidy <SRC_DIR> <DEST_DIR>
```

### Déroulé

L'exécution se fait en cinq étapes, dans cet ordre. **Rien n'est copié avant
que la totalité de la source ait été inventoriée et planifiée** : on sait donc
combien de fichiers et d'octets sont concernés, et si la place disque suffit,
avant la première écriture.

| Étape | Rôle |
|---|---|
| 1. Scan de la source | un seul parcours, taille et date obtenues dans le même appel système ; classement par type et détection des paires Live Photo |
| 2. Index de la destination | inventaire du déjà-présent, synchronisé dans un manifeste SQLite |
| 3. Métadonnées | lecture EXIF / ffprobe et empreintes, **en parallèle** (le gros du temps) |
| 4. Planification | destination de chaque fichier, résolution des doublons et des collisions, contrôle de l'espace disque |
| 5. Copie | copie atomique, puis écriture du rapport |

### Options

| Option | Effet |
|---|---|
| `--dry-run` | tout analyser et produire le rapport, sans rien écrire |
| `--no-recursive` | ne pas descendre dans les sous-dossiers de la source |
| `--live-photos keep-both` | conserver aussi la composante vidéo des Live Photos |
| `--copy-unsupported` | recopier les formats non gérés dans `_non_tries/`, arborescence d'origine conservée |
| `--index-dest` | calculer l'empreinte de tout le déjà-présent (voir § 4) |
| `--require-ffprobe` | ignorer les vidéos si `ffprobe` est absent, au lieu de les dater d'après leur nom ou leur date système |
| `--verify` | comparer les SHA-256 source/destination après chaque copie (plus lent) |
| `--keep-original-name` | garder le nom d'origine dans le nom final |
| `--meta-workers N` | threads de lecture des métadonnées (défaut : 8) |
| `--copy-workers N` | copies simultanées ; `1` est le plus rapide sur disque externe |
| `--report-dir DIR` | déplacer les rapports hors de la destination |
| `-v` / `-q` | journal détaillé / seulement le rapport final |

Codes de sortie : `0` succès, `1` au moins un échec, `2` arrêt avant copie
(chemins invalides, espace insuffisant), `130` interruption.

## 3. Nommage et dates

Chaque média part dans `DEST_DIR/AAAA/MM/AAAA-MM-JJ_HH-MM-SS.ext`.

La date est cherchée dans cet ordre, et **son origine est tracée dans le
rapport** :

1. **photos** : EXIF `DateTimeOriginal` (avec sous-secondes et décalage
   horaire), puis `DateTimeDigitized`, puis `DateTime` ;
2. **vidéos** : `com.apple.quicktime.creationdate` (qui porte le fuseau réel),
   sinon `creation_time` (UTC, converti en heure locale) ;
3. **nom du fichier** : `IMG_20230810_142536`, `PXL_…`, `Screenshot 2023-08-10 at 14.25.36`,
   `signal-2023-08-10-142536`, etc. ;
4. **date de modification du fichier** en dernier recours.

Les fichiers datés par ce dernier recours sont listés dans la section *Dates
peu fiables* du rapport : la date de modification est souvent celle d'une
copie, pas de la prise de vue.

### Collisions

Deux médias différents pris à la même seconde (rafale) donnent
`…_14-30-00.jpg` et `…_14-30-00-01.jpg`. La numérotation est déterministe :
elle ne change pas d'une exécution à l'autre.

## 4. Doublons et reprise

`DEST_DIR` se comporte comme un point de reprise : relancer la même commande
ne recopie que ce qui manque. Trois mécanismes se complètent :

* **manifeste** `DEST_DIR/_pyxtidy/manifest.sqlite3` : empreinte de chaque
  média importé. Un fichier déjà importé est reconnu même si son nom de source
  a changé ;
* **nom + contenu** : si le nom de destination est déjà pris, l'empreinte des
  deux fichiers est comparée — contenu identique, on ignore ; contenu
  différent, on numérote ;
* **doublons internes à la source** : deux copies du même média dans `SRC_DIR`
  n'en produisent qu'une.

L'empreinte est un hachage rapide (taille + 256 Ko de début + 256 Ko de fin),
assez discriminant pour des médias et assez léger pour des milliers de
fichiers. `--verify` ajoute une comparaison SHA-256 intégrale après copie.

> **Destination créée avec une version antérieure à 0.2** : ses fichiers ne
> sont pas dans le manifeste. Lancer **une fois** `--index-dest` pour calculer
> leurs empreintes et éviter des recopies.

Une interruption (`Ctrl-C`, câble débranché) ne laisse aucun fichier tronqué :
chaque copie passe par un fichier temporaire `*.pyxtidy-part` renommé seulement
une fois complète et vérifiée, et les temporaires restants sont nettoyés au
démarrage suivant. Le manifeste est validé à chaque fichier, donc la reprise
repart exactement là où elle s'est arrêtée.

## 5. Rapport

À chaque exécution, quatre fichiers dans `DEST_DIR/_pyxtidy/reports/` :

| Fichier | Contenu |
|---|---|
| `import_<date>.md` | le rapport à conserver : paramètres, environnement, volumes, répartition par statut et par période, et toutes les anomalies |
| `import_<date>.csv` | une ligne par fichier source : statut, destination, date retenue et son origine, empreinte, raison, erreur |
| `import_<date>.json` | les mêmes données, exploitables par un script |
| `import_<date>.log` | journal horodaté de l'exécution |

Le rapport Markdown détaille en particulier :

* les **échecs** — fichiers non importés, avec l'erreur exacte ; ils restent
  intacts dans la source ;
* les **métadonnées dégradées** — média importé, mais EXIF ou ffprobe en échec ;
* les **dates peu fiables** — datées d'après la date système ;
* les **formats non gérés**, regroupés par extension ;
* les entrées **illisibles** rencontrées pendant le scan.

Synthèse affichée en fin d'exécution :

```
══ Rapport d'import ══
  Source                           : /Users/moi/Photos
  Destination                      : /Volumes/harddisk/Photos/tri
  Fichiers inventoriés             : 3131  (48.2 Go)
  Copiés                           : 2315  (37.1 Go)
  déjà importé (empreinte connue)  : 199
  doublon dans la source           : 12
  vidéo Live Photo ignorée         : 559
  fichier annexe ignoré            : 92
  Échecs                           : 0
  Durée                            : 24m18s

  Rapport  : /Volumes/harddisk/Photos/tri/_pyxtidy/reports/import_2026-11-05_14-22-41.md
  Détail   : /Volumes/harddisk/Photos/tri/_pyxtidy/reports/import_2026-11-05_14-22-41.csv
```

## 6. Dépendances externes

| Dépendance | Rôle | Sans elle |
|---|---|---|
| [pillow-heif](https://pypi.org/project/pillow-heif/) | EXIF des HEIC/HEIF | dates HEIC dégradées (nom de fichier ou date système) |
| [ffmpeg](https://ffmpeg.org/) (`ffprobe`) | date de création des vidéos | dates vidéo dégradées, ou vidéos ignorées avec `--require-ffprobe` |

```bash
brew install ffmpeg
```

Leur présence est affichée au démarrage et consignée dans chaque rapport.

## 7. Formats

* **Photo** : jpg, jpeg, jpe, png, heic, heif, avif, tif, tiff, gif, webp,
  bmp, et les RAW dng, cr2, cr3, nef, nrw, arw, srf, sr2, raf, orf, rw2, pef, raw
* **Vidéo** : mov, mp4, m4v, avi, mkv, 3gp, mts, m2ts, mpg, mpeg, webm
* **Annexes ignorés** : aae, xmp, thm, lrv, lrf, ctg, sec, pp3, on1
* **Autres** : laissés dans la source, ou recopiés dans `_non_tries/` avec
  `--copy-unsupported`

Les fichiers système (`.DS_Store`, `Thumbs.db`, resource forks `._*`) sont
ignorés sans bruit.
