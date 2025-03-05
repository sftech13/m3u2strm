import os
import re
import json
import logging
import requests
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

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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


def parse_tv_filename(filename):
    cleaned = re.sub(r"(\[.*?\]|\{.*?\}|\(\d{4}\))", "", filename)
    match = re.search(r"[Ss](?:eason\s*)?(\d+)[Ee](\d+)", cleaned, re.IGNORECASE)
    if not match:
        logging.debug(f"Failed to parse SxxExx from filename: {filename}")
        return None, None, None
    season_num = match.group(1)
    episode_num = match.group(2)
    cleaned = re.sub(
        r"\s*[Ss](?:eason\s*)?\d+[Ee]\d+\s*", " ", cleaned, flags=re.IGNORECASE
    ).strip()
    core = cleaned.split("-")[0] if "-" in cleaned else cleaned
    show_name = re.sub(r"[^\w\s-]", "", core)
    show_name = re.sub(r"\s+", " ", show_name).strip().lower()
    logging.debug(
        f"Parsed TV filename '{filename}' as: show_name='{show_name}', season={season_num}, episode={episode_num}"
    )
    return show_name, season_num, episode_num


def parse_tv_m3u_entry(title):
    show_name, season_num, episode_num = parse_tv_filename(title)
    if show_name is None:
        return None, None, None, None
    return show_name, season_num, episode_num, title


def parse_m3u(m3u_file):
    vod_entries = []
    seen_titles = set()
    try:
        with open(m3u_file, "r", encoding="utf-8") as file:
            lines = file.readlines()
        logging.debug(f"Read {len(lines)} lines from M3U file: {m3u_file}")
    except Exception as e:
        logging.error(f"Failed to read M3U file: {e}")
        return []
    for i in range(len(lines)):
        if lines[i].startswith("#EXTINF"):
            match = re.search(r'group-title="([^"]*)",(.*)', lines[i])
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
                url = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if url and title not in seen_titles:
                    seen_titles.add(title)
                    vod_entries.append(
                        {"title": title, "url": url, "category": category}
                    )
                    logging.debug(f"Added entry: {title} ({category})")
    logging.info(f"Parsed {len(vod_entries)} entries from M3U file: {m3u_file}")
    return vod_entries


def extract_movie_details(title):
    match = re.match(r"(.*?)[\s\(\[](\d{4})[\)\]]$", title)
    if match:
        return sanitize_title(match.group(1)), match.group(2)
    return sanitize_title(title), None


def extract_tv_details(title):
    title = re.sub(r"\(\d{4}\)", "", title).strip()
    match = re.search(r"[Ss](?:eason\s*)?(\d+)[Ee](\d+)", title, re.IGNORECASE)
    if not match:
        logging.debug(f"No valid SxxExx found in TV title: {title}, skipping.")
        return None, None, None
    season_num = match.group(1)
    episode_num = match.group(2)
    title = re.sub(
        r"\s*[Ss](?:eason\s*)?\d+[Ee]\d+\s*", " ", title, flags=re.IGNORECASE
    ).strip()
    show_folder_name = sanitize_title(title)
    season_folder_name = f"Season {season_num}"
    episode_str = f"{show_folder_name} S{season_num}E{episode_num}"
    logging.debug(
        f"Extracted TV details: {show_folder_name}, {season_folder_name}, {episode_str}"
    )
    return show_folder_name, season_folder_name, episode_str


