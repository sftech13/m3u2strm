# M3U to STRM Converter

This tool automates the conversion of a single M3U file containing movies, TV shows, documentaries, and more into Emby-compatible `.strm` files. It checks your existing media library to avoid creating duplicate stream files, normalizes titles for accurate matching, and supports categories such as Movies, TV Shows, Documentaries, Animation, and StandUp.

## Features

- **Single M3U Input:**  
  Process a single M3U file that includes all media entries.

- **Category Detection:**  
  Classify entries into TV shows, movies, or documentaries based on group keywords in the M3U file.

- **Normalization:**  
  Clean and standardize titles by stripping extra text (like years and IMDb IDs) and descriptive information.

- **Existing Media Check:**  
  Scan user-specified directories to build caches for existing media files (with separate handling for TV shows and generic media).  
  TV shows (and Animation) are parsed using season/episode detection while Movies, Documentaries, and StandUp use generic normalization.

- **Parallel Processing:**  
  Build caches in parallel to speed up processing when scanning large media libraries.

- **Dry-Run Mode:**  
  Test the conversion process without writing any files, so you can verify what would be created.

- **TMDB Integration:**  
  Optionally fetch movie genres via the TMDB API for additional classification (e.g., automatically treating a movie as a documentary if appropriate).

## Installation

1. **Clone the Repository:**

   ```bash
   git clone https://github.com/sftech13/m3u2strm.git
   cd m3u2strm
   ```

2. **Install Dependencies:**

   The script requires Python 3 and the following packages:
   - `requests`


## Configuration

The script uses a configuration file named `config.json` that holds all user-adjustable settings. Create or edit `config.json` in the root directory with the following keys (example values have been omitted):

```json
{
  "m3u": "<path to your M3U file>",
  "cache_file": "<path to your cache file>",
  "log_file": "<path to your log file>",
  "output_dir": "<base output directory for STRM files>",
  "tmdb_api": "<your TMDB API key>",
  "existing_media_dir": "<base directory for your existing media>",
  "existing_media_cache_file": "<path to your existing media cache file>",
  "tv_group_keywords": ["<keyword1>", "<keyword2>"],
  "doc_group_keywords": ["<keyword1>", "<keyword2>"],
  "movie_group_keywords": ["<keyword1>"],
  "movies_existing_dir": "<path to existing movies directory>",
  "tv_existing_dir": "<path to existing TV shows directory>",
  "docs_existing_dir": "<path to existing documentaries directory>",
  "animation_existing_dir": "<path to existing animation directory>",
  "standup_existing_dir": "<path to existing standup directory>",
  "dry_run": false,
  "max_workers": 5
}
```

**User Options:**

- **m3u:**  
  Path to your input M3U file.

- **cache_file:**  
  File used to store the conversion cache (to avoid duplicate processing).

- **log_file:**  
  Log file path for debugging and information.

- **output_dir:**  
  Base output directory where subdirectories (Movies, TV Shows, Documentaries) will be created.

- **tmdb_api:**  
  Your TMDB API key, used for optionally fetching movie genres.

- **existing_media_dir:**  
  Base directory for your existing media library.

- **existing_media_cache_file:**  
  File used to store a combined cache of existing media from various categories.

- **tv_group_keywords, doc_group_keywords, movie_group_keywords:**  
  Lists of keywords used to classify M3U entries.

- **movies_existing_dir, tv_existing_dir, docs_existing_dir, animation_existing_dir, standup_existing_dir:**  
  Directories under your existing media library for each category.  
  TV and Animation directories are parsed using TV logic (season/episode detection); the rest use generic normalization.

- **dry_run:**  
  If set to `true`, the script will only log actions (no files will be created). Set to `false` to actually create `.strm` files.

- **max_workers:**  
  Maximum number of worker threads for parallel processing when building caches.

## Usage

Run the script from the command line:

```bash
python3 M3UtoStrm.py
```

If `dry_run` is enabled in your configuration, the script will log the actions it would take without writing any files. Once you're satisfied with the output, disable dry-run mode in your `config.json` and run the script again to create the `.strm` files.

## How It Works

1. **Configuration Loading:**  
   Reads user options from `config.json`.

2. **Cache Building:**  
   *First Run Can Take A While For Large Libaries*
   Scans specified directories (Movies, TV Shows, Documentaries, Animation, StandUp) in parallel using the `build_all_caches` function.  
   TV and Animation directories use a TV-specific parser, while other categories use a generic parser.

3. **M3U Parsing:**  
   Reads the M3U file and classifies each entry as a TV show, movie, or documentary based on group keywords.

4. **Existing Media Check:**  
   Before creating a `.strm` file, the script compares the normalized title of the entry with the combined existing media cache to avoid duplicates.

5. **.strm File Creation:**  
   Creates `.strm` files in the appropriate output directory (Movies, TV Shows, or Documentaries). If dry-run mode is enabled, it logs what would be created instead of writing files.

## Troubleshooting

- **Slow Performance:**  
  Ensure that your media directories are not overly large. The parallel processing should help, but consider running in dry-run mode first for testing.

- **Logging:**  
  Check the log file (as specified in `config.json`) for detailed debug messages if things aren’t working as expected.

## License

This project is released under the [MIT License](LICENSE).

---

Feel free to adjust the README to suit your needs. Let me know if you have any further questions, matt!
