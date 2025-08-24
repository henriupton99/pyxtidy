import os
import sys
import json
import shutil
import hashlib
import subprocess

from time import time
from datetime import datetime
from PIL import Image
from PIL.ExifTags import TAGS

# --- Config ---
IMAGE_EXTS = {"jpg", "jpeg", "png", "heic", "heif"}
VIDEO_EXTS = {"mov", "mp4", "m4v"}

# --- Détection HEIF_OPENER : formats photo HEIC, HEIF  ---
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    print("[WARNING] pillow-heif non installé → les photos au format HEIC/HEIF tomberont en fallback (car lecture EXIF limitée).")
    print("Pour supprimer ce message, installer pillow-heif : pip install pillow-heif")
    time.sleep(5)
    
# --- Détection FFPROBE : formats video MP4, MOV, M4V ---
FFPROBE_PATH = shutil.which("ffprobe")
if not FFPROBE_PATH:
    # fallbacks courants (macOS ARM Homebrew / Linux / Windows)
    for p in ["/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", "/usr/bin/ffprobe", "C:\\ffmpeg\\bin\\ffprobe.exe"]:
        if os.path.exists(p):
            FFPROBE_PATH = p
            break

if FFPROBE_PATH:
    print(f"✅ ffprobe détecté : {FFPROBE_PATH}")
else:
    print("[WARNING] ffprobe introuvable → les vidéos seront ignorées.")
    print("Pour supprimer ce message, installer pillow-heif : brew install ffmpeg")
    time.sleep(5)

# ---------- Utilitaires cache Live Photo ----------
def _live_cache_path(target_dir: str) -> str:
    return os.path.join(target_dir, ".live_cache.json")

def load_live_cache(target_dir: str) -> set:
    """Charge le set des basenames d'images déjà traitées pour ce dossier année/mois."""
    path = _live_cache_path(target_dir)
    if os.path.exists(path):
        try:
            data = json.load(open(path, "r", encoding="utf-8"))
            return set(data.get("image_basenames", []))
        except Exception:
            return set()
    return set()

def save_live_cache(target_dir: str, cache_set: set) -> None:
    path = _live_cache_path(target_dir)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"image_basenames": sorted(cache_set)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Impossible d'écrire {path} : {e}")

# ---------- Lecture des dates ----------
def _exif_datetime_local(img_path: str) -> datetime:
    """
    Date EXIF pour images (locale). Gère SubSecTimeOriginal si présent (précision sub-secondes).
    Fallback = mtime du fichier (locale).
    """
    try:
        img = Image.open(img_path)
        exif = img._getexif()
        if exif:
            dt_str = None
            subsec = None
            for tag, val in exif.items():
                name = TAGS.get(tag, str(tag))
                lname = str(name).lower()
                if name == "DateTimeOriginal":
                    dt_str = val
                elif lname in ("subsectimeoriginal", "subsectime"):
                    subsec = str(val)

            if dt_str:
                # "YYYY:MM:DD HH:MM:SS"
                dt = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
                if subsec:
                    # convertir "123" (ms) ou "123456" (µs) en microsecondes
                    us = int(str(subsec).ljust(6, "0")[:6])  # pad/tronc à 6 chiffres
                    dt = dt.replace(microsecond=us)
                return dt
    except Exception:
        pass

    # fallback = date système (locale)
    return datetime.fromtimestamp(os.path.getmtime(img_path))