def get_movie_genres(title, year=None):
    api_key = TMDB_API
    if not api_key:
        logging.error("TMDB API key not found in environment variables.")
        return []
    search_url = "https://api.themoviedb.org/3/search/movie"
    params = {"api_key": api_key, "query": title}
    if year:
        params["year"] = year
    try:
        response = requests.get(search_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        logging.debug(
            f"TMDB API search for '{title}' returned {len(data.get('results', []))} results"
        )
    except Exception as e:
        logging.error(f"TMDB API search error for '{title}': {e}")
        return []
    if data.get("results"):
        movie_id = data["results"][0]["id"]
        details_url = f"https://api.themoviedb.org/3/movie/{movie_id}"
        try:
            details_response = requests.get(
                details_url, params={"api_key": api_key}, timeout=10
            )
            details_response.raise_for_status()
            details = details_response.json()
        except Exception as e:
            logging.error(f"TMDB API details error for '{title}': {e}")
            return []
        genres = details.get("genres", [])
        return [genre["name"] for genre in genres]
    return []


def build_existing_media_cache(directory, parser):
    existing_files = set()
    if not os.path.exists(directory):
        logging.warning(
            f"Directory {directory} does not exist; skipping this category."
        )
        return existing_files
    video_extensions = [".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".mpg", ".mpeg"]
    logging.info(f"Building media cache from directory: {directory}")
    for root, dirs, files in os.walk(directory, followlinks=True):
        for file in files:
            name, ext = os.path.splitext(file)
            if ext.lower() in video_extensions:
                parsed_val = parser(name)
                if parsed_val is not None:
                    if isinstance(parsed_val, tuple):
                        if None in parsed_val:
                            continue
                        normalized = " ".join(parsed_val)
                    else:
                        normalized = parsed_val
                    existing_files.add(normalized.lower())
                    logging.debug(f"Found file in {directory}: {normalized.lower()}")
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


def create_strm_files(
    vod_entries, movies_dir, tvshows_dir, docs_dir, cache, existing_media
):
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
                logging.info(f"Skipping TV entry without valid SxxExx pattern: {title}")
                continue
            show_name, season, episode_str = details
            target_folder = os.path.join(tvshows_dir, show_name, season)
            os.makedirs(target_folder, exist_ok=True)
            base_filename = episode_str
            parsed = parse_tv_filename(base_filename)
            if parsed is None or None in parsed:
                logging.info(
                    f"Skipping TV entry (unable to parse base filename): {title}"
                )
                continue
            normalized_tuple = (parsed[0], parsed[1], parsed[2])
            normalized_str = " ".join(tuple(str(x) for x in normalized_tuple))
            if normalized_str in existing_media:
                logging.info(
                    f"TV episode already exists for '{base_filename}'. Skipping .strm creation."
                )
                continue
            strm_file_path = os.path.join(target_folder, f"{base_filename}.strm")
        elif category == "documentary":
            doc_name, year = extract_movie_details(title)
            target_folder = os.path.join(
                docs_dir, f"{doc_name} ({year})" if year else doc_name
            )
            os.makedirs(target_folder, exist_ok=True)
            base_filename = f"{doc_name} ({year})" if year else doc_name
            if base_filename.lower() in existing_media:
                logging.info(
                    f"Documentary '{base_filename}' already exists. Skipping .strm creation."
                )
                continue
            strm_file_path = os.path.join(target_folder, f"{base_filename}.strm")
        else:
            movie_name, year = extract_movie_details(title)
            genres = get_movie_genres(movie_name, year)
            if "Documentary" in genres:
                category = "documentary"
                target_folder = os.path.join(
                    docs_dir, f"{movie_name} ({year})" if year else movie_name
                )
                os.makedirs(target_folder, exist_ok=True)
                base_filename = f"{movie_name} ({year})" if year else movie_name
                if base_filename.lower() in existing_media:
                    logging.info(
                        f"Documentary '{base_filename}' already exists. Skipping .strm creation."
                    )
                    continue
                strm_file_path = os.path.join(target_folder, f"{base_filename}.strm")
            else:
                target_folder = os.path.join(
                    movies_dir, f"{movie_name} ({year})" if year else movie_name
                )
                os.makedirs(target_folder, exist_ok=True)
                base_filename = f"{movie_name} ({year})" if year else movie_name
                if base_filename.lower() in existing_media:
                    logging.info(
                        f"Movie '{base_filename}' already exists. Skipping .strm creation."
                    )
                    continue
                strm_file_path = os.path.join(target_folder, f"{base_filename}.strm")
        if base_filename.lower() in existing_media:
            logging.info(
                f"Media file exists for '{base_filename}' (found in existing media cache). Skipping .strm creation."
            )
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
    logging.info(
        "Starting M3U to STRM conversion for Movies, TV Shows, and Documentaries..."
    )
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
        logging.info(
            f"Initialized existing media cache with {len(existing_media)} entries"
        )
    vod_entries = parse_m3u(M3U)
    if vod_entries:
        create_strm_files(
            vod_entries, MOVIES_DIR, TVSHOWS_DIR, DOCS_DIR, cache, existing_media
        )
    else:
        logging.warning("No entries found in the M3U file.")
    save_cache(cache)
    logging.info("All .strm files have been created successfully for Emby.")


if __name__ == "__main__":
    main()
