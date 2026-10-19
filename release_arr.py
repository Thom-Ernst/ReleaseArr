import os
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Set, Optional
import requests
import qbittorrentapi
from dotenv import load_dotenv
import schedule

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

SONARR_URL = os.getenv("SONARR_URL", "http://localhost:8989").rstrip("/")
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "")
QBITTORRENT_URL = os.getenv("QBITTORRENT_URL", "http://localhost:8080")
QBITTORRENT_USERNAME = os.getenv("QBITTORRENT_USERNAME", "admin")
QBITTORRENT_PASSWORD = os.getenv("QBITTORRENT_PASSWORD", "adminadmin")
DELAY_MINUTES = int(os.getenv("DELAY_MINUTES", "120"))
FAKE_EXTENSIONS = [ext.strip().lower() for ext in os.getenv("FAKE_EXTENSIONS", ".exe,.iso,.scr,.bat").split(",")]
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))
FULL_SCAN_INTERVAL_MINUTES = int(os.getenv("FULL_SCAN_INTERVAL_MINUTES", "60"))
# Releases published earlier than (air time - grace) are treated as fakes: a real release cannot exist before the episode aired.
PRE_AIR_GRACE_MINUTES = int(os.getenv("PRE_AIR_GRACE_MINUTES", "0"))
# When set, log every write (monitor toggles, queue removal, grabs, torrent deletion) instead of performing it.
DRY_RUN = os.getenv("DRY_RUN", "0").strip().lower() in ("1", "true", "yes", "on")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def pick_release(releases: List[Dict[str, Any]], air_date_utc: datetime) -> Optional[Dict[str, Any]]:
    """Best release Sonarr would accept that was not published before the episode aired.

    Old fakes seeded before the air date stay in the indexers' search results, so a plain
    EpisodeSearch right after the air date can still grab one. Sonarr sorts decisions by
    quality profile, custom format score and seeders; releaseWeight is that sort position
    (lower is better).
    """
    cutoff = air_date_utc - timedelta(minutes=PRE_AIR_GRACE_MINUTES)
    candidates = []
    for r in releases:
        if r.get("rejected") or not r.get("downloadAllowed", True):
            continue
        published = r.get("publishDate")
        if not published:
            continue
        try:
            if parse_utc(published) < cutoff:
                logger.info(f"Skipping pre-air release: {r.get('title')} (published {published})")
                continue
        except ValueError:
            continue
        candidates.append(r)
    if not candidates:
        return None
    return min(candidates, key=lambda r: r.get("releaseWeight", 0))


