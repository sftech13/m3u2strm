import os
import re
import json
import logging
import requests
import asyncio
import aiohttp
import aiofiles
import platform
import unicodedata
import shutil
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import psutil
except ImportError:
    psutil = None

def load_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_file = os.path.join(script_dir, "config.json")
    with open(config_file, "r", encoding="utf-8") as file:
        return json.load(file)

config = load_config()


M3U = config["m3u"]
CACHE_FILE = config["cache_file"]
LOG_FILE = config["log_file"]
OUTPUT_DIR = config["output_dir"]
TMDB_API = config["tmdb_api"]
EXISTING_MEDIA_DIR = config["existing_media_dir"]
EXISTING_MEDIA_CACHE_FILE = config["existing_media_cache_file"]
TV_GROUP_KEYWORDS = config["tv_group_keywords"]
DOC_GROUP_KEYWORDS = config["doc_group_keywords"]
MOVIE_GROUP_KEYWORDS = config["movie_group_keywords"]
DRY_RUN = config.get("dry_run", False)
#MAX_WORKERS = config.get("max_workers", 5)

MOVIES_DIR = os.path.join(OUTPUT_DIR, "Movies")
TVSHOWS_DIR = os.path.join(OUTPUT_DIR, "TV Shows")
DOCS_DIR = os.path.join(OUTPUT_DIR, "Documentaries")
os.makedirs(MOVIES_DIR, exist_ok=True)
os.makedirs(TVSHOWS_DIR, exist_ok=True)
os.makedirs(DOCS_DIR, exist_ok=True)

