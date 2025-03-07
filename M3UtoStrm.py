import os
import re
import json
import logging
import requests
import asyncio
import aiohttp
import aiofiles
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

def load_config(config_file="config.json"):
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
MOVIES_EXISTING_DIR = config.get("movies_existing_dir", EXISTING_MEDIA_DIR)
TV_EXISTING_DIR = config.get("tv_existing_dir", EXISTING_MEDIA_DIR)
DOCS_EXISTING_DIR = config.get("docs_existing_dir", EXISTING_MEDIA_DIR)
ANIMATION_EXISTING_DIR = config.get("animation_existing_dir", EXISTING_MEDIA_DIR)
STANDUP_EXISTING_DIR = config.get("standup_existing_dir", EXISTING_MEDIA_DIR)
DRY_RUN = config.get("dry_run", False)
MAX_WORKERS = config.get("max_workers", 5)

MOVIES_DIR = os.path.join(OUTPUT_DIR, "Movies")
TVSHOWS_DIR = os.path.join(OUTPUT_DIR, "TV Shows")
DOCS_DIR = os.path.join(OUTPUT_DIR, "Documentaries")
os.makedirs(MOVIES_DIR, exist_ok=True)
os.makedirs(TVSHOWS_DIR, exist_ok=True)
os.makedirs(DOCS_DIR, exist_ok=True)

