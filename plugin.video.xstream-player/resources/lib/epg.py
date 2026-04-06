# -*- coding: utf-8 -*-
import calendar
import json
import os
import time
import urllib.parse
from xml.etree import ElementTree as ET

import requests
import xbmc
import xbmcaddon
import xbmcvfs

from profiles import ProfileManager

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

_startup_forced = False


def _get_timeout():
    try:
        return int(xbmcaddon.Addon().getSetting('stream_timeout') or '15')
    except ValueError:
        return 15


def _get_headers():
    custom_ua = xbmcaddon.Addon().getSetting('custom_user_agent')
    if custom_ua:
        return {'User-Agent': custom_ua}
    return HEADERS


def _epg_log(msg):
    xbmc.log(f'[XStream Player EPG] {msg}', xbmc.LOGINFO)


class EPG:
    def __init__(self, addon):
        self.addon = addon
        pm = ProfileManager(addon)
        creds = pm.get_credentials()
        # Use per-profile EPG URL
        self.epg_url = creds.get('epg_url', '')
        # Auto-detect XMLTV URL from Xtream if no EPG URL is set and auto-detect is enabled
        if not self.epg_url and addon.getSetting('auto_epg').lower() != 'false':
            xt_url = creds.get('xtream_url', '')
            xt_user = creds.get('xtream_username', '')
            xt_pwd = creds.get('xtream_password', '')
            if xt_url and xt_user and xt_pwd:
                self.epg_url = f"{xt_url.rstrip('/')}/xmltv.php?username={urllib.parse.quote(xt_user)}&password={urllib.parse.quote(xt_pwd)}"
                _epg_log('Auto-detected EPG URL from Xtream credentials')
        try:
            self.cache_hours = int(addon.getSetting('epg_refresh') or '4')
        except ValueError:
            self.cache_hours = 4
        try:
            self.offset_hours = float(addon.getSetting('epg_offset') or '0')
        except ValueError:
            self.offset_hours = 0.0
        self.profile_path = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
        active_profile = pm.active
        self.cache_file = os.path.join(self.profile_path, f'epg_cache_profile_{active_profile}.json')
        self.programs = {}

    def _ensure_profile(self):
        if not os.path.exists(self.profile_path):
            os.makedirs(self.profile_path)

    def _cache_valid(self):
        if not os.path.exists(self.cache_file):
            return False
        age = time.time() - os.path.getmtime(self.cache_file)
        return age < (self.cache_hours * 3600)

    def _save_cache(self):
        self._ensure_profile()
        with open(self.cache_file, 'w', encoding='utf-8') as f:
            json.dump(self.programs, f)

    def _load_cache(self):
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                self.programs = json.load(f)
            _epg_log(f'Loaded cache with {len(self.programs)} channels')
        except Exception as e:
            _epg_log(f'Cache load failed: {e}')
            self.programs = {}

    def fetch(self):
        if not self.epg_url:
            _epg_log('No EPG URL configured')
            self.programs = {}
            return
        safe_url = self.epg_url
        if safe_url:
            safe_url = urllib.parse.urlparse(safe_url)._replace(query='').geturl()
        _epg_log(f'Fetching EPG from {safe_url}')
        try:
            resp = requests.get(self.epg_url, headers=_get_headers(), timeout=_get_timeout())
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            _epg_log(f'EPG fetch failed: {e}')
            self.programs = {}
            return

        lang_pref = (self.addon.getSetting('epg_language') or '').lower()

        self.programs = {}
        for prog in root.findall('programme'):
            channel = prog.get('channel', '')
            if not channel:
                continue

            titles = prog.findall('title')
            title_el = titles[0] if titles else None
            if lang_pref and titles:
                for t in titles:
                    if (t.get('lang') or '').lower() == lang_pref:
                        title_el = t
                        break

            descs = prog.findall('desc')
            desc_el = descs[0] if descs else None
            if lang_pref and descs:
                for d in descs:
                    if (d.get('lang') or '').lower() == lang_pref:
                        desc_el = d
                        break

            icon_el = prog.find('icon')

            start = prog.get('start', '')
            stop = prog.get('stop', '')

            entry = {
                'title': title_el.text if title_el is not None else 'Unknown',
                'desc': desc_el.text if desc_el is not None else '',
                'icon': icon_el.get('src') if icon_el is not None else '',
                'start': start,
                'stop': stop,
                'start_timestamp': _xmltv_time_to_simple(start),
                'start_str': _xmltv_time_to_display(start),
                'stop_timestamp': _xmltv_time_to_simple(stop),
                'duration_sec': _xmltv_duration_sec(start, stop)
            }
            self.programs.setdefault(channel, []).append(entry)

        try:
            past_days = int(self.addon.getSetting('epg_past_days') or '3')
        except ValueError:
            past_days = 3
        cutoff = time.time() - (past_days * 86400)

        for ch in self.programs:
            self.programs[ch] = [p for p in self.programs[ch] if _parse_xmltv_time(p['stop']) > cutoff]
            self.programs[ch].sort(key=lambda x: x['start'])
        _epg_log(f'Parsed {len(self.programs)} channels from XMLTV')
        self._save_cache()

    def load(self):
        global _startup_forced
        force_startup = self.addon.getSetting('epg_force_refresh_startup').lower() == 'true'
        if force_startup and not _startup_forced:
            _epg_log('Startup force refresh triggered')
            _startup_forced = True
            self.fetch()
            return
        if self._cache_valid():
            _epg_log('EPG cache is valid, loading from disk')
            self._load_cache()
        else:
            _epg_log('EPG cache expired or missing, fetching fresh')
            self.fetch()

    def _apply_offset(self, ts):
        if not self.offset_hours:
            return ts
        return ts + (self.offset_hours * 3600)

    def _find_channel_id(self, channel_id, channel_name=''):
        if not channel_id and not channel_name:
            return None
        # Exact ID match
        if channel_id and channel_id in self.programs:
            return channel_id
        # Name match (case-insensitive, exact)
        if channel_name:
            name_lower = channel_name.lower()
            for cid in self.programs:
                if cid.lower() == name_lower:
                    return cid
        # Numeric ID vs string match
        if channel_id:
            for cid in self.programs:
                if str(cid) == str(channel_id):
                    return cid
        # Partial name match
        if channel_name:
            for cid in self.programs:
                if name_lower in cid.lower() or cid.lower() in name_lower:
                    return cid
        return None

    def get_current_program(self, channel_id, channel_name=''):
        matched_id = self._find_channel_id(channel_id, channel_name)
        if not matched_id:
            return None
        now = time.time()
        for prog in self.programs[matched_id]:
            start = self._apply_offset(_parse_xmltv_time(prog['start']))
            stop = self._apply_offset(_parse_xmltv_time(prog['stop']))
            if start and stop and start <= now < stop:
                return prog
        return None

    def get_next_program(self, channel_id, channel_name=''):
        matched_id = self._find_channel_id(channel_id, channel_name)
        if not matched_id:
            return None
        now = time.time()
        found_current = False
        for prog in self.programs[matched_id]:
            start = self._apply_offset(_parse_xmltv_time(prog['start']))
            stop = self._apply_offset(_parse_xmltv_time(prog['stop']))
            if start and stop and start <= now < stop:
                found_current = True
                continue
            if found_current and start and start > now:
                return prog
        # If no current program found, return first future program
        for prog in self.programs[matched_id]:
            start = self._apply_offset(_parse_xmltv_time(prog['start']))
            if start and start > now:
                return prog
        return None

    def get_programs_for_channel(self, channel_id, channel_name='', days_back=None):
        matched_id = self._find_channel_id(channel_id, channel_name)
        if not matched_id:
            return []
        if days_back is None:
            try:
                days_back = int(self.addon.getSetting('replay_days') or '7')
            except ValueError:
                days_back = 7
        cutoff = time.time() - (days_back * 86400)
        result = []
        for prog in self.programs[matched_id]:
            stop = self._apply_offset(_parse_xmltv_time(prog['stop']))
            if stop and stop > cutoff:
                result.append(prog)
        return result

    def export_xmltv(self, dest_path):
        if not self.programs:
            return False
        try:
            now = time.time()
            # Only export a reasonable window for PVR to keep startup fast
            start_cutoff = now - (1 * 86400)   # 1 day past
            stop_cutoff = now + (2 * 86400)    # 2 days future
            root = ET.Element('tv')
            exported_channels = set()
            for channel_id, progs in self.programs.items():
                channel_has_progs = False
                for prog in progs:
                    prog_start = _parse_xmltv_time(prog.get('start', ''))
                    prog_stop = _parse_xmltv_time(prog.get('stop', ''))
                    if prog_stop < start_cutoff or prog_start > stop_cutoff:
                        continue
                    if channel_id not in exported_channels:
                        ch_el = ET.SubElement(root, 'channel', {'id': str(channel_id)})
                        disp = ET.SubElement(ch_el, 'display-name')
                        disp.text = str(channel_id)
                        exported_channels.add(channel_id)
                    channel_has_progs = True
                    prog_el = ET.SubElement(root, 'programme', {
                        'start': prog.get('start', ''),
                        'stop': prog.get('stop', ''),
                        'channel': str(channel_id)
                    })
                    title_el = ET.SubElement(prog_el, 'title')
                    title_el.text = prog.get('title', 'Unknown')
                    if prog.get('desc'):
                        desc_el = ET.SubElement(prog_el, 'desc')
                        desc_el.text = prog['desc']
                    if prog.get('icon'):
                        icon_el = ET.SubElement(prog_el, 'icon', {'src': prog['icon']})
                if channel_has_progs:
                    _epg_log(f'Exported channel {channel_id} with programs in window')
            tree = ET.ElementTree(root)
            self._ensure_profile()
            tree.write(dest_path, encoding='utf-8', xml_declaration=True)
            _epg_log(f'Exported XMLTV to {dest_path}')
            return True
        except Exception as e:
            _epg_log(f'Export XMLTV failed: {e}')
            return False


