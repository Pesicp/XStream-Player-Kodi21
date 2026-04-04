# -*- coding: utf-8 -*-
import json
import os

import xbmcvfs


class Favorites:
    def __init__(self, addon):
        self.profile_path = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
        self.file = os.path.join(self.profile_path, 'favorites.json')
        self._ensure_profile()
        self.items = self._load()

    def _ensure_profile(self):
        if not os.path.exists(self.profile_path):
            os.makedirs(self.profile_path)

    def _load(self):
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Migration: old format was flat list, new format has folders
                if isinstance(data, list):
                    return {'Favorites': data}
                return data
        except Exception:
            return {'Favorites': []}

    def _save(self):
        with open(self.file, 'w', encoding='utf-8') as f:
            json.dump(self.items, f, ensure_ascii=False)

    def get_folders(self):
        """Return list of folder names."""
        return list(self.items.keys())

    def is_favorite(self, item_id, folder=None):
        if folder:
            return any(i.get('id') == item_id for i in self.items.get(folder, []))
        return any(i.get('id') == item_id
                   for items in self.items.values() for i in items)

    def add(self, item, folder='Favorites'):
        if folder not in self.items:
            self.items[folder] = []
        if not self.is_favorite(item.get('id'), folder):
            self.items[folder].append(item)
            self._save()
            return True
        return False

    def remove(self, item_id, folder=None):
        removed = False
        if folder:
            before = len(self.items.get(folder, []))
            self.items[folder] = [i for i in self.items.get(folder, []) if i.get('id') != item_id]
            removed = len(self.items[folder]) < before
        else:
            for f in self.items:
                before = len(self.items[f])
                self.items[f] = [i for i in self.items[f] if i.get('id') != item_id]
                if len(self.items[f]) < before:
                    removed = True
        if removed:
            self._save()
        return removed

    def get_all(self, folder=None):
        if folder:
            return self.items.get(folder, [])
        # Flat list of all items across all folders
        result = []
        for items in self.items.values():
            result.extend(items)
        return result

    def toggle(self, item, folder='Favorites'):
        if self.is_favorite(item.get('id')):
            self.remove(item.get('id'))
            return False
        else:
            self.add(item, folder)
            return True

    def create_folder(self, name):
        if name not in self.items:
            self.items[name] = []
            self._save()

    def delete_folder(self, name):
        if name in self.items and name != 'Favorites':
            del self.items[name]
            self._save()

    def export_m3u(self, path, folder=None):
        """Export favorites as M3U file."""
        items = self.get_all(folder)
        lines = ['#EXTM3U']
        for item in items:
            name = item.get('name', 'Unknown')
            url = item.get('url', '')
            icon = item.get('icon', '')
            if not url:
                continue
            attrs = f'tvg-name="{name}"'
            if icon:
                attrs += f' tvg-logo="{icon}"'
            lines.append(f'#EXTINF:-1 {attrs},{name}')
            lines.append(url)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        return len(items)
