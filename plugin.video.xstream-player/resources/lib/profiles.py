# -*- coding: utf-8 -*-
import json
import os
import time

import xbmcvfs


class ProfileManager:
    def __init__(self, addon):
        self.addon = addon
        raw = addon.getSetting('active_profile') or 'Profile 1'
        self.active = raw.replace('Profile ', '').strip() or '1'
        self.profile_path = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
        if not os.path.exists(self.profile_path):
            os.makedirs(self.profile_path)
        self._cat_file = os.path.join(self.profile_path, 'category_prefs.json')

    def _load_cat_prefs(self):
        try:
            with open(self._cat_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cat_prefs(self, prefs):
        with open(self._cat_file, 'w', encoding='utf-8') as f:
            json.dump(prefs, f)

    def get_profile_setting(self, key):
        return self.addon.getSetting(f'profile_{self.active}_{key}')

    def get_credentials(self):
        source_type = self.get_profile_setting('source_type')
        creds = {
            'name': self.get_profile_setting('name'),
            'source_type': source_type,
            'm3u_url': '',
            'xtream_url': '',
            'xtream_username': '',
            'xtream_password': '',
            'epg_url': self.get_profile_setting('epg_url'),
        }
        if source_type == 'M3U':
            creds['m3u_url'] = self.get_profile_setting('m3u')
        else:
            creds['xtream_url'] = self.get_profile_setting('xtream_url')
            creds['xtream_username'] = self.get_profile_setting('xtream_username')
            creds['xtream_password'] = self.get_profile_setting('xtream_password')
        return creds

    def get_visible_categories(self):
        prefs = self._load_cat_prefs()
        raw = prefs.get(self.active)
        if raw is None:
            return ['live_pvr', 'pvr_favs', 'live_classic', 'guide', 'movies', 'series', 'replay', 'search', 'favorites', 'tools']
        return [x.strip().lower() for x in raw.split('|') if x.strip()]

    def set_visible_categories(self, categories):
        prefs = self._load_cat_prefs()
        prefs[self.active] = '|'.join(categories)
        self._save_cat_prefs(prefs)


class RefreshTracker:
    def __init__(self, addon):
        self.addon = addon
        raw = addon.getSetting('active_profile') or 'Profile 1'
        self.profile = raw.replace('Profile ', '').strip() or '1'
        self.profile_path = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
        self.file = os.path.join(self.profile_path, f'refresh_{self.profile}.txt')
        self._ensure_profile()

    def _ensure_profile(self):
        if not os.path.exists(self.profile_path):
            os.makedirs(self.profile_path)

    def get_last_refresh(self):
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                return float(f.read().strip())
        except Exception:
            return 0

    def set_last_refresh(self, t=None):
        t = t or time.time()
        with open(self.file, 'w', encoding='utf-8') as f:
            f.write(str(t))

    def should_refresh(self):
        if self.addon.getSetting('auto_refresh_enabled').lower() != 'true':
            return False
        interval = self.addon.getSetting('auto_refresh_interval') or '24'
        if interval.lower() == 'never':
            return False
        try:
            hours = float(interval)
        except ValueError:
            hours = 24
        last = self.get_last_refresh()
        return (time.time() - last) > (hours * 3600)