class SonarrClient:
    def __init__(self, url: str, api_key: str):
        self.url: str = url
        self.headers: Dict[str, str] = {"X-Api-Key": api_key}
        self.session: requests.Session = requests.Session()
        self.session.headers.update(self.headers)

    def get_series(self) -> List[Dict[str, Any]]:
        url = f"{self.url}/api/v3/series"
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()

    def get_episodes(self, series_id: int) -> List[Dict[str, Any]]:
        url = f"{self.url}/api/v3/episode"
        response = self.session.get(url, params={"seriesId": series_id})
        response.raise_for_status()
        return response.json()

    def get_calendar(self, start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        url = f"{self.url}/api/v3/calendar"
        params = {
            "start": start_date.strftime("%Y-%m-%d"),
            "end": end_date.strftime("%Y-%m-%d"),
            "unmonitored": "true"
        }
        response = self.session.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def update_episodes_monitor_status(self, episode_ids: List[int], monitored: bool) -> None:
        if not episode_ids:
            return
        if DRY_RUN:
            logger.info(f"[DRY RUN] would set monitored={monitored} on {len(episode_ids)} episodes: {episode_ids}")
            return
        url = f"{self.url}/api/v3/episode/monitor"
        data = {
            "episodeIds": episode_ids,
            "monitored": monitored
        }
        response = self.session.put(url, json=data)
        response.raise_for_status()

    def get_episode(self, episode_id: int) -> Dict[str, Any]:
        response = self.session.get(f"{self.url}/api/v3/episode/{episode_id}")
        response.raise_for_status()
        return response.json()

    def get_releases(self, episode_id: int) -> List[Dict[str, Any]]:
        """Interactive-search results for one episode (slow: queries every indexer)."""
        response = self.session.get(f"{self.url}/api/v3/release", params={"episodeId": episode_id}, timeout=180)
        response.raise_for_status()
        return response.json()

    def grab_release(self, release: Dict[str, Any]) -> None:
        response = self.session.post(
            f"{self.url}/api/v3/release",
            json={"guid": release["guid"], "indexerId": release["indexerId"]},
            timeout=60,
        )
        response.raise_for_status()

    def search_after_air(self, episode_ids: List[int]) -> None:
        """Replacement for a plain EpisodeSearch: grab the best release published after the air date."""
        now = datetime.now(timezone.utc)
        for episode_id in episode_ids:
            try:
                ep = self.get_episode(episode_id)
                air_str = ep.get("airDateUtc")
                if not air_str:
                    logger.info(f"Episode {episode_id} has no air date; leaving it to Sonarr's own RSS sync.")
                    continue
                air_date = parse_utc(air_str)
                if now < air_date + timedelta(minutes=DELAY_MINUTES):
                    logger.info(f"Episode {episode_id} has not aired yet; not searching.")
                    continue
                best = pick_release(self.get_releases(episode_id), air_date)
                if not best:
                    logger.info(f"No post-air release available yet for episode {episode_id}; RSS sync will pick one up.")
                    continue
                if DRY_RUN:
                    logger.info(f"[DRY RUN] would grab '{best['title']}' from {best.get('indexer')} for episode {episode_id}")
                    continue
                self.grab_release(best)
                logger.info(f"Grabbed '{best['title']}' from {best.get('indexer')} for episode {episode_id}")
            except Exception as e:
                logger.error(f"Search for episode {episode_id} failed: {e}")

    def get_torrent_categories(self) -> List[str]:
        url = f"{self.url}/api/v3/downloadclient"
        categories: Set[str] = set()
        response = self.session.get(url)
        response.raise_for_status()
        clients = response.json()
        for client in clients:
            if client.get("protocol") == "torrent" and client.get("enable"):
                for field in client.get("fields", []):
                    if field.get("name") in ("tvCategory", "category"):
                        val = field.get("value")
                        if val:
                            categories.add(val)
        return list(categories)

    def get_queue(self) -> List[Dict[str, Any]]:
        # Sonarr pages the queue at 10 records by default; a long queue would otherwise look mostly absent.
        url = f"{self.url}/api/v3/queue"
        records: List[Dict[str, Any]] = []
        page = 1
        while True:
            response = self.session.get(url, params={"page": page, "pageSize": 500})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or "records" not in data:
                return data
            records.extend(data["records"])
            if len(records) >= data.get("totalRecords", 0) or not data["records"]:
                return records
            page += 1

    def mark_download_failed(self, queue_id: int) -> None:
        if DRY_RUN:
            logger.info(f"[DRY RUN] would remove queue item {queue_id} from client and blocklist it")
            return
        url = f"{self.url}/api/v3/queue/{queue_id}"
        params = {
            "removeFromClient": "true",
            "blocklist": "true",
            # Sonarr's own auto-redownload would pick the best release without the pre-air filter.
            "skipRedownload": "true"
        }
        response = self.session.delete(url, params=params)
        response.raise_for_status()

class QbitClient:
    def __init__(self, host: str, username: str, password: str):
        self.host = host
        self.username = username
        self.password = password
        self.client = qbittorrentapi.Client(host=host, username=username, password=password)
        self.is_logged_in = False

    def login(self) -> bool:
        """Attempts to log in to qBittorrent. Returns True if successful."""
        try:
            self.client.auth_log_in()
            self.is_logged_in = True
            return True
        except Exception as e:
            # With "bypass authentication for clients on localhost/whitelisted subnets" the login
            # call can be refused while the rest of the API is open, so probe before giving up.
            try:
                self.client.app_version()
                logger.info(f"qBittorrent login failed ({e}) but the API is reachable without it; continuing.")
                self.is_logged_in = True
                return True
            except Exception:
                logger.error(f"qBittorrent connection failed during login: {e}")
                self.is_logged_in = False
                return False

    def get_torrents(self) -> Any:
        return self.client.torrents_info()

    def get_torrent_files(self, torrent_hash: str) -> Any:
        return self.client.torrents_files(torrent_hash=torrent_hash)

    def delete_torrent(self, torrent_hash: str) -> None:
        if DRY_RUN:
            logger.info(f"[DRY RUN] would delete torrent {torrent_hash} from qBittorrent with its files")
            return
        self.client.torrents_delete(delete_files=True, torrent_hashes=torrent_hash)

def process_episodes(sonarr: SonarrClient, episodes: List[Dict[str, Any]], series_dict: Dict[int, Dict[str, Any]]) -> None:
    """Core logic to evaluate episodes and update their monitored status."""
    now = datetime.now(timezone.utc)
    delay_delta = timedelta(minutes=DELAY_MINUTES)
    
    episodes_to_unmonitor: List[int] = []
    episodes_to_monitor: List[int] = []

    for ep in episodes:
        series_id = ep.get("seriesId")
        series = series_dict.get(series_id)  # type: ignore
        
        # Skip if the series itself is not monitored or missing
        if not series or not series.get("monitored"):
            continue

        season_num = ep.get("seasonNumber")
        monitored_seasons = {season["seasonNumber"] for season in series.get("seasons", []) if season.get("monitored")}
        
        if season_num not in monitored_seasons:
            continue

        air_date_utc_str = ep.get("airDateUtc")
        if not air_date_utc_str:
            continue
            
        try:
            air_date_utc = datetime.fromisoformat(air_date_utc_str.replace('Z', '+00:00'))
        except ValueError:
            continue

        is_monitored = ep.get("monitored")
        has_file = ep.get("hasFile")
        
        if has_file:
            continue

        available_time = air_date_utc + delay_delta
        
        if now < available_time:
            if is_monitored:
                logger.info(f"Unmonitoring: {series['title']} - S{season_num:02d}E{ep['episodeNumber']:02d} (Airs: {air_date_utc})")
                episodes_to_unmonitor.append(ep["id"])
        else:
            if not is_monitored:
                logger.info(f"Monitoring: {series['title']} - S{season_num:02d}E{ep['episodeNumber']:02d} (Aired: {air_date_utc})")
                episodes_to_monitor.append(ep["id"])

    if episodes_to_unmonitor:
        try:
            sonarr.update_episodes_monitor_status(episodes_to_unmonitor, False)
            logger.info(f"Successfully unmonitored {len(episodes_to_unmonitor)} episodes.")
        except Exception as e:
            logger.error(f"Failed to unmonitor episodes: {e}")

    if episodes_to_monitor:
        try:
            sonarr.update_episodes_monitor_status(episodes_to_monitor, True)
            logger.info(f"Successfully monitored {len(episodes_to_monitor)} episodes.")
            
            logger.info("Searching for newly monitored episodes (post-air releases only)...")
            sonarr.search_after_air(episodes_to_monitor)
        except Exception as e:
            logger.error(f"Failed to monitor or search episodes: {e}")

def process_sonarr_full(sonarr: SonarrClient) -> None:
    """Full scan: checks every episode of every series (Heavier on the API)."""
    logger.info("Performing FULL Sonarr scan (checking all series)...")
    try:
        all_series = sonarr.get_series()
    except Exception as e:
        logger.error(f"Failed to fetch series from Sonarr: {e}")
        return

    series_dict = {s["id"]: s for s in all_series}
    all_episodes = []

    for series in all_series:
        if not series.get("monitored"):
            continue
        try:
            eps = sonarr.get_episodes(series["id"])
            all_episodes.extend(eps)
        except Exception as e:
            logger.error(f"Failed to fetch episodes for series {series.get('title')}: {e}")
            continue

    process_episodes(sonarr, all_episodes, series_dict)

def process_sonarr_quick(sonarr: SonarrClient) -> None:
    """Quick scan: checks only the calendar for recent and upcoming episodes (Lighter on the API)."""
    logger.info("Performing QUICK Sonarr scan (Calendar)...")
    try:
        all_series = sonarr.get_series()
    except Exception as e:
        logger.error(f"Failed to fetch series from Sonarr: {e}")
        return

    series_dict = {s["id"]: s for s in all_series}

    now = datetime.now(timezone.utc)
    # Check episodes that aired in the last 7 days, up to 14 days in the future
    start_date = now - timedelta(days=7)
    end_date = now + timedelta(days=14)

    try:
        episodes = sonarr.get_calendar(start_date, end_date)
    except Exception as e:
        logger.error(f"Failed to fetch calendar from Sonarr: {e}")
        return

    process_episodes(sonarr, episodes, series_dict)

def process_qbittorrent(qbit: QbitClient, sonarr: SonarrClient) -> None:
    logger.info("Checking qBittorrent for fake releases...")
    
    try:
        categories = sonarr.get_torrent_categories()
        if not categories:
            logger.warning("Could not find any torrent categories in Sonarr. Falling back to 'tv-sonarr'.")
            categories = ["tv-sonarr"]
        else:
            logger.info(f"Retrieved Sonarr categories: {categories}")
    except Exception as e:
        logger.error(f"Failed to fetch categories from Sonarr: {e}")
        categories = ["tv-sonarr"]

    queue_map: Dict[str, Dict[str, Any]] = {}
    try:
        queue = sonarr.get_queue()
        for item in queue:
            download_id = item.get("downloadId")
            if download_id:
                queue_map[download_id.lower()] = {
                    "queue_id": item.get("id"),
                    "episode_id": item.get("episodeId")
                }
    except Exception as e:
        logger.error(f"Failed to fetch Sonarr queue: {e}")

    try:
        torrents = qbit.get_torrents()
    except Exception as e:
        logger.error(f"Failed to fetch torrents from qBittorrent: {e}")
        return

    for torrent in torrents:
        if torrent.category not in categories:
            continue

        try:
            files = qbit.get_torrent_files(torrent.hash)
        except Exception as e:
            logger.error(f"Failed to fetch files for torrent {torrent.name}: {e}")
            continue

        fake_found = False
        for f in files:
            name = f.name.lower()
            if any(name.endswith(ext) for ext in FAKE_EXTENSIONS):
                fake_found = True
                logger.warning(f"Fake file found in torrent '{torrent.name}' -> file: '{f.name}'")
                break
        
        if fake_found:
            t_hash = torrent.hash.lower()
            if t_hash in queue_map:
                logger.info(f"Torrent '{torrent.name}' found in Sonarr queue. Marking as failed and blocklisting...")
                q_item = queue_map[t_hash]
                try:
                    sonarr.mark_download_failed(q_item["queue_id"])
                    
                    if q_item.get("episode_id"):
                        logger.info("Searching for a replacement (post-air releases only)...")
                        sonarr.search_after_air([q_item["episode_id"]])
                except Exception as e:
                    logger.error(f"Failed to mark torrent as failed in Sonarr: {e}")
            else:
                logger.info(f"Torrent '{torrent.name}' not in Sonarr queue. Deleting manually from qBittorrent...")
                try:
                    qbit.delete_torrent(torrent.hash)
                except Exception as e:
                    logger.error(f"Failed to delete torrent {torrent.name}: {e}")

def job(sonarr: SonarrClient, qbit: QbitClient, is_full_scan: bool = False) -> None:
    if is_full_scan:
        process_sonarr_full(sonarr)
    else:
        process_sonarr_quick(sonarr)
        
    # Only process qBittorrent if we can successfully establish a connection this cycle
    if qbit.login():
        process_qbittorrent(qbit, sonarr)
    else:
        logger.warning("Skipping qBittorrent checks this cycle due to connection issues.")

def main():
    if not SONARR_API_KEY:
        logger.error("SONARR_API_KEY is not set. Please check your .env file.")
        return

    logger.info("ReleaseArr script started.")
    logger.info(f"Check interval: {CHECK_INTERVAL_MINUTES} minutes.")
    logger.info(f"Delay minutes after airtime: {DELAY_MINUTES}")
    logger.info(f"Fake extensions to look for: {FAKE_EXTENSIONS}")
    logger.info(f"Full scan every {FULL_SCAN_INTERVAL_MINUTES} min; pre-air grace {PRE_AIR_GRACE_MINUTES} min")
    if DRY_RUN:
        logger.warning("DRY_RUN is on: nothing will be changed, actions are only logged.")
    
    # Initialize clients ONCE
    sonarr = SonarrClient(SONARR_URL, SONARR_API_KEY)
    qbit = QbitClient(QBITTORRENT_URL, QBITTORRENT_USERNAME, QBITTORRENT_PASSWORD)
    
    # Run a full scan on startup to catch everything up
    job(sonarr, qbit, is_full_scan=True)
    
    # Schedule quick scans every X minutes (much lighter on the API)
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(job, sonarr=sonarr, qbit=qbit, is_full_scan=False)
    
    # The quick scan only sees -7/+14 days around now. New or far-future episodes arrive monitored,
    # so run the full scan often enough that none stays grabbable for long.
    schedule.every(FULL_SCAN_INTERVAL_MINUTES).minutes.do(job, sonarr=sonarr, qbit=qbit, is_full_scan=True)
    
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