def _parse_xmltv_time(ts):
    if not ts:
        return 0
    ts = ts.strip()
    offset_sec = 0
    if ' ' in ts:
        parts = ts.split(' ', 1)
        ts = parts[0]
        tz = parts[1].strip()
        if tz and (tz[0] == '+' or tz[0] == '-'):
            try:
                sign = 1 if tz[0] == '+' else -1
                tz = tz[1:]
                offset_sec = sign * (int(tz[:2]) * 3600 + int(tz[2:4]) * 60)
            except (ValueError, IndexError):
                offset_sec = 0
    try:
        t = time.strptime(ts, '%Y%m%d%H%M%S')
        return calendar.timegm(t) - offset_sec
    except ValueError:
        return 0


def _xmltv_time_to_simple(ts):
    t = _parse_xmltv_time(ts)
    if not t:
        return ts
    return time.strftime('%Y-%m-%d:%H-%M', time.localtime(t))


def _xmltv_time_to_display(ts):
    t = _parse_xmltv_time(ts)
    if not t:
        return ''
    return time.strftime('%d/%m %H:%M', time.localtime(t))


def _xmltv_duration_sec(start, stop):
    s = _parse_xmltv_time(start)
    e = _parse_xmltv_time(stop)
    if s and e:
        return int(e - s)
    return 3600
