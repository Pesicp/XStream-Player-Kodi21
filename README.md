# XStream Player

A Kodi addon for Xtream Codes and M3U playlist playback with organized categories, EPG support, and PVR integration.

## Features

- **Live TV** with EPG guide and PVR integration
- **Movies** with plot info and poster art
- **Series** with season/episode tracking and watched status
- **Replay / Catchup** for channels with archive support
- **10 Profiles** with independent credentials and settings
- **Search** across Live TV, Movies, and Series
- **Favorites** with folder organization and M3U export
- **Watch History** with resume playback support
- **Parental Control** with PIN lock
- **Credentials PIN lock** to protect profile settings
- **TMDB integration** for movie metadata (optional)
- **Auto-refresh** and data caching for fast navigation

## Screenshots

![Addon Icon](Screenshots/1.png)

![Main Menu](Screenshots/2.png)

![Tools Menu](Screenshots/3.png)

## Installation

### Option 1: Install via File Manager (easiest - no downloads needed)

1. In Kodi, go to **Settings > File Manager**
2. Select **Add source**
3. Enter the URL: `https://raw.githubusercontent.com/Pesicp/XStream-Player/main/repo`
4. Name it `XStream Player` and click OK
5. Go to **Settings > Add-ons > Install from ZIP file**
6. Select **XStream Player** source
7. Select **repository.xstream-player** > `repository.xstream-player-1.0.0.zip`
8. Wait for "Repository installed" notification
9. Go to **Settings > Add-ons > Install from repository > XStream Player Repository > Video add-ons**
10. Select **XStream Player** and install

### Option 2: Install via Repository ZIP (enables auto-updates)

1. Download `repository.xstream-player-1.0.0.zip` from the [repo folder](repo/repository.xstream-player)
2. In Kodi, go to **Settings > Add-ons > Install from ZIP file**
3. Select the downloaded repository ZIP
4. Go to **Settings > Add-ons > Install from repository > XStream Player Repository > Video add-ons**
5. Select **XStream Player** and install

### Option 3: Install from ZIP (manual)

1. Download `plugin.video.xstream-player.zip` from the [Releases](../../releases) page
2. In Kodi, go to **Settings > Add-ons > Install from ZIP file**
3. Select the downloaded ZIP
4. PVR IPTV Simple Client will be installed automatically

## Setup

1. Open **XStream Player** from your Video Add-ons
2. Go to **Tools > Settings**
3. Under **Profiles**, enter your Xtream server URL, username, and password or M3U
4. Go back and select **Refresh List** to load your channels
5. When prompted, restart Kodi for PVR Live TV to work properly
6. After restart, open XStream Player and enjoy Live TV, Movies, and Series

### Live TV Mode

The addon supports two Live TV modes (configurable in **Settings > PVR**):

- **PVR (recommended)** - Uses Kodi's native PVR/Live TV with channel guide. Requires PVR IPTV Simple Client and a Kodi restart after first setup.
- **Plugin list** - Browse and play Live TV directly within the addon. No PVR required, no restart needed. Good for devices where PVR causes issues.

## Settings Overview

### Profiles
- **Active Profile** - Switch between up to 10 profiles, each with its own credentials
- **Lock credentials with PIN** - Protect settings access with a PIN code

### Playback
- **Stream timeout** - How long to wait before giving up on a stream (default 15s)
- **Custom User-Agent** - Override the browser user-agent for streams that require it
- **Use InputStream Adaptive** - Enable for HLS/adaptive streams (try if streams don't play)
- **Pre-fetch all data on startup** - Cache everything when you first open the addon

### EPG
- **Auto-detect EPG from Xtream** - Automatically fetch the TV guide from your provider
- **Show EPG info in Live TV** - Display current/next program info on channel listings
- **EPG language priority** - Preferred language for guide data (e.g. `en`, `fr`)
- **EPG refresh interval** - How often to update the guide (default every 4 hours)
- **EPG timezone offset** - Adjust if program times are wrong
- **Replay days back** - How many days of catchup/replay to show (default 7)

### Metadata
- **Enable TMDB metadata** - Fetch movie plots and posters from TMDB (requires free API key from themoviedb.org)
- **Use TMDB posters/ratings** - Choose what to pull from TMDB

### Parental Control
- **Enable parental control** - Require PIN to access Movies and Series
- **Hide adult categories** - Filter out adult content from listings (toggle in Tools menu)

### PVR
- **Live TV mode** - Choose between PVR (native Kodi Live TV) or Plugin list (in-addon browsing)
- **Auto-sync Live TV to PVR** - Automatically update PVR channels on refresh

### Maintenance
- **Backup/Restore** - Save and restore your profiles and settings
- **Clear Cache** - Clear EPG, channel, or TMDB cache separately or all at once

## Requirements

- Kodi 21 (Omega) or later
- PVR IPTV Simple Client (installed automatically)

## License

MIT