def _ffprobe_creation_local(video_path: str) -> datetime | None:
    """
    Date de création via ffprobe (UTC -> locale). Retourne None si indisponible.
    """
    if not FFPROBE_PATH:
        return None
    try:
        cmd = [
            FFPROBE_PATH, "-v", "quiet",
            "-print_format", "json",
            "-show_entries", "format_tags=creation_time",
            video_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(res.stdout) if res.stdout else {}
        ct = data.get("format", {}).get("tags", {}).get("creation_time")
        if ct:
            # ex: "2023-08-10T14:25:36.123456Z"
            dt_utc = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            return dt_utc.astimezone()  # -> heure locale
    except Exception as e:
        print(f"⚠️ ffprobe échec ({video_path}) : {e}")
    return None

def get_media_datetime(path: str) -> datetime:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in IMAGE_EXTS:
        return _exif_datetime_local(path)
    if ext in VIDEO_EXTS:
        dt = _ffprobe_creation_local(path)
        if dt:
            return dt
        # si pas ffprobe : fallback mtime ou… skip (on choisit mtime pour garder l'ordre)
        return datetime.fromtimestamp(os.path.getmtime(path))
    # Autres : fallback mtime
    return datetime.fromtimestamp(os.path.getmtime(path))

# ---------- Nommage déterministe et anti-doublons ----------
def _hash6(source_basename: str, dt: datetime) -> str:
    """
    Hash court, stable par fichier, pour lever les collisions à la même seconde.
    On utilise le basename d'origine + subsec pour différencier proprement.
    """
    token = f"{source_basename}|{dt.strftime('%f')}"
    return hashlib.sha1(token.encode("utf-8")).hexdigest()[:6]

def build_dest_name(dt: datetime, ext: str, source_basename: str) -> tuple[str, str]:
    """
    Retourne (plain_name, hashed_name) :
    - plain_name  = 'YYYY-MM-DD_HH-MM-SS.ext'
    - hashed_name = 'YYYY-MM-DD_HH-MM-SS--<hash6>.ext'  (stable)
    On ne décide PAS ici lequel utiliser; la décision se prend en regardant le disque.
    """
    stamp = dt.strftime("%Y-%m-%d_%H-%M-%S")
    ext = ext.lower()
    plain  = f"{stamp}.{ext}"
    hashed = f"{stamp}--{_hash6(source_basename, dt)}.{ext}"
    return plain, hashed

# ---------- Copie avec idempotence ----------
def copy_once(src_path: str, target_dir: str, dt: datetime) -> tuple[bool, str]:
    """
    Copie en appliquant le schéma de nommage déterministe.
    Si 'plain' existe déjà -> on considère que le fichier (ou un équivalent)
    a déjà été traité (skip). Sinon on tente 'hashed'. Si 'hashed' existe -> skip.
    Retourne (copied?, dest_path_or_reason).
    """
    os.makedirs(target_dir, exist_ok=True)

    filename = os.path.basename(src_path)
    source_basename, ext = os.path.splitext(filename)
    ext = ext.lstrip(".").lower()

    plain_name, hashed_name = build_dest_name(dt, ext, source_basename)
    plain_path  = os.path.join(target_dir, plain_name)
    hashed_path = os.path.join(target_dir, hashed_name)

    # 1) Si le nom "plain" existe déjà → on considère traité (idempotent)
    if os.path.exists(plain_path):
        return (False, f"existe (plain): {plain_name}")

    # 2) Sinon, si le nom "hashed" existe déjà → déjà traité aussi
    if os.path.exists(hashed_path):
        return (False, f"existe (hashed): {hashed_name}")

    # 3) Sinon, on essaye de poser le "plain" en premier
    try:
        shutil.copy2(src_path, plain_path)
        return (True, plain_path)
    except FileExistsError:
        pass
    except Exception as e:
        # si la copie plain échoue pour une autre raison, on tente hashed
        # (ex: un autre process vient d'écrire plain avec le même nom)
        print(f"ℹ️ copie plain échouée, on tente hashed ({e})")

    shutil.copy2(src_path, hashed_path)
    return (True, hashed_path)

# ---------- Pipeline ----------

def main():
    if not os.path.exists(DEST_DIR):
        os.makedirs(DEST_DIR)

    copied, skipped, existed, fmt_not_handled = 0, 0, 0, 0
    live_cache_per_dir: dict[str, set] = {}  # map de target_dir -> set(basenames img)

    # --- 1) Traiter d'abord les IMAGES ---
    for filename in sorted(os.listdir(SOURCE_DIR)):
        src = os.path.join(SOURCE_DIR, filename)
        if not os.path.isfile(src):
            continue

        ext = os.path.splitext(filename)[1].lower().lstrip(".")
        if ext not in VIDEO_EXTS and ext not in IMAGE_EXTS:
            print(f"⏭️ Image ignorée : format not handled ({ext}) : {filename}")
            fmt_not_handled += 1 
            continue
        
        if ext not in IMAGE_EXTS:
          continue

        dt = get_media_datetime(src)
        year, month = dt.strftime("%Y"), dt.strftime("%m")
        target_dir = os.path.join(DEST_DIR, year, month)

        # Charger (ou init) le cache Live de ce dossier
        if target_dir not in live_cache_per_dir:
            live_cache_per_dir[target_dir] = load_live_cache(target_dir)

        # Copier avec nommage déterministe
        ok, info = copy_once(src, target_dir, dt)
        if ok:
            copied += 1
            # Ajouter le basename d'origine de l'image au cache Live
            base = os.path.splitext(filename)[0]
            live_cache_per_dir[target_dir].add(base)
            print(f"✅ Copié (image) → {info}")
        else:
            # Pas copié (déjà présent)
            existed += 1
            print(f"⏭️ Image ignorée ({info})")

    # Sauvegarder les caches Live mis à jour après les images
    for td, cache_set in live_cache_per_dir.items():
        save_live_cache(td, cache_set)

    # --- 2) Traiter ensuite les VIDEOS ---
    for filename in sorted(os.listdir(SOURCE_DIR)):
        src = os.path.join(SOURCE_DIR, filename)
        if not os.path.isfile(src):
            continue

        ext = os.path.splitext(filename)[1].lower().lstrip(".")
        
        if ext not in VIDEO_EXTS and ext not in IMAGE_EXTS:
            print(f"⏭️ Vidéo ignorée : format not handled ({ext}) : {filename}")
            fmt_not_handled += 1 
            continue
        
        if ext not in VIDEO_EXTS:
          continue
        
        if not FFPROBE_PATH:
            skipped += 1
            print(f"⏭️ Vidéo ignorée (pas de ffprobe) : {filename}")
            continue

        dt = get_media_datetime(src)
        year, month = dt.strftime("%Y"), dt.strftime("%m")
        target_dir = os.path.join(DEST_DIR, year, month)

        # Charger (ou init) le cache Live de ce dossier
        if target_dir not in live_cache_per_dir:
            live_cache_per_dir[target_dir] = load_live_cache(target_dir)

        # Règle Live Photo : si une image du même basename a été traitée → skip la vidéo
        base = os.path.splitext(filename)[0]
        if base in live_cache_per_dir[target_dir]:
            skipped += 1
            print(f"⏭️ Vidéo ignorée (Live Photo, image présente) : {filename}")
            continue

        # Copier avec nommage déterministe (idempotent)
        ok, info = copy_once(src, target_dir, dt)
        if ok:
            copied += 1
            print(f"✅ Copié (vidéo) → {info}")
        else:
            existed += 1
            print(f"⏭️ Vidéo ignorée ({info})")

    print("\n--- Rapport ---")
    print(f"Nombre total : {len(os.listdir(SOURCE_DIR))}")
    print(f"Copiés : {copied}")
    print(f"Ignorés (déjà présents / Live / sans ffprobe) : {skipped}")
    print(f"Déjà présents (checkpoint) : {existed}")
    print(f"Format non supporté : {fmt_not_handled}")


if __name__ == "__main__":
    SOURCE_DIR = sys.argv[1]
    DEST_DIR   = sys.argv[2]
    main()
