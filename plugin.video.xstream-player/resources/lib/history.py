# -*- coding: utf-8 -*-
"""Watch history tracking — stores last N watched items with deduplication."""
import json
import os
import time

import xbmcvfs
import xbmcaddon

MAX_HISTORY = 50


class WatchHistory:
    def __init__(self, addon=None):
        if addon is None:
            addon = xbmcaddon.Addon()
        profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
        if not os.path.exists(profile):
            os.makedirs(profile)
        self._path = os.path.join(profile, 'watch_history.json')
        self._items = self._load()

    def _load(self):
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self):
        with open(self._path, 'w', encoding='utf-8') as f:
            json.dump(self._items, f, ensure_ascii=False)

    def add(self, name, url, icon='', stype='live', extra=None):
        """Add an item to history. Deduplicates by name+stype, most recent first."""
        entry = {
            'name': name,
            'url': url,
            'icon': icon,
            'stype': stype,
            'timestamp': time.time(),
        }
        if extra and isinstance(extra, dict):
            entry.update(extra)
        # Remove existing entry with same name+stype
        self._items = [i for i in self._items
                       if not (i.get('name') == name and i.get('stype') == stype)]
        # Insert at front
        self._items.insert(0, entry)
        # Trim
        self._items = self._items[:MAX_HISTORY]
        self._save()

    def get_all(self, stype=None):
        """Return history items, optionally filtered by type."""
        if stype:
            return [i for i in self._items if i.get('stype') == stype]
        return list(self._items)

    def clear(self):
        self._items = []
        self._save()

    def remove(self, name, stype=None):
        """Remove a specific item from history."""
        if stype:
            self._items = [i for i in self._items
                           if not (i.get('name') == name and i.get('stype') == stype)]
        else:
            self._items = [i for i in self._items if i.get('name') != name]
        self._save()


class ResumePoints:
    """Track playback resume positions for movies/series."""

    def __init__(self, addon=None):
        if addon is None:
            addon = xbmcaddon.Addon()
        profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
        if not os.path.exists(profile):
            os.makedirs(profile)
        self._path = os.path.join(profile, 'resume_points.json')
        self._data = self._load()

    def _load(self):
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        with open(self._path, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, ensure_ascii=False)

    def _key(self, name, url):
        return f'{name}||{url}'

    def save_position(self, name, url, position, duration):
        """Save playback position. Only saves if >60s in and not near the end."""
        if position < 60 or duration < 120:
            return
        if position > duration * 0.93:
            # Near end — mark as finished, remove resume point
            self._data.pop(self._key(name, url), None)
            self._save()
            return
        self._data[self._key(name, url)] = {
            'name': name,
            'url': url,
            'position': position,
            'duration': duration,
            'timestamp': time.time(),
        }
        self._save()

    def get_position(self, name, url):
        """Return saved position in seconds, or 0 if none."""
        entry = self._data.get(self._key(name, url))
        if entry:
            return entry.get('position', 0)
        return 0

    def remove(self, name, url):
        self._data.pop(self._key(name, url), None)
        self._save()

    def clear(self):
        self._data = {}
        self._save()


class WatchedEpisodes:
    """Track which series episodes have been watched."""

    def __init__(self, addon=None):
        if addon is None:
            addon = xbmcaddon.Addon()
        profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
        if not os.path.exists(profile):
            os.makedirs(profile)
        self._path = os.path.join(profile, 'watched_episodes.json')
        self._data = self._load()

    def _load(self):
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        with open(self._path, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, ensure_ascii=False)

    def mark_watched(self, series_id, season_num, episode_id):
        """Mark an episode as watched."""
        key = str(series_id)
        if key not in self._data:
            self._data[key] = {}
        season = str(season_num)
        if season not in self._data[key]:
            self._data[key][season] = []
        ep_id = str(episode_id)
        if ep_id not in self._data[key][season]:
            self._data[key][season].append(ep_id)
            self._save()

    def mark_unwatched(self, series_id, season_num, episode_id):
        """Mark an episode as unwatched."""
        key = str(series_id)
        season = str(season_num)
        if key in self._data and season in self._data[key]:
            ep_id = str(episode_id)
            if ep_id in self._data[key][season]:
                self._data[key][season].remove(ep_id)
                self._save()

    def is_watched(self, series_id, season_num, episode_id):
        """Check if an episode has been watched."""
        key = str(series_id)
        season = str(season_num)
        return str(episode_id) in self._data.get(key, {}).get(season, [])

    def get_watched_count(self, series_id, season_num=None):
        """Get count of watched episodes for a series or season."""
        key = str(series_id)
        if key not in self._data:
            return 0
        if season_num is not None:
            return len(self._data[key].get(str(season_num), []))
        return sum(len(eps) for eps in self._data[key].values())

    def clear_series(self, series_id):
        """Clear all watched data for a series."""
        key = str(series_id)
        if key in self._data:
            del self._data[key]
            self._save()

    def clear(self):
        self._data = {}
        self._save()