logging.basicConfig(filename=LOG_FILE, level=logging.INFO, 
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
    title = remove_imdb_id(title)
    title = re.sub(r"[^\w\s\(\)-]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title

# New robust TV season/episode pattern Thx @mickle026
tv_pattern = re.compile(
    r"(?i)(\s+(S|Season|Sezon|Serie|Series|Seazon|シーズン|시즌)\s?\d{1,4}[\s._-]*(?:E|Episode|ep|e|エピソード|에피소드|화|話)[\s]*?(\d{1,4}))"
    r"|(?:^|\s)S(?P<season>\d{1,2})\s*E(?P<episode>\d{1,2})"
    r"|(?:Episode[\s._-]*(?P<episodeOnly>\d{1,4}))"
    r"|(?:エピソード[\s._-]*(?P<episodeOnly>\d{1,4}))"
    r"|(?:에피소드[\s._-]*(?P<episodeOnly>\d{1,4}))"
    r"|(?:화[\s._-]*(?P<episodeOnly>\d{1,4}))"
    r"|(?:話[\s._-]*(?P<episodeOnly>\d{1,4}))"
    r"|(?P<seasonOnly>\d{1,2})\s*-\s*(?:Episode|エピソード|에피소드|화|話)\s*(?P<ep>\d{1,4})",
    re.IGNORECASE
)

def parse_tv_filename(filename):
    cleaned = re.sub(r"(\[.*?\]|\{.*?\}|\(\d{4}\))", "", filename)
    match = tv_pattern.search(cleaned)
    if not match:
        logging.debug(f"Failed to parse TV pattern from filename: {filename}")
        return None, None, None
    d = match.groupdict()
    if d.get("season") and d.get("episode"):
        season_num = d["season"]
        episode_num = d["episode"]
    elif d.get("seasonOnly") and d.get("ep"):
        season_num = d["seasonOnly"]
        episode_num = d["ep"]
    elif match.group(2) and match.group(3):
        season_num = match.group(2)
        episode_num = match.group(3)
    elif d.get("episodeOnly"):
        return None, None, None
    else:
        return None, None, None
    cleaned_name = tv_pattern.sub(" ", filename).strip()
    show_name = sanitize_title(cleaned_name).lower()
    logging.debug(f"Parsed TV filename '{filename}' as: show_name='{show_name}', season={season_num}, episode={episode_num}")
    return show_name, season_num, episode_num

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

def extract_tv_details(title):
    title = re.sub(r"\(\d{4}\)", "", title).strip()
    regex = tv_pattern
    match = regex.search(title)
    if not match:
        logging.debug(f"No valid TV pattern found in title: {title}, skipping.")
        return None, None, None
    d = match.groupdict()
    if d.get("season") and d.get("episode"):
        season_num = d["season"]
        episode_num = d["episode"]
    elif d.get("seasonOnly") and d.get("ep"):
        season_num = d["seasonOnly"]
        episode_num = d["ep"]
    elif match.group(2) and match.group(3):
        season_num = match.group(2)
        episode_num = match.group(3)
    else:
        return None, None, None
    cleaned = regex.sub(" ", title).strip()
    show_folder_name = sanitize_title(cleaned).lower()
    season_folder_name = f"Season {season_num}"
    episode_str = f"{show_folder_name} S{season_num}E{episode_num}"
    logging.debug(f"Extracted TV details: {show_folder_name}, {season_folder_name}, {episode_str}")
    return show_folder_name, season_folder_name, episode_str

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

def build_existing_media_cache(directory, parser):
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
                    parsed_val = parser(name)
                    if parsed_val is not None:
                        if isinstance(parsed_val, tuple):
                            if None in parsed_val:
                                pbar.update(1)
                                continue
                            normalized = " ".join(parsed_val)
                        else:
                            normalized = parsed_val
                        existing_files.add(normalized.lower())
                        logging.debug(f"Found file in {directory}: {normalized.lower()}")
                    pbar.update(1)
    logging.info(f"Built cache with {len(existing_files)} entries from {directory}")
    return existing_files

def tv_parser(filename):
    return parse_tv_filename(filename)

def generic_parser(filename):
    return sanitize_title(strip_after_year(filename)).lower()

def build_all_caches(directories):
    caches = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_cat = {}
        for cat, path in directories.items():
            parser = tv_parser if cat in ["tv", "animation"] else generic_parser
            future = executor.submit(build_existing_media_cache, path, parser)
            future_to_cat[future] = cat
        for future in as_completed(future_to_cat):
            cat = future_to_cat[future]
            try:
                caches[cat] = future.result()
            except Exception as exc:
                logging.error(f"Error building cache for {cat}: {exc}")
    return caches

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

def create_strm_files(vod_entries, movies_dir, tvshows_dir, docs_dir, cache, existing_media):
    for entry in vod_entries:
        title = entry["title"]
        url = entry["url"]
        category = entry["category"]
        if title in cache and cache[title] == url:
            logging.debug(f"Skipping (cached): {title}")
            continue
        if category == "tvshow":
            details = extract_tv_details(title)
            if not details or details[0] is None:
                logging.debug(f"Skipping TV entry without valid SxxExx pattern: {title}")
                continue
            show_name, season, episode_str = details
            target_folder = os.path.join(tvshows_dir, show_name, season)
            os.makedirs(target_folder, exist_ok=True)
            base_filename = episode_str
            parsed = parse_tv_filename(base_filename)
            if parsed is None or None in parsed:
                logging.debug(f"Skipping TV entry (unable to parse base filename): {title}")
                continue
            normalized_tuple = (parsed[0], parsed[1], parsed[2])
            normalized_str = " ".join(tuple(str(x) for x in normalized_tuple))
            if normalized_str in existing_media:
                logging.debug(f"TV episode already exists for '{base_filename}'. Skipping .strm creation.")
                continue
            strm_file_path = os.path.join(target_folder, f"{base_filename}.strm")
        elif category == "documentary":
            doc_name, year = extract_movie_details(title)
            target_folder = os.path.join(docs_dir, f"{doc_name} ({year})" if year else doc_name)
            os.makedirs(target_folder, exist_ok=True)
            base_filename = f"{doc_name} ({year})" if year else doc_name
            if base_filename.lower() in existing_media:
                logging.debug(f"Documentary '{base_filename}' already exists. Skipping .strm creation.")
                continue
            strm_file_path = os.path.join(target_folder, f"{base_filename}.strm")
        else:
            movie_name, year = extract_movie_details(title)
            genres = get_movie_genres(movie_name, year)
            if "Documentary" in genres:
                category = "documentary"
                target_folder = os.path.join(docs_dir, f"{movie_name} ({year})" if year else movie_name)
                os.makedirs(target_folder, exist_ok=True)
                base_filename = f"{movie_name} ({year})" if year else movie_name
                if base_filename.lower() in existing_media:
                    logging.debug(f"Documentary '{base_filename}' already exists. Skipping .strm creation.")
                    continue
                strm_file_path = os.path.join(target_folder, f"{base_filename}.strm")
            else:
                target_folder = os.path.join(movies_dir, f"{movie_name} ({year})" if year else movie_name)
                os.makedirs(target_folder, exist_ok=True)
                base_filename = f"{movie_name} ({year})" if year else movie_name
                if base_filename.lower() in existing_media:
                    logging.debug(f"Movie '{base_filename}' already exists. Skipping .strm creation.")
                    continue
                strm_file_path = os.path.join(target_folder, f"{base_filename}.strm")
        if base_filename.lower() in existing_media:
            logging.debug(f"Media file exists for '{base_filename}' (found in existing media cache). Skipping .strm creation.")
            continue
        if DRY_RUN:
            logging.info(f"[DRY RUN] Would create: {strm_file_path} with URL: {url}")
        else:
            try:
                with open(strm_file_path, "w", encoding="utf-8") as strm_file:
                    strm_file.write(url + "\n")
                logging.info(f"Created: {strm_file_path}")
                cache[title] = url
            except Exception as e:
                logging.error(f"Failed to create {strm_file_path}: {e}")

def main():
    logging.info("Starting M3U to STRM conversion for Movies, TV Shows, and Documentaries...")
    cache = load_cache()
    directories = {
        "movies": MOVIES_EXISTING_DIR,
        "tv": TV_EXISTING_DIR,
        "docs": DOCS_EXISTING_DIR,
        "animation": ANIMATION_EXISTING_DIR,
        "standup": STANDUP_EXISTING_DIR,
    }
    caches = build_all_caches(directories)
    combined_existing = set()
    combined_existing.update(caches.get("movies", set()))
    combined_existing.update(caches.get("docs", set()))
    combined_existing.update(caches.get("animation", set()))
    combined_existing.update(caches.get("standup", set()))
    combined_existing.update(caches.get("tv", set()))
    existing_media = load_existing_media_cache()
    if not existing_media:
        existing_media = combined_existing
        save_existing_media_cache(existing_media)
        logging.info(f"Initialized existing media cache with {len(existing_media)} entries")
    vod_entries = parse_m3u(M3U)
    if vod_entries:
        create_strm_files(vod_entries, MOVIES_DIR, TVSHOWS_DIR, DOCS_DIR, cache, existing_media)
    else:
        logging.warning("No entries found in the M3U file.")
    save_cache(cache)
    logging.info("All .strm files have been created successfully for Emby.")

if __name__ == "__main__":
    main()