logging.basicConfig(filename=LOG_FILE, level=logging.WARNING,
    format="%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console_handler.setFormatter(formatter)
logging.getLogger().addHandler(console_handler)

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as file:
            cache = json.load(file)
            logging.debug(f"Loaded cache with {len(cache)} entries")
            return cache
    logging.debug("No cache file found; starting with empty cache")
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as file:
        json.dump(cache, file, indent=4)
    logging.debug(f"Saved cache with {len(cache)} entries")

def strip_after_year(text):
    return re.sub(r"(\(\d{4}\)).*$", r"\1", text)

def remove_imdb_id(title):
    return re.sub(r"[\{\(]?\btt\d+\b[\}\)]?", "", title, flags=re.IGNORECASE)

def sanitize_title(title):
    title = title.strip()
    title = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode('ascii')
    title = remove_imdb_id(title)
    title = re.sub(r"[^\w\s\(\)-]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title

# Updated pattern for TV shows: gli alieni sono tra noi S01 E05
tv_pattern = re.compile(r"(?i)S(?:eason)?\s*(\d{1,4})\s*E(?:pisode)?\s*(\d{1,4})")

def parse_tv_filename(filename):
    cleaned = re.sub(r"(\[.*?\]|\{.*?\}|\(\d{4}\))", "", filename)
    match = tv_pattern.search(cleaned)
    if not match:
        logging.debug(f"Failed to parse TV pattern from filename: {filename}")
        return None, None, None
    season_num, episode_num = match.groups()
    cleaned = tv_pattern.sub(" ", cleaned).strip()
    core = cleaned.split("-")[0] if "-" in cleaned else cleaned
    show_name = re.sub(r"[^\w\s-]", "", core)
    show_name = re.sub(r"\s+", " ", show_name).strip().lower()
    logging.debug(f"Parsed TV filename '{filename}' as: show_name='{show_name}', season={season_num}, episode={episode_num}")
    return show_name, season_num, episode_num

def extract_tv_details(title):
    title = re.sub(r"\(\d{4}\)", "", title).strip()
    match = tv_pattern.search(title)
    if not match:
        logging.debug(f"No valid TV pattern found in title: {title}, skipping.")
        return None, None, None
    season_num, episode_num = match.groups()
    title = tv_pattern.sub(" ", title).strip()
    show_folder_name = sanitize_title(title)
    season_folder_name = f"Season {season_num}"
    episode_str = f"{show_folder_name} S{season_num}E{episode_num}"
    logging.debug(f"Extracted TV details: {show_folder_name}, {season_folder_name}, {episode_str}")
    return show_folder_name, season_folder_name, episode_str

def parse_tv_m3u_entry(title):
    show_name, season_num, episode_num = parse_tv_filename(title)
    if show_name is None:
        return None, None, None, None
    return show_name, season_num, episode_num, title

async def parse_m3u_async(m3u_file):
    async with aiofiles.open(m3u_file, "r", encoding="utf-8") as f:
        content = await f.read()
    blocks = re.split(r"(?=#EXTINF)", content)
    vod_entries = []
    seen_titles = set()
    group_pattern = re.compile(r'group-title="([^"]*)",(.*)')
    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        if lines[0].startswith("#EXTINF"):
            match = group_pattern.search(lines[0])
            if match:
                group = match.group(1).strip().lower()
                raw_title = match.group(2).strip()
                raw_title = strip_after_year(raw_title)
                title = sanitize_title(raw_title)
                if any(keyword in group for keyword in TV_GROUP_KEYWORDS):
                    category = "tvshow"
                elif any(keyword in group for keyword in DOC_GROUP_KEYWORDS):
                    category = "documentary"
                elif any(keyword in group for keyword in MOVIE_GROUP_KEYWORDS):
                    category = "movie"
                else:
                    category = "movie"
                url = lines[1].strip() if len(lines) > 1 else ""
                if url and title not in seen_titles:
                    seen_titles.add(title)
                    vod_entries.append({"title": title, "url": url, "category": category})
    logging.info(f"Parsed {len(vod_entries)} entries from M3U file: {m3u_file}")
    return vod_entries

def parse_m3u(m3u_file):
    return asyncio.run(parse_m3u_async(m3u_file))

def extract_movie_details(title):
    match = re.match(r"(.*?)[\s\(\[](\d{4})[\)\]]$", title)
    if match:
        return sanitize_title(match.group(1)), match.group(2)
    return sanitize_title(title), None

async def get_movie_genres_async(session, title, year=None):
    params = {"api_key": TMDB_API, "query": title}
    if year:
        params["year"] = year
    search_url = "https://api.themoviedb.org/3/search/movie"
    try:
        async with session.get(search_url, params=params, timeout=10) as response:
            data = await response.json()
            logging.debug(f"TMDB API search for '{title}' returned {len(data.get('results', []))} results")
    except Exception as e:
        logging.error(f"TMDB API search error for '{title}': {e}")
        return []
    if data.get("results"):
        movie_id = data["results"][0]["id"]
        details_url = f"https://api.themoviedb.org/3/movie/{movie_id}"
        try:
            async with session.get(details_url, params={"api_key": TMDB_API}, timeout=10) as details_response:
                details = await details_response.json()
        except Exception as e:
            logging.error(f"TMDB API details error for '{title}': {e}")
            return []
        genres = details.get("genres", [])
        return [genre["name"] for genre in genres]
    return []

def get_movie_genres(title, year=None):
    async def wrapper():
        async with aiohttp.ClientSession() as session:
            return await get_movie_genres_async(session, title, year)
    return asyncio.run(wrapper())

def build_existing_media_cache(directory):
    existing_files = set()
    if not os.path.exists(directory):
        logging.warning(f"Directory {directory} does not exist; skipping this category.")
        return existing_files
    video_extensions = [".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".mpg", ".mpeg"]
    total = 0
    for root, dirs, files in os.walk(directory, followlinks=True):
        for file in files:
            name, ext = os.path.splitext(file)
            if ext.lower() in video_extensions:
                total += 1
    logging.info(f"Building media cache from directory: {directory}")
    with tqdm(total=total, desc=f"Scanning {directory}", unit="files") as pbar:
        for root, dirs, files in os.walk(directory, followlinks=True):
            for file in files:
                name, ext = os.path.splitext(file)
                if ext.lower() in video_extensions:
                    if "tv shows" in root.lower():
                        parsed_val = parse_tv_filename(name)
                    else:
                        parsed_val = sanitize_title(strip_after_year(name)).lower()
                    if parsed_val is not None:
                        if isinstance(parsed_val, tuple):
                            if None in parsed_val:
                                pbar.update(1)
                                continue
                            normalized = " ".join(parsed_val)
                        else:
                            normalized = parsed_val
                        existing_files.add(normalized.lower())
                        logging.debug(f"Found file in {root}: {normalized.lower()}")
                    pbar.update(1)
    logging.info(f"Built cache with {len(existing_files)} entries from {directory}")
    return existing_files

def build_all_caches(directory):
    return build_existing_media_cache(directory)

def load_existing_media_cache():
    if os.path.exists(EXISTING_MEDIA_CACHE_FILE):
        with open(EXISTING_MEDIA_CACHE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            logging.debug(f"Loaded existing media cache with {len(data)} entries")
            return set(data)
    logging.debug("No existing media cache file found")
    return set()

def save_existing_media_cache(existing_files):
    with open(EXISTING_MEDIA_CACHE_FILE, "w", encoding="utf-8") as file:
        json.dump(list(existing_files), file, indent=4)
    logging.debug(f"Saved existing media cache with {len(existing_files)} entries")

def get_recommended_max_workers():
    arch = platform.machine()
    cpu_count = os.cpu_count() or 1
    if psutil:
        total_mem_gb = psutil.virtual_memory().total / (1024 ** 3)
    else:
        total_mem_gb = 4
    recommended = min(max(1, int(total_mem_gb / 0.5)), cpu_count * 2)
    logging.info(f"System architecture: {arch}, CPU cores: {cpu_count}, Memory: {total_mem_gb:.2f}GB, recommended parallel writes: {recommended}")
    return recommended

def should_ignore_title(title, ignore_list):
    title_lower = title.lower()
    for keyword in ignore_list:
        if keyword.lower() in title_lower:
            logging.debug(f"Ignoring title '{title}' due to ignore keyword '{keyword}'")
            return True
    return False

def process_entry(entry, movies_dir, tvshows_dir, docs_dir, existing_media, DRY_RUN):
    title = entry["title"]
    url = entry["url"]
    category = entry["category"]

    if category == "tvshow":
        ignore_list = config.get("ignore_keywords", {}).get("tvshows", [])
    else:
        ignore_list = config.get("ignore_keywords", {}).get("movies", [])

    if should_ignore_title(title, ignore_list):
        logging.info(f"Skipping '{title}' due to ignore keywords")
        return None

    if title in existing_media:
        logging.debug(f"Skipping (exists): {title}")
        return None

    if category == "tvshow":
        details = extract_tv_details(title)
        if not details or details[0] is None:
            logging.debug(f"Skipping TV entry without valid pattern: {title}")
            return None
        show_name, season, episode_str = details
        base_filename = episode_str
        parsed = parse_tv_filename(base_filename)
        if parsed is None or None in parsed:
            logging.debug(f"Skipping TV entry (unable to parse base filename): {title}")
            return None
        normalized_tuple = (parsed[0], parsed[1], parsed[2])
        normalized_str = " ".join(str(x) for x in normalized_tuple)
        if normalized_str in existing_media:
            logging.debug(f"TV episode already exists for '{base_filename}'. Skipping .strm creation.")
            return None
        target_folder = os.path.join(tvshows_dir, show_name, season)
        os.makedirs(target_folder, exist_ok=True)
        strm_file_path = os.path.join(target_folder, f"{base_filename}.strm")
    elif category == "documentary":
        doc_name, year = extract_movie_details(title)
        base_filename = f"{doc_name} ({year})" if year else doc_name
        if base_filename.lower() in existing_media:
            logging.debug(f"Documentary '{base_filename}' already exists. Skipping .strm creation.")
            return None
        target_folder = os.path.join(docs_dir, f"{doc_name} ({year})" if year else doc_name)
        os.makedirs(target_folder, exist_ok=True)
        strm_file_path = os.path.join(target_folder, f"{base_filename}.strm")
    else:
        movie_name, year = extract_movie_details(title)
        genres = get_movie_genres(movie_name, year)
        if "Documentary" in genres:
            category = "documentary"
            base_filename = f"{movie_name} ({year})" if year else movie_name
            if base_filename.lower() in existing_media:
                logging.debug(f"Documentary '{base_filename}' already exists. Skipping .strm creation.")
                return None
            target_folder = os.path.join(docs_dir, f"{movie_name} ({year})" if year else movie_name)
            os.makedirs(target_folder, exist_ok=True)
            strm_file_path = os.path.join(target_folder, f"{base_filename}.strm")
        else:
            base_filename = f"{movie_name} ({year})" if year else movie_name
            if base_filename.lower() in existing_media:
                logging.debug(f"Movie '{base_filename}' already exists. Skipping .strm creation.")
                return None
            target_folder = os.path.join(movies_dir, f"{movie_name} ({year})" if year else movie_name)
            os.makedirs(target_folder, exist_ok=True)
            strm_file_path = os.path.join(target_folder, f"{base_filename}.strm")
            
    if base_filename.lower() in existing_media:
        logging.debug(f"Media file exists for '{base_filename}' (in cache). Skipping .strm creation.")
        return None

    if DRY_RUN:
        logging.info(f"[DRY RUN] Would create: {strm_file_path} with URL: {url}")
        return (title, url, strm_file_path)
    else:
        try:
            with open(strm_file_path, "w", encoding="utf-8") as strm_file:
                strm_file.write(url + "\n")
            logging.debug(f"Created: {strm_file_path}")
            return (title, url, strm_file_path)
        except Exception as e:
            logging.error(f"Failed to create {strm_file_path}: {e}")
            return None

def create_strm_files(vod_entries, movies_dir, tvshows_dir, docs_dir, cache, existing_media, DRY_RUN, max_workers):
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_entry, entry, movies_dir, tvshows_dir, docs_dir, existing_media, DRY_RUN): entry 
                   for entry in vod_entries}
        for future in tqdm(as_completed(futures), total=len(futures), desc="Creating STRM files", unit="entry"):
            result = future.result()
            if result:
                title, url, strm_file_path = result
                cache[title] = {"url": url, "path": strm_file_path}

def cleanup_removed_entries_from_cache(cache, current_entries):
    current_titles = {entry["title"] for entry in current_entries}
    titles_to_remove = [title for title in list(cache.keys()) if title not in current_titles]
    
    for title in titles_to_remove:
        entry_val = cache[title]
        if not isinstance(entry_val, dict):
            logging.warning(f"Skipping '{title}' as its cache entry is in an unexpected format.")
            continue
        
        strm_file_path = entry_val.get("path")
        if strm_file_path:
            parent_dir = os.path.dirname(strm_file_path)
            if os.path.exists(parent_dir):
                try:
                    shutil.rmtree(parent_dir)
                    logging.info(f"Removed directory and all its contents: {parent_dir}")
                except Exception as e:
                    logging.error(f"Error removing directory {parent_dir}: {e}")
        del cache[title]


def main():
    logging.info("Starting M3U to STRM conversion for Movies, TV Shows, and Documentaries...")
    cache = load_cache() 
    
    combined_existing = build_all_caches(EXISTING_MEDIA_DIR)
    existing_media = load_existing_media_cache()
    if not existing_media:
        existing_media = combined_existing
        save_existing_media_cache(existing_media)
        logging.info(f"Initialized existing media cache with {len(existing_media)} entries")
    
    vod_entries = parse_m3u(M3U)
    if vod_entries:
        recommended_workers = get_recommended_max_workers()
        configured_workers = config.get("max_workers", recommended_workers)
        final_max_workers = min(configured_workers, recommended_workers)
        logging.info(f"Using {final_max_workers} worker threads for file creation (configured: {configured_workers}, recommended: {recommended_workers})")
        create_strm_files(vod_entries, MOVIES_DIR, TVSHOWS_DIR, DOCS_DIR, cache, existing_media, DRY_RUN, final_max_workers)
    else:
        logging.warning("No entries found in the M3U file.")

    save_cache(cache)
    logging.info("All .strm files have been created successfully for Emby.")

    logging.info("Starting cleanup of outdated .strm files based on the cache...")
    cleanup_removed_entries_from_cache(cache, vod_entries)
    
    save_cache(cache)
    logging.info("Cleanup completed.")

if __name__ == "__main__":
    main()
