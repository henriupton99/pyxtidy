# pyxtidy

Utilitaire de tri de fichiers medias (photos, vidéos). 

## 1. Usage 

Installer le gestionnaire de dépendances en python *uv* (version de python recommandée >=3.13) : 

```bash
pip install uv
uv --version
```

Cloner le projet : 

```bash
git clone https://github.com/henriupton99/pyxtidy
cd pyxtidy
```

Lancer un tri de fichiers en donnant en entrée :
* le dossier source de medias à trier : *SRC_DIR* 
* le dossier de destination de medias triés : *DEST_DIR*

et en executant la commande suivante depuis le repertoire principal du projet : 

```bash
uv run python3 ./src/pyxtidy/main.py <SRC_DIR> <DEST_DIR>
```

*NB : DEST_DIR fonctionne avec un système de checkpoint. Si ce dossier existe déjà, seulement les médias qui ne sont pas dans ce fichier seront traités. Ainsi il est possible de réaliser plusieurs backup en ciblant toujours le même dossier DEST_DIR afin de récupérer depuis le dernier media sauvegardé.*

A partir d'un dossier SRC_DIR contenant tous les medias à organiser, le programme va traiter toutes les photos et vidéos et les ranger dans des sous-dossiers par YYYY/MM, avec comme nom générique "YYYY-MM-DD_HH-MM-SS" pour organiser par ordre chronologique et ignorer les doublons. 

Exemple de logs en sortie : 
```bash
✅ Copié (vidéo) → ./test_out/2024/09/2024-09-17_16-00-28.mov
✅ Copié (vidéo) → ./test_out/2024/09/2024-09-29_13-29-52.mov
⏭️ Vidéo ignorée : format not handled (aae) : IMG_5669.AAE
⏭️ Vidéo ignorée : format not handled (aae) : IMG_5670.AAE
```

Un rapport final indique le nombre de fichiers initial, les fichiers finalement copiés, ainsi que ceux ignorés (doublons, déjà présents, format non supporté) : 

```bash
--- Rapport ---
Nombre total : 3131
Copiés : 2315
Ignorés (déjà présents / Live / sans ffprobe) : 571
Déjà présents (checkpoint) : 199
Format non supporté : 92
```

## 2. Dépendances

Certaines dépendances sont requises pour le traitement des métadonnées de certains formats de médias : 

- fichiers photos *HEIC/HEIF* : [**pillow-heif**](https://pypi.org/project/pillow-heif/) 

- fichiers video : [**ffmpeg**](https://ffmpeg.org/)

Sans ces dépendances, les formats de fichiers associés seront ignorés.

## 3. Formats supportés

Photo : **jpg, jpeg, png, heic, heif**

Video : **mov, mp4, m4v**
