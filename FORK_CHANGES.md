# Changes in this branch

Only grab releases published after the episode aired, plus a few robustness fixes.

## Why

Unmonitoring unaired episodes keeps Sonarr from grabbing fakes while the episode is unaired, but the
search that ReleaseArr triggers right after re-monitoring is a plain `EpisodeSearch`. Fakes that were
seeded *before* the air date (wrong video, trolls) are still listed by the indexers, so that search can
grab one. The extension check cannot catch them because the file type is fine.

## What changed

- `search_after_air()` replaces `EpisodeSearch`: it asks Sonarr for the interactive-search results
  (`GET /api/v3/release?episodeId=`), drops anything Sonarr rejected or that was `publishDate`d before
  the episode's `airDateUtc` (minus `PRE_AIR_GRACE_MINUTES`), and grabs the best remaining one by
  Sonarr's own `releaseWeight` (`POST /api/v3/release`). If nothing qualifies it does nothing and
  Sonarr's RSS sync picks up the real release when it appears.
- Blocklisting a fake now passes `skipRedownload=true`; otherwise Sonarr's own auto-redownload would
  pick a replacement without the pre-air filter.
- `get_queue()` paginates. Sonarr returns 10 records by default, so with a long queue most fake
  torrents were wrongly treated as "not in the Sonarr queue" and deleted without a blocklist entry.
- The full scan runs every `FULL_SCAN_INTERVAL_MINUTES` (default 60) instead of once a day, so
  episodes further out than the quick scan's 14-day window do not stay monitored for up to 24 hours.
- `DRY_RUN=1` logs every write (monitor toggles, queue removal, grabs, torrent deletion) instead of
  performing it.
- qBittorrent login: if the login call is refused but the API answers anyway (auth bypass for
  whitelisted subnets), carry on instead of skipping the cycle.
- Dependencies pinned.

## New environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `0` | Log writes instead of performing them |
| `PRE_AIR_GRACE_MINUTES` | `0` | Reject releases published earlier than air time minus this |
| `FULL_SCAN_INTERVAL_MINUTES` | `60` | How often the full (all series) scan runs |
