# -*- coding: utf-8 -*-
import json
import os
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from iptv import IPTV, build_m3u_content
from epg import EPG, _parse_xmltv_time
from favorites import Favorites
from history import WatchHistory, ResumePoints, WatchedEpisodes
from profiles import ProfileManager, RefreshTracker
from tmdb import TMDB

addon = xbmcaddon.Addon()
addon_handle = int(sys.argv[1])
base_url = sys.argv[0]
args = urllib.parse.parse_qs(sys.argv[2][1:])
fav = Favorites(addon)
watch_history = WatchHistory(addon)
resume_db = ResumePoints(addon)
watched_db = WatchedEpisodes(addon)
pm = ProfileManager(addon)

# xbmcplugin.setContent removed globally; content type set per-folder where appropriate

import xbmc

def _bootstrap_settings():
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    dest = os.path.join(profile, 'settings.xml')
    if os.path.exists(dest):
        return
    src = os.path.join(xbmcvfs.translatePath(addon.getAddonInfo('path')), 'resources', 'userdata', 'settings.xml')
    if os.path.exists(src):
        if not os.path.exists(profile):
            os.makedirs(profile)
        import shutil
        shutil.copy2(src, dest)
        xbmc.log('[XStream Player] Bootstrapped settings from addon package', xbmc.LOGINFO)

_bootstrap_settings()


def _bootstrap_pvr():
    """Create empty PVR stub files so PVR IPTV Simple Client doesn't get stuck on startup."""
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    if not os.path.exists(profile):
        os.makedirs(profile)
    m3u_path = os.path.join(profile, 'pvr_live.m3u8')
    epg_path = os.path.join(profile, 'pvr_epg.xml')
    if not os.path.exists(m3u_path):
        with open(m3u_path, 'w', encoding='utf-8') as f:
            f.write('#EXTM3U\n')
    if not os.path.exists(epg_path):
        with open(epg_path, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="utf-8"?><tv></tv>\n')
    # Configure PVR Simple Client to point at our stub files
    try:
        pvr_profile = xbmcvfs.translatePath('special://profile/addon_data/pvr.iptvsimple')
        settings_path = os.path.join(pvr_profile, 'instance-settings-1.xml')
        if not os.path.exists(pvr_profile):
            os.makedirs(pvr_profile)
        needs_update = False
        if os.path.exists(settings_path):
            tree = ET.parse(settings_path)
            root = tree.getroot()
            updates = {'m3uPathType': '0', 'm3uPath': m3u_path, 'epgPathType': '0', 'epgPath': epg_path}
            for key, val in updates.items():
                el = root.find(f".//setting[@id='{key}']")
                if el is not None:
                    current = el.text or ''
                    if not current or current == 'true':
                        el.text = val
                        if 'default' in el.attrib:
                            del el.attrib['default']
                        needs_update = True
                else:
                    new_el = ET.SubElement(root, 'setting', {'id': key})
                    new_el.text = val
                    needs_update = True
            if needs_update:
                tree.write(settings_path, encoding='utf-8', xml_declaration=True)
        else:
            root = ET.Element('settings', {'version': '2'})
            for key, val in [('m3uPathType', '0'), ('m3uPath', m3u_path), ('epgPathType', '0'), ('epgPath', epg_path)]:
                el = ET.SubElement(root, 'setting', {'id': key})
                el.text = val
            tree = ET.ElementTree(root)
            tree.write(settings_path, encoding='utf-8', xml_declaration=True)
    except Exception:
        pass

_bootstrap_pvr()


def _apply_buffer_fix():
    """Set Kodi cache/buffer settings via JSON-RPC and configure PVR for stable IPTV playback."""
    if addon.getSetting('buffer_fix_enabled') != 'true':
        return
    size_mb = int(addon.getSetting('buffer_size_mb') or '100')
    read_factor = int(addon.getSetting('buffer_read_factor') or '20')
    settings = {
        'filecache.buffermode': 1,
        'filecache.memorysize': size_mb,
        'filecache.readfactor': read_factor * 100,
    }
    for key, val in settings.items():
        xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0', 'method': 'Settings.SetSettingValue',
            'params': {'setting': key, 'value': val}, 'id': 1
        }))
    # Configure PVR IPTV Simple Client to use inputstream.ffmpegdirect for stable live streams
    try:
        pvr_profile = xbmcvfs.translatePath('special://profile/addon_data/pvr.iptvsimple')
        settings_path = os.path.join(pvr_profile, 'instance-settings-1.xml')
        if os.path.exists(settings_path):
            tree = ET.parse(settings_path)
            root = tree.getroot()
            pvr_updates = {'defaultInputstream': 'inputstream.ffmpegdirect', 'defaultMimeType': 'video/mp2t'}
            changed = False
            for key, val in pvr_updates.items():
                el = root.find(f".//setting[@id='{key}']")
                if el is not None:
                    if (el.text or '') != val:
                        el.text = val
                        if 'default' in el.attrib:
                            del el.attrib['default']
                        changed = True
                else:
                    new_el = ET.SubElement(root, 'setting', {'id': key})
                    new_el.text = val
                    changed = True
            if changed:
                tree.write(settings_path, encoding='utf-8', xml_declaration=True)
    except Exception:
        pass
    xbmc.log(f'[XStream Player] Buffer fix applied: {size_mb}MB, read factor {read_factor}x', xbmc.LOGINFO)

_apply_buffer_fix()


def _log(msg):
    xbmc.log(f'[XStream Player] {msg}', xbmc.LOGINFO)


def build_url(query):
    return base_url + '?' + urllib.parse.urlencode(query, doseq=True)


def _apply_sort():
    if addon.getSetting('default_sort_order') == 'A-Z':
        xbmcplugin.addSortMethod(addon_handle, xbmcplugin.SORT_METHOD_LABEL)


def _get_credentials():
    return pm.get_credentials()


def _prefetch_vod_info_batch(streams, base_url, user, pwd):
    """Fetch vod_info for a batch of movies in parallel threads."""
    def _fetch_one(s):
        sid = str(s.get('stream_id', ''))
        if not sid:
            return
        cname = f'vod_info_{sid}'
        if _cache_load(cname) is not None:
            return
        try:
            info = IPTV.get_vod_info(base_url, user, pwd, sid)
            _cache_save(cname, info or {})
        except Exception:
            pass

    threads = []
    for s in streams:
        t = threading.Thread(target=_fetch_one, args=(s,))
        t.daemon = True
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=15)


def _enrich_movie_info(s, base_url=None, user=None, pwd=None):
    result = {
        'plot': s.get('plot') or s.get('description') or s.get('info', {}).get('plot') or '',
        'poster_url': s.get('stream_icon') or '',
        'rating': str(s.get('rating', '') or s.get('rating_5based', '') or ''),
        'year': ''
    }
    if s.get('stream_id'):
        sid = str(s.get('stream_id'))
        cname = f'vod_info_{sid}'
        info = _cache_load(cname)
        if info is not None:
            result['plot'] = info.get('info', {}).get('plot') or info.get('plot') or result['plot']
            result['poster_url'] = info.get('info', {}).get('movie_image') or info.get('stream_icon') or result['poster_url']
            result['year'] = str(info.get('info', {}).get('releasedate', '') or info.get('info', {}).get('release_date', '') or '')[:4]

    tmdb_enabled = addon.getSetting('tmdb_enabled').lower() == 'true'
    tmdb_key = addon.getSetting('tmdb_api_key') or ''
    if tmdb_enabled and tmdb_key:
        needs_tmdb = not result['plot'] or (addon.getSetting('tmdb_posters').lower() == 'true' and not result['poster_url'])
        if needs_tmdb:
            clean_title = s.get('name', '')
            cname = f"tmdb_search_{clean_title.lower().replace(' ', '_')}"
            tmdb_data = _cache_load(cname)
            if tmdb_data is None or not _cache_valid(cname, hours=720):
                tmdb = TMDB(tmdb_key)
                tmdb_data = tmdb.enrich(clean_title)
                _cache_save(cname, tmdb_data)
            if addon.getSetting('tmdb_posters').lower() == 'true' and tmdb_data.get('poster_url') and not result['poster_url']:
                result['poster_url'] = tmdb_data['poster_url']
            if not result['plot'] and tmdb_data.get('plot'):
                result['plot'] = tmdb_data['plot']
            if addon.getSetting('tmdb_ratings').lower() == 'true':
                result['rating'] = tmdb_data.get('rating', '')
                result['year'] = tmdb_data.get('year', '')
    return result


def _cache_path(name):
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    return os.path.join(profile, f'data_cache_{name}.json')


def _cache_load(name):
    try:
        with open(_cache_path(name), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _cache_save(name, data):
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    if not os.path.exists(profile):
        os.makedirs(profile)
    with open(_cache_path(name), 'w', encoding='utf-8') as f:
        json.dump(data, f)


def _cache_valid(name, hours=None):
    if hours is None:
        try:
            hours = float(addon.getSetting('auto_refresh_interval') or '24')
        except ValueError:
            hours = 24
    path = _cache_path(name)
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < (hours * 3600)


def _cache_clear_all():
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    count = 0
    try:
        for fname in os.listdir(profile):
            if fname.startswith('data_cache_') or fname.startswith('epg_cache_profile_') or fname.startswith('vod_info_') or fname.startswith('tmdb_'):
                os.remove(os.path.join(profile, fname))
                count += 1
    except Exception as e:
        _log(f'Cache clear error: {e}')
    return count


def _get_cached_m3u_channels(m3u_url):
    if not m3u_url or addon.getSetting('prefetch_on_startup').lower() != 'true':
        return IPTV.get_m3u_channels(m3u_url)
    if not _cache_valid('m3u'):
        data = IPTV.get_m3u_channels(m3u_url)
        _cache_save('m3u', data)
        return data
    return _cache_load('m3u') or []


def _get_cached_xtream_categories(url, user, pwd, stype):
    if not url or not user or not pwd or addon.getSetting('prefetch_on_startup').lower() != 'true':
        return IPTV.get_xtream_categories(url, user, pwd, stype)
    key = f'xtream_cats_{stype}'
    if not _cache_valid(key):
        data = IPTV.get_xtream_categories(url, user, pwd, stype)
        _cache_save(key, data)
        return data
    return _cache_load(key) or []


def _get_cached_xtream_streams(url, user, pwd, stype, category_id=None):
    if not url or not user or not pwd or addon.getSetting('prefetch_on_startup').lower() != 'true':
        return IPTV.get_xtream_streams(url, user, pwd, stype, category_id)
    key = f'xtream_streams_{stype}'
    if not _cache_valid(key):
        data = IPTV.get_xtream_streams(url, user, pwd, stype, None)
        _cache_save(key, data)
    else:
        data = _cache_load(key) or []
    if category_id:
        return [s for s in data if str(s.get('category_id', '')) == str(category_id)]
    return data


def _pvr_m3u_path():
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    return os.path.join(profile, 'pvr_live.m3u8')


def _pvr_epg_path():
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    return os.path.join(profile, 'pvr_epg.xml')


def _export_pvr_m3u():
    creds = _get_credentials()
    m3u_url = creds.get('m3u_url', '')
    xt_url = creds.get('xtream_url', '')
    xt_user = creds.get('xtream_username', '')
    xt_pwd = creds.get('xtream_password', '')

    channels = []
    if m3u_url:
        for ch in _get_cached_m3u_channels(m3u_url):
            channels.append({
                'name': ch.get('name', 'Unknown'),
                'url': ch.get('url', ''),
                'tvg_id': ch.get('tvg_id', ''),
                'logo': ch.get('logo', ''),
                'group': ch.get('group', 'General'),
                'catchup': ch.get('catchup', ''),
                'catchup_source': ch.get('catchup_source', ''),
                'catchup_days': ch.get('catchup_days', '')
            })
    if xt_url and xt_user and xt_pwd:
        # Build category ID → name lookup
        cats = _cache_load('xtream_cats_live') or IPTV.get_xtream_categories(xt_url, xt_user, xt_pwd, 'live')
        cat_map = {}
        for c in (cats or []):
            cat_map[str(c.get('category_id', ''))] = c.get('category_name', 'Live TV')

        streams = _get_cached_xtream_streams(xt_url, xt_user, xt_pwd, 'live')
        for s in streams:
            sid = str(s.get('stream_id', ''))
            epg_id = s.get('epg_channel_id') or sid
            url = IPTV.build_xtream_stream_url(xt_url, xt_user, xt_pwd, s, 'live')
            group = cat_map.get(str(s.get('category_id', '')), 'Live TV')
            channels.append({
                'name': s.get('name', 'Unknown'),
                'url': url,
                'tvg_id': epg_id,
                'logo': s.get('stream_icon', ''),
                'group': group,
                'catchup': 'default' if s.get('tv_archive') or s.get('catchup') else '',
                'catchup_source': '',
                'catchup_days': str(s.get('tv_archive_duration', '')) if s.get('tv_archive_duration') else ''
            })

    if not channels:
        return False
    m3u_path = _pvr_m3u_path()
    m3u_data = build_m3u_content(channels)
    with open(m3u_path, 'w', encoding='utf-8') as f:
        f.write(m3u_data)
    # Quick validation: every EXTINF must contain a comma
    bad_lines = [ln for ln in m3u_data.splitlines() if ln.startswith('#EXTINF:') and ',' not in ln]
    if bad_lines:
        _log(f'M3U validation failed on {len(bad_lines)} lines')
        return False
    _log(f'Exported PVR M3U with {len(channels)} channels')
    return True


def _export_pvr_epg():
    epg_path = _pvr_epg_path()
    try:
        epg = EPG(addon)
        epg.load()
        if epg.programs and epg.export_xmltv(epg_path):
            _log(f'Exported real EPG for PVR ({len(epg.programs)} channels)')
            return True
        # Fallback stub if no data available
        stub = '<?xml version="1.0" encoding="utf-8"?><tv></tv>'
        with open(epg_path, 'w', encoding='utf-8') as f:
            f.write(stub)
        _log('Exported stub EPG for PVR (no EPG data available)')
        return True
    except Exception as e:
        _log(f'EPG export failed: {e}')
        return False


def _configure_pvr_iptvsimple():
    try:
        pvr_profile = xbmcvfs.translatePath('special://profile/addon_data/pvr.iptvsimple')
        if not os.path.exists(pvr_profile):
            os.makedirs(pvr_profile)
        settings_path = os.path.join(pvr_profile, 'instance-settings-1.xml')
        m3u_path = _pvr_m3u_path()
        epg_path = _pvr_epg_path()
        root = ET.Element('settings', {'version': '2'})
        defs = [
            ('m3uPathType', '0'),
            ('m3uPath', m3u_path),
            ('m3uUrl', ''),
            ('epgPathType', '0'),
            ('epgPath', epg_path),
            ('epgUrl', ''),
            ('m3uRefreshMode', '1'),
            ('logoPathType', '0'),
            ('logoPath', ''),
            ('logoBaseUrl', ''),
        ]
        for key, val in defs:
            el = ET.SubElement(root, 'setting', {'id': key})
            el.text = val
        tree = ET.ElementTree(root)
        tree.write(settings_path, encoding='utf-8', xml_declaration=True)
        _log('Configured PVR IPTV Simple Client')

        xbmcgui.Dialog().notification(
            'XStream Player',
            'PVR config updated. Restart Kodi to apply.',
            xbmcgui.NOTIFICATION_INFO,
            5000
        )
        return True
    except Exception as e:
        _log(f'Configure PVR failed: {e}')
        return False




def is_pvr_iptvsimple_installed():
    try:
        xbmcaddon.Addon('pvr.iptvsimple')
        return True
    except Exception:
        return False


def prompt_install_pvr():
    dlg = xbmcgui.Dialog()
    choice = dlg.yesno(
        'PVR IPTV Simple Client required',
        'To use the native Live TV preview, you must install PVR IPTV Simple Client from the official Kodi repository.',
        yeslabel='Install now',
        nolabel='Cancel'
    )
    if choice:
        xbmc.executebuiltin('InstallAddon(pvr.iptvsimple)')
        # Wait for installation and notify
        for _ in range(30):
            xbmc.sleep(1000)
            if is_pvr_iptvsimple_installed():
                xbmcgui.Dialog().notification('XStream Player', 'PVR IPTV Simple Client installed successfully', xbmcgui.NOTIFICATION_INFO, 5000)
                return True
    return choice


def _sync_pvr():
    if addon.getSetting('auto_sync_pvr').lower() != 'true':
        return False
    return _sync_pvr_force()


def _sync_pvr_force():
    """Sync PVR without checking mode/auto_sync settings."""
    if not is_pvr_iptvsimple_installed():
        prompt_install_pvr()
        return False
    if not _export_pvr_m3u():
        return False
    _export_pvr_epg()
    ok = _configure_pvr_iptvsimple()
    if ok:
        _maybe_show_pvr_first_run()
    return ok


def _maybe_show_pvr_first_run():
    flag = os.path.join(xbmcvfs.translatePath(addon.getAddonInfo('profile')), 'pvr_first_run_shown')
    if os.path.exists(flag):
        return
    try:
        with open(flag, 'w', encoding='utf-8') as f:
            f.write('1')
        xbmcgui.Dialog().ok('XStream Player', 'Live TV has been synced to Kodi PVR.\nOpen TV from the home menu for the best Live TV experience.')
    except Exception:
        pass


def _prefetch_all_data_silent():
    """Refresh cached data without showing a progress dialog."""
    creds = _get_credentials()
    xt_url = creds.get('xtream_url', '')
    xt_user = creds.get('xtream_username', '')
    xt_pwd = creds.get('xtream_password', '')
    m3u_url = creds.get('m3u_url', '')
    if xt_url and xt_user and xt_pwd:
        try:
            _cache_save('xtream_cats_live', IPTV.get_xtream_categories(xt_url, xt_user, xt_pwd, 'live'))
            _cache_save('xtream_streams_live', IPTV.get_xtream_streams(xt_url, xt_user, xt_pwd, 'live'))
        except Exception as e:
            _log(f'Silent prefetch error: {e}')
    if m3u_url:
        try:
            _cache_save('m3u', IPTV.get_m3u_channels(m3u_url))
        except Exception as e:
            _log(f'Silent M3U prefetch error: {e}')


def _prefetch_all_data():
    creds = _get_credentials()
    xt_url = creds.get('xtream_url', '')
    xt_user = creds.get('xtream_username', '')
    xt_pwd = creds.get('xtream_password', '')
    m3u_url = creds.get('m3u_url', '')

    pd = xbmcgui.DialogProgress()
    pd.create('XStream Player', 'Updating data, please wait...')

    fast_steps = []
    if m3u_url:
        fast_steps.append(('M3U channels', lambda: _cache_save('m3u', IPTV.get_m3u_channels(m3u_url))))
    if xt_url and xt_user and xt_pwd:
        fast_steps.extend([
            ('Live TV categories', lambda: _cache_save('xtream_cats_live', IPTV.get_xtream_categories(xt_url, xt_user, xt_pwd, 'live'))),
            ('Live TV channels', lambda: _cache_save('xtream_streams_live', IPTV.get_xtream_streams(xt_url, xt_user, xt_pwd, 'live'))),
            ('Movie categories', lambda: _cache_save('xtream_cats_movie', IPTV.get_xtream_categories(xt_url, xt_user, xt_pwd, 'movie'))),
            ('Movies', lambda: _cache_save('xtream_streams_movie', IPTV.get_xtream_streams(xt_url, xt_user, xt_pwd, 'movie'))),
            ('Series categories', lambda: _cache_save('xtream_cats_series', IPTV.get_xtream_categories(xt_url, xt_user, xt_pwd, 'series'))),
            ('Series', lambda: _cache_save('xtream_streams_series', IPTV.get_xtream_streams(xt_url, xt_user, xt_pwd, 'series'))),
        ])

    slow_steps = [
        ('EPG guide', lambda: EPG(addon).fetch()),
    ]
    if addon.getSetting('auto_sync_pvr').lower() == 'true':
        slow_steps.append(('PVR sync', _sync_pvr))

    all_steps = fast_steps + slow_steps
    total = len(all_steps)

    for idx, (label, fn) in enumerate(all_steps):
        percent = int((idx / total) * 100) if total else 0
        pd.update(percent, f'Loading {label}...')
        try:
            fn()
        except Exception as e:
            _log(f'Prefetch error {label}: {e}')

    pd.close()
    xbmcgui.Dialog().notification('XStream Player', 'Update complete')


def _live_url(url):
    opts = 'reconnect=1&reconnect_streamed=1&reconnect_at_eof=1&reconnect_delay_max=5&analyzeduration=8000000&probesize=10485760'
    if not url or opts in url:
        return url
    if '|' in url:
        return url + '&' + opts
    return url + '|' + opts


def _set_live_props(li):
    li.setMimeType('video/mp2t')
    li.setContentLookup(False)


def play_stream(play_url, name, title='', plot='', icon='', stype='live',
                series_id='', season_num='', ep_id=''):
    if stype == 'live':
        play_url = _live_url(play_url)
    li = xbmcgui.ListItem(path=play_url)
    li.setProperty('IsPlayable', 'true')
    info_tag = li.getVideoInfoTag()
    info_tag.setMediaType('video')
    info_tag.setTitle(title or name)
    info_tag.setPlot(plot or '')
    if icon:
        li.setArt({'icon': icon, 'thumb': icon})
    _prepare_playback_item(li)
    if stype == 'live':
        _set_live_props(li)
    # Resume point for non-live content
    resume_pos = 0
    if stype in ('movie', 'series', 'episode'):
        resume_pos = resume_db.get_position(name, play_url)
        if resume_pos > 0:
            mins = int(resume_pos // 60)
            secs = int(resume_pos % 60)
            choice = xbmcgui.Dialog().yesno(
                'Resume Playback',
                f'Resume from {mins}:{secs:02d}?',
                yeslabel='Resume', nolabel='Start Over')
            if not choice:
                resume_pos = 0
    xbmcplugin.setResolvedUrl(addon_handle, True, listitem=li)
    if resume_pos > 0:
        # Wait for player to start, then seek
        player = xbmc.Player()
        for _ in range(50):
            if player.isPlaying():
                player.seekTime(resume_pos)
                break
            xbmc.sleep(100)
    watch_history.add(name, play_url, icon=icon, stype=stype)
    # Mark series episode as watched
    if stype == 'series' and series_id and season_num and ep_id:
        watched_db.mark_watched(series_id, season_num, ep_id)
    # Monitor playback for resume saving and error recovery
    _monitor_playback(name, play_url)


def _monitor_playback(name, url):
    """Background thread to save resume position and detect stream failures."""
    def _worker():
        player = xbmc.Player()
        xbmc.sleep(3000)  # wait for playback to stabilize
        if not player.isPlaying():
            # Playback failed to start — offer retry
            _log(f'Playback failed to start for: {name}')
            retry = xbmcgui.Dialog().yesno('Stream Error',
                f'Failed to play: {name}', yeslabel='Retry', nolabel='Cancel')
            if retry:
                li = xbmcgui.ListItem(path=url)
                li.setProperty('IsPlayable', 'true')
                _prepare_playback_item(li)
                _set_live_props(li)
                player.play(url, li)
            return
        while player.isPlaying():
            try:
                pos = player.getTime()
                dur = player.getTotalTime()
                if dur > 0:
                    resume_db.save_position(name, url, pos, dur)
            except Exception:
                pass
            xbmc.sleep(5000)
    t = threading.Thread(target=_worker)
    t.daemon = True
    t.start()


def _prepare_playback_item(li):
    if addon.getSetting('use_inputstream_adaptive').lower() == 'true':
        li.setProperty('inputstream', 'inputstream.adaptive')
        li.setProperty('inputstream.adaptive.manifest_type', 'hls')
    custom_ua = addon.getSetting('custom_user_agent')
    if custom_ua:
        li.setProperty('http-header', f'User-Agent={urllib.parse.quote(custom_ua)}')


def _is_adult_category(name):
    adult_keywords = ['xxx', 'adult', '18+', 'mature', 'porn']
    lower = name.lower()
    return any(k in lower for k in adult_keywords)


def _check_pin(required_for=''):
    if addon.getSetting('enable_parental_control').lower() != 'true':
        return True
    pin = addon.getSetting('parental_pin') or '0000'
    kb = xbmcgui.Dialog().input(f'Enter PIN for {required_for}', type=xbmcgui.INPUT_NUMERIC)
    if kb != pin:
        xbmcgui.Dialog().notification('XStream Player', 'Incorrect PIN')
        return False
    return True


def _check_auto_refresh():
    creds = _get_credentials()
    if not creds.get('xtream_url') and not creds.get('m3u_url'):
        return
    rt = RefreshTracker(addon)
    if rt.should_refresh():
        _log('Auto-refresh triggered')
        refresh_data()


def _is_first_refresh():
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    return not os.path.exists(os.path.join(profile, 'first_refresh_done'))

def _mark_first_refresh_done():
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    with open(os.path.join(profile, 'first_refresh_done'), 'w') as f:
        f.write('1')

def refresh_data():
    first_time = _is_first_refresh()
    count = _cache_clear_all()
    _log(f'Cleared {count} cache files')
    rt = RefreshTracker(addon)
    rt.set_last_refresh()
    xbmcgui.Dialog().notification('XStream Player', 'List refreshed')
    _prefetch_all_data()
    if first_time:
        _mark_first_refresh_done()
        choice = xbmcgui.Dialog().yesno(
            'XStream Player',
            'Setup complete! Kodi needs to restart for PVR Live TV to work properly.',
            yeslabel='Restart now',
            nolabel='Later')
        if choice:
            xbmc.executebuiltin('RestartApp')
            return
    xbmc.executebuiltin('Container.Refresh')


def sync_pvr():
    if not is_pvr_iptvsimple_installed():
        prompt_install_pvr()
        return
    pd = xbmcgui.DialogProgress()
    pd.create('XStream Player', 'Syncing Live TV to PVR...')
    ok = False
    try:
        ok = _sync_pvr()
    except Exception as e:
        _log(f'PVR sync error: {e}')
    pd.close()
    if ok:
        xbmcgui.Dialog().notification('XStream Player', 'PVR sync complete')
    else:
        xbmcgui.Dialog().notification('XStream Player', 'PVR sync failed')


def open_pvr():
    xbmcplugin.endOfDirectory(addon_handle, succeeded=False)
    xbmc.executebuiltin('ActivateWindow(TVChannels)')


def open_pvr_guide():
    xbmcplugin.endOfDirectory(addon_handle, succeeded=False)
    xbmc.executebuiltin('ActivateWindow(TVGuide)')



def _check_account_expiry():
    """Warn if Xtream account expires within 7 days."""
    creds = _get_credentials()
    url = creds.get('xtream_url', '')
    user = creds.get('xtream_username', '')
    pwd = creds.get('xtream_password', '')
    if not url or not user or not pwd:
        return
    # Only check once per session using a flag file
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    flag = os.path.join(profile, 'expiry_checked')
    if os.path.exists(flag):
        try:
            age = time.time() - os.path.getmtime(flag)
            if age < 86400:  # check once per day
                return
        except Exception:
            pass
    try:
        info = IPTV.validate_xtream(url, user, pwd)
        if info and info.get('exp_date'):
            import datetime
            exp_dt = datetime.datetime.fromtimestamp(int(info['exp_date']))
            days_left = (exp_dt - datetime.datetime.now()).days
            if days_left < 7:
                xbmcgui.Dialog().notification(
                    'XStream Player',
                    f'Account expires in {days_left} days!',
                    xbmcgui.NOTIFICATION_WARNING, 5000)
        with open(flag, 'w') as f:
            f.write('1')
    except Exception:
        pass


def main_menu():
    _log('Opening main menu')
    # Give Kodi time to fully initialize on first launch
    xbmc.sleep(500)
    _check_auto_refresh()
    _check_account_expiry()
    if addon.getSetting('prefetch_on_startup').lower() == 'true':
        creds = _get_credentials()
        has_m3u = bool(creds.get('m3u_url'))
        has_xtream = bool(creds.get('xtream_url'))
        needs_prefetch = False
        if has_m3u and not _cache_valid('m3u'):
            needs_prefetch = True
        if has_xtream and not _cache_valid('xtream_streams_live'):
            needs_prefetch = True
        if needs_prefetch:
            _prefetch_all_data()
    visible = set(pm.get_visible_categories())
    creds = _get_credentials()
    has_live = bool(creds.get('xtream_url') or creds.get('m3u_url'))
    has_movies = bool(creds.get('xtream_url'))
    has_series = bool(creds.get('xtream_url'))
    has_replay = bool(creds.get('xtream_url'))
    has_favs = len(fav.get_all()) > 0

    items = []
    if 'live' in visible and has_live:
        items.append(('Live TV - PVR', {'mode': 'open_pvr'}, 'DefaultAddonPVRClient.png'))
        items.append(('Live TV - Classic', {'mode': 'live_menu'}, 'DefaultTVShows.png'))
    if 'guide' in visible and has_live:
        items.append(('Guide', {'mode': 'open_pvr_guide'}, 'DefaultPVRGuide.png'))
    if 'movies' in visible and has_movies:
        items.append(('Movies', {'mode': 'movies_menu'}, 'DefaultMovies.png'))
    if 'series' in visible and has_series:
        items.append(('Series', {'mode': 'series_menu'}, 'DefaultTVShows.png'))
    if 'replay' in visible and has_replay:
        items.append(('Replay', {'mode': 'replay_menu'}, 'DefaultAddonsUpdates.png'))
    if 'search' in visible and has_live:
        items.append(('Search', {'mode': 'search_global'}, 'DefaultAddonsSearch.png'))
    if 'favorites' in visible and has_favs:
        items.append(('Favorites', {'mode': 'favorites_menu'}, 'DefaultFavourites.png'))
    items.append(('Tools', {'mode': 'tools_menu'}, 'DefaultAddonService.png'))

    for label, q, icon in items:
        li = xbmcgui.ListItem(label=label)
        li.setArt({'icon': icon})
        is_folder = q.get('mode') not in ('open_pvr', 'open_pvr_guide')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=is_folder)
    xbmcplugin.endOfDirectory(addon_handle)


def tools_menu():
    _log('Opening tools menu')
    creds = _get_credentials()
    active_name = creds.get('name', 'Profile 1')
    hide_adult = addon.getSetting('hide_adult_categories').lower() == 'true'
    pvr_synced = os.path.exists(_pvr_m3u_path())
    items = [
        ('[COLOR yellow]Settings[/COLOR]', {'mode': 'settings'}, 'DefaultAddonService.png'),
        (f'Refresh List ({active_name})', {'mode': 'refresh_data'}, 'DefaultAddonService.png'),
        ('Manage visible categories', {'mode': 'manage_visible_cats'}, 'DefaultAddonService.png'),
        (f'Hide adult categories: {"ON" if hide_adult else "OFF"}', {'mode': 'toggle_setting', 'key': 'hide_adult_categories'}, 'DefaultAddonService.png'),
        ('Clear All Cache', {'mode': 'clear_all_caches'}, 'DefaultAddonService.png'),
        ('Clear EPG Cache', {'mode': 'clear_epg_cache'}, 'DefaultAddonService.png'),
        ('Clear Channel Cache', {'mode': 'clear_channel_cache'}, 'DefaultAddonService.png'),
        ('Clear TMDB Cache', {'mode': 'clear_tmdb_cache'}, 'DefaultAddonService.png'),
        ('Clear Watch History', {'mode': 'history_clear'}, 'DefaultAddonService.png'),
        ('Switch Profile', {'mode': 'switch_profile'}, 'DefaultAddonService.png'),
        ('Test Connection', {'mode': 'test_connection'}, 'DefaultAddonService.png'),
        ('Account Info', {'mode': 'account_info'}, 'DefaultAddonService.png'),
    ]
    for label, q, icon in items:
        li = xbmcgui.ListItem(label=label)
        li.setArt({'icon': icon})
        is_folder = q.get('mode') not in ('settings', 'refresh_data', 'toggle_setting', 'manage_visible_cats',
                                             'clear_all_caches', 'clear_epg_cache', 'clear_channel_cache', 'clear_tmdb_cache')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=is_folder)
    xbmcplugin.endOfDirectory(addon_handle)


def manage_visible_cats():
    visible = set(pm.get_visible_categories())
    options = ['Live TV', 'Guide', 'Movies', 'Series', 'Replay', 'Search', 'Favorites']
    keys = ['live', 'guide', 'movies', 'series', 'replay', 'search', 'favorites']
    preselect = [i for i, k in enumerate(keys) if k in visible]
    dialog = xbmcgui.Dialog()
    result = dialog.multiselect('Select visible categories', options, preselect=preselect)
    if result is None:
        return
    new_visible = [keys[i] for i in result] if result else []
    pm.set_visible_categories(new_visible)
    xbmcgui.Dialog().notification('XStream Player', 'Categories updated')
    xbmc.executebuiltin('Container.Refresh')


def live_menu():
    creds = _get_credentials()
    xtream_url = creds.get('xtream_url', '')
    m3u_url = creds.get('m3u_url', '')
    _log(f'live_menu: xtream_url={bool(xtream_url)}, m3u_url={bool(m3u_url)}')

    if not xtream_url and not m3u_url:
        li = xbmcgui.ListItem(label='No playlist configured. Go to Settings.')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url='', listitem=li, isFolder=False)

        xbmcplugin.endOfDirectory(addon_handle)
        return

    if xtream_url and not m3u_url:
        li = xbmcgui.ListItem(label='[COLOR yellow]Search[/COLOR]')
        li.setArt({'icon': 'DefaultAddonsSearch.png'})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'search_global'}), listitem=li, isFolder=True)
        li = xbmcgui.ListItem(label='[COLOR gold]Favorites[/COLOR]')
        li.setArt({'icon': 'DefaultFolder.png'})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'favorites_menu', 'stype': 'live'}), listitem=li, isFolder=True)
        xtream_categories('live')
        return
    if m3u_url and not xtream_url:
        li = xbmcgui.ListItem(label='[COLOR yellow]Search[/COLOR]')
        li.setArt({'icon': 'DefaultAddonsSearch.png'})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'search_global'}), listitem=li, isFolder=True)
        li = xbmcgui.ListItem(label='[COLOR gold]Favorites[/COLOR]')
        li.setArt({'icon': 'DefaultFolder.png'})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'favorites_menu', 'stype': 'live'}), listitem=li, isFolder=True)
        m3u_live()
        return

    li = xbmcgui.ListItem(label='[COLOR yellow]Search[/COLOR]')
    li.setArt({'icon': 'DefaultAddonsSearch.png'})
    xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'search_global'}), listitem=li, isFolder=True)
    li = xbmcgui.ListItem(label='[COLOR gold]Favorites[/COLOR]')
    li.setArt({'icon': 'DefaultFolder.png'})
    xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'favorites_menu', 'stype': 'live'}), listitem=li, isFolder=True)
    li = xbmcgui.ListItem(label='Xtream Codes - Live TV')
    li.setArt({'icon': 'DefaultFolder.png'})
    xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'xtream_categories', 'type': 'live'}), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label='M3U - Live Channels')
    li.setArt({'icon': 'DefaultFolder.png'})
    xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'm3u_live'}), listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(addon_handle)


def m3u_live():
    creds = _get_credentials()
    channels = _get_cached_m3u_channels(creds.get('m3u_url', ''))
    groups = {}
    for ch in channels:
        grp = ch.get('group') or 'General'
        groups.setdefault(grp, []).append(ch)
    for grp in sorted(groups.keys()):
        li = xbmcgui.ListItem(label=grp)
        li.setArt({'icon': 'DefaultFolder.png'})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'm3u_group', 'group': grp}), listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(addon_handle)


def _epg_enabled():
    return addon.getSetting('show_epg_live').lower() == 'true'


def m3u_group(group):
    xbmcplugin.setContent(addon_handle, 'livetv')
    show_epg = _epg_enabled()
    epg = EPG(addon)
    if show_epg:
        epg.load()
    creds = _get_credentials()
    channels = _get_cached_m3u_channels(creds.get('m3u_url', ''))
    for ch in channels:
        if (ch.get('group') or 'General') != group:
            continue
        name = ch.get('name', 'Unknown')
        tvg_id = ch.get('tvg_id') or name
        url = ch.get('url')
        item_id = ch.get('tvg_id') or url
        plot, display_name, current_title = _make_epg_info(epg, tvg_id, name) if show_epg else ('', name, '')
        li = xbmcgui.ListItem(label=display_name)
        info_tag = li.getVideoInfoTag()
        info_tag.setMediaType('video')
        info_tag.setTitle(current_title or name)
        info_tag.setPlot(plot)
        if ch.get('logo'):
            li.setArt({'icon': ch['logo'], 'thumb': ch['logo']})
        li.setProperty('IsPlayable', 'true')
        li.setProperty('IsLiveTV', '1')
        li.setProperty('previewpath', _live_url(url))
        _prepare_playback_item(li)
        _set_live_props(li)
        live_play_url = _live_url(url)
        action = 'Add to Favorites' if not fav.is_favorite(item_id) else 'Remove from Favorites'
        ctx = [(
            action,
            f'RunPlugin({build_url({"mode": "toggle_fav", "id": item_id, "name": name, "stype": "live", "icon": ch.get("logo", ""), "url": url, "epg_id": tvg_id})})'
        )]
        li.addContextMenuItems(ctx)
        q = {'mode': 'play_stream', 'url': live_play_url, 'name': name, 'title': current_title or name, 'plot': plot, 'icon': ch.get('logo', '')}
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=False)
    _apply_sort()

    xbmcplugin.endOfDirectory(addon_handle)


def xtream_categories(stype):
    creds = _get_credentials()
    url = creds.get('xtream_url', '')
    user = creds.get('xtream_username', '')
    pwd = creds.get('xtream_password', '')
    cats = _get_cached_xtream_categories(url, user, pwd, stype)
    hide_adult = addon.getSetting('hide_adult_categories').lower() == 'true'
    for c in cats:
        name = c.get('category_name', 'Unknown')
        if hide_adult and _is_adult_category(name):
            continue
        count = len(_get_cached_xtream_streams(url, user, pwd, stype, c.get('category_id', '')))
        name = f"{name}  [COLOR gray]({count})[/COLOR]"
        li = xbmcgui.ListItem(label=name)
        li.setArt({'icon': 'DefaultFolder.png'})
        q = {'mode': 'xtream_streams', 'type': stype, 'cat_id': c.get('category_id', '')}
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=True)
    _apply_sort()
    xbmcplugin.endOfDirectory(addon_handle)


def xtream_streams(stype, cat_id, page=1):
    creds = _get_credentials()
    url = creds.get('xtream_url', '')
    user = creds.get('xtream_username', '')
    pwd = creds.get('xtream_password', '')
    streams = _get_cached_xtream_streams(url, user, pwd, stype, cat_id)
    epg = EPG(addon)
    epg.load()

    per_page = 50
    total = len(streams)
    start = (page - 1) * per_page
    end = start + per_page
    page_streams = streams[start:end]

    if stype == 'movie' and url and user and pwd:
        _prefetch_vod_info_batch(page_streams, url, user, pwd)

    if stype == 'live':
        xbmcplugin.setContent(addon_handle, 'livetv')
    show_epg = _epg_enabled() if stype == 'live' else False
    for s in page_streams:
        name = s.get('name', 'Unknown')
        if stype == 'live':
            sid = str(s.get('stream_id', ''))
            epg_id = s.get('epg_channel_id') or sid
            play_url = _live_url(IPTV.build_xtream_stream_url(url, user, pwd, s, 'live'))
            plot, display_name, current_title = _make_epg_info(epg, epg_id, name) if show_epg else ('', name, '')
            li = xbmcgui.ListItem(label=display_name)
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType('video')
            info_tag.setTitle(current_title or name)
            info_tag.setPlot(plot)
            if s.get('stream_icon'):
                li.setArt({'icon': s['stream_icon'], 'thumb': s['stream_icon']})
            li.setProperty('IsPlayable', 'true')
            li.setProperty('IsLiveTV', '1')
            li.setProperty('previewpath', play_url)
            _prepare_playback_item(li)
            _set_live_props(li)
            action = 'Add to Favorites' if not fav.is_favorite(sid) else 'Remove from Favorites'
            ctx = [(
                action,
                f'RunPlugin({build_url({"mode": "toggle_fav", "id": sid, "name": name, "stype": "live", "icon": s.get("stream_icon", ""), "url": play_url, "epg_id": epg_id})})'
            )]
            li.addContextMenuItems(ctx)
            q = {'mode': 'play_stream', 'url': play_url, 'name': name, 'title': current_title or name, 'plot': plot, 'icon': s.get('stream_icon', '')}
            xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=False)
        elif stype == 'movie':
            sid = str(s.get('stream_id', ''))
            play_url = IPTV.build_xtream_stream_url(url, user, pwd, s, 'movie')
            info = _enrich_movie_info(s, url, user, pwd)
            li = xbmcgui.ListItem(label=name)
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType('movie')
            info_tag.setTitle(name)
            info_tag.setPlot(info['plot'])
            if info['rating']:
                try:
                    info_tag.setRating(float(info['rating']))
                except Exception:
                    pass
            if info['year']:
                try:
                    info_tag.setYear(int(info['year']))
                except Exception:
                    pass
            art = {}
            if info['poster_url']:
                art['icon'] = info['poster_url']
                art['thumb'] = info['poster_url']
                art['poster'] = info['poster_url']
            li.setArt(art)
            li.setProperty('IsPlayable', 'true')
            _prepare_playback_item(li)
            action = 'Add to Favorites' if not fav.is_favorite(sid) else 'Remove from Favorites'
            ctx = [(
                action,
                f'RunPlugin({build_url({"mode": "toggle_fav", "id": sid, "name": name, "stype": "movie", "icon": info.get("poster_url", ""), "url": play_url})})'
            )]
            li.addContextMenuItems(ctx)
            xbmcplugin.addDirectoryItem(handle=addon_handle, url=play_url, listitem=li, isFolder=False)
        elif stype == 'series':
            sid = str(s.get('series_id', ''))
            q = {'mode': 'xtream_series', 'series_id': sid}
            series_url = build_url(q)
            li = xbmcgui.ListItem(label=name)
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType('tvshow')
            info_tag.setTitle(name)
            info_tag.setPlot(s.get('plot', ''))
            if s.get('cover'):
                li.setArt({'icon': s['cover'], 'thumb': s['cover']})
            action = 'Add to Favorites' if not fav.is_favorite(sid) else 'Remove from Favorites'
            ctx = [(
                action,
                f'RunPlugin({build_url({"mode": "toggle_fav", "id": sid, "name": name, "stype": "series", "icon": s.get("cover", ""), "url": series_url})})'
            )]
            li.addContextMenuItems(ctx)
            xbmcplugin.addDirectoryItem(handle=addon_handle, url=series_url, listitem=li, isFolder=True)

    if stype in ('movie', 'series') and end < total:
        li = xbmcgui.ListItem(label='[COLOR yellow]Next Page >>[/COLOR]')
        li.setArt({'icon': 'DefaultFolder.png'})
        q = {'mode': 'xtream_streams', 'type': stype, 'cat_id': cat_id, 'page': page + 1}
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=True)

    _apply_sort()
    xbmcplugin.endOfDirectory(addon_handle)


def xtream_series(series_id):
    creds = _get_credentials()
    url = creds.get('xtream_url', '')
    user = creds.get('xtream_username', '')
    pwd = creds.get('xtream_password', '')
    info = IPTV.get_xtream_series_info(url, user, pwd, series_id)
    episodes = info.get('episodes', {})
    for season_num in sorted(episodes.keys(), key=lambda x: (0, int(x)) if str(x).isdigit() else (1, str(x))):
        total_eps = len(episodes[season_num])
        watched_count = watched_db.get_watched_count(series_id, season_num)
        label = f'Season {season_num}'
        if watched_count > 0:
            label += f'  [COLOR gray]({watched_count}/{total_eps})[/COLOR]'
        li = xbmcgui.ListItem(label=label)
        li.setArt({'icon': 'DefaultFolder.png'})
        q = {'mode': 'xtream_season', 'series_id': series_id, 'season_num': season_num}
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(addon_handle)


def xtream_season(series_id, season_num):
    creds = _get_credentials()
    url = creds.get('xtream_url', '')
    user = creds.get('xtream_username', '')
    pwd = creds.get('xtream_password', '')
    info = IPTV.get_xtream_series_info(url, user, pwd, series_id)
    eps = info.get('episodes', {}).get(season_num, [])
    for ep in eps:
        ep_id = str(ep.get('id', ''))
        title = ep.get('title') or f"Episode {ep.get('episode_num', '?')}"
        watched = watched_db.is_watched(series_id, season_num, ep_id)
        label = f'[COLOR green]✓[/COLOR] {title}' if watched else title
        li = xbmcgui.ListItem(label=label)
        info_tag = li.getVideoInfoTag()
        info_tag.setMediaType('episode')
        info_tag.setTitle(title)
        info_tag.setPlot(ep.get('info', {}).get('plot', ''))
        if watched:
            info_tag.setPlaycount(1)
        movie_image = ep.get('info', {}).get('movie_image')
        if movie_image:
            li.setArt({'icon': movie_image, 'thumb': movie_image})
        li.setProperty('IsPlayable', 'true')
        _prepare_playback_item(li)
        play_url = IPTV.build_xtream_stream_url(url, user, pwd, ep, 'series')
        # Context menu for watched toggle
        if watched:
            ctx_label = 'Mark as Unwatched'
            ctx_url = build_url({'mode': 'toggle_watched', 'series_id': series_id, 'season_num': season_num, 'ep_id': ep_id, 'action': 'unwatched'})
        else:
            ctx_label = 'Mark as Watched'
            ctx_url = build_url({'mode': 'toggle_watched', 'series_id': series_id, 'season_num': season_num, 'ep_id': ep_id, 'action': 'watched'})
        li.addContextMenuItems([(ctx_label, f'RunPlugin({ctx_url})')])
        q = {'mode': 'play_stream', 'url': play_url, 'name': title, 'icon': movie_image or '',
             'stype': 'series', 'series_id': series_id, 'season_num': season_num, 'ep_id': ep_id}
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(addon_handle)


def movies_menu():
    if not _check_pin('Movies'):
        return
    creds = _get_credentials()
    xtream_url = creds.get('xtream_url', '')
    if xtream_url:
        li = xbmcgui.ListItem(label='[COLOR yellow]Search[/COLOR]')
        li.setArt({'icon': 'DefaultAddonsSearch.png'})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'search_global'}), listitem=li, isFolder=True)
        li = xbmcgui.ListItem(label='[COLOR gold]Favorites[/COLOR]')
        li.setArt({'icon': 'DefaultFolder.png'})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'favorites_menu', 'stype': 'movie'}), listitem=li, isFolder=True)
        xtream_categories('movie')
    else:
        li = xbmcgui.ListItem(label='No movie source configured.')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url='', listitem=li, isFolder=False)

        xbmcplugin.endOfDirectory(addon_handle)


def series_menu():
    if not _check_pin('Series'):
        return
    creds = _get_credentials()
    xtream_url = creds.get('xtream_url', '')
    if xtream_url:
        li = xbmcgui.ListItem(label='[COLOR yellow]Search[/COLOR]')
        li.setArt({'icon': 'DefaultAddonsSearch.png'})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'search_global'}), listitem=li, isFolder=True)
        li = xbmcgui.ListItem(label='[COLOR gold]Favorites[/COLOR]')
        li.setArt({'icon': 'DefaultFolder.png'})
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'favorites_menu', 'stype': 'series'}), listitem=li, isFolder=True)
        xtream_categories('series')
    else:
        li = xbmcgui.ListItem(label='No series source configured.')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url='', listitem=li, isFolder=False)

        xbmcplugin.endOfDirectory(addon_handle)


def replay_menu():
    creds = _get_credentials()
    url = creds.get('xtream_url', '')
    user = creds.get('xtream_username', '')
    pwd = creds.get('xtream_password', '')
    streams = _get_cached_xtream_streams(url, user, pwd, 'live')
    epg = EPG(addon)
    epg.load()

    for s in streams:
        if not s.get('tv_archive') and not s.get('catchup'):
            continue
        name = s.get('name', 'Unknown')
        li = xbmcgui.ListItem(label=name)
        if s.get('stream_icon'):
            li.setArt({'icon': s['stream_icon'], 'thumb': s['stream_icon']})
        q = {
            'mode': 'replay_channel',
            'stream_id': str(s.get('stream_id', '')),
            'epg_id': s.get('epg_channel_id') or str(s.get('stream_id', '')),
            'name': name
        }
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=True)
    _apply_sort()

    xbmcplugin.endOfDirectory(addon_handle)


def replay_channel(stream_id, epg_id, name=''):
    epg = EPG(addon)
    epg.load()
    try:
        days_back = int(addon.getSetting('replay_days') or '7')
    except ValueError:
        days_back = 7
    programs = epg.get_programs_for_channel(epg_id, channel_name=name, days_back=days_back)

    if not programs:
        import datetime
        now = datetime.datetime.now()
        for hours_back in range(1, 25):
            start = now - datetime.timedelta(hours=hours_back)
            start_fmt = start.strftime('%Y-%m-%d:%H-%M')
            title = f"{start.strftime('%d/%m %H:%M')} - Replay slot"
            li = xbmcgui.ListItem(label=title)
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType('video')
            info_tag.setTitle(title)
            li.setProperty('IsPlayable', 'true')
            _prepare_playback_item(li)
            q = {
                'mode': 'replay_play',
                'stream_id': stream_id,
                'start': start_fmt,
                'duration': 3600
            }
            xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=False)
    else:
        for prog in programs:
            title = f"{prog['start_str']} - {prog['title']}"
            li = xbmcgui.ListItem(label=title)
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType('video')
            info_tag.setTitle(prog['title'])
            info_tag.setPlot(prog.get('desc', ''))
            li.setProperty('IsPlayable', 'true')
            _prepare_playback_item(li)
            q = {
                'mode': 'replay_play',
                'stream_id': stream_id,
                'start': prog['start_timestamp'],
                'duration': prog['duration_sec']
            }
            xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(addon_handle)


def replay_play(stream_id, start, duration):
    creds = _get_credentials()
    url = creds.get('xtream_url', '')
    user = creds.get('xtream_username', '')
    pwd = creds.get('xtream_password', '')
    try:
        duration = int(duration)
    except (ValueError, TypeError):
        duration = 3600
    play_url = IPTV.build_catchup_url(url, user, pwd, stream_id, start, duration)
    li = xbmcgui.ListItem(path=play_url)
    li.setProperty('IsPlayable', 'true')
    info_tag = li.getVideoInfoTag()
    info_tag.setMediaType('video')
    info_tag.setTitle(f'Replay {stream_id}')
    _prepare_playback_item(li)
    xbmcplugin.setResolvedUrl(addon_handle, True, listitem=li)


def _search_history_path():
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    return os.path.join(profile, 'search_history.json')


def _load_search_history():
    try:
        with open(_search_history_path(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_search_history(history):
    with open(_search_history_path(), 'w', encoding='utf-8') as f:
        json.dump(history[:10], f, ensure_ascii=False)


def search_global(query=None):
    if query is None:
        history = _load_search_history()
        if history:
            options = ['New search...'] + history
            idx = xbmcgui.Dialog().select('Search', options)
            if idx < 0:
                return
            if idx == 0:
                kb = xbmcgui.Dialog().input('Search', type=xbmcgui.INPUT_ALPHANUM)
                if not kb:
                    return
                query = kb
            else:
                query = history[idx - 1]
        else:
            kb = xbmcgui.Dialog().input('Search', type=xbmcgui.INPUT_ALPHANUM)
            if not kb:
                return
            query = kb
    # Save to search history
    history = _load_search_history()
    if query in history:
        history.remove(query)
    history.insert(0, query)
    _save_search_history(history)
    unified_search(query)


def unified_search(query):
    creds = _get_credentials()
    url = creds.get('xtream_url', '')
    user = creds.get('xtream_username', '')
    pwd = creds.get('xtream_password', '')
    qlower = query.lower()
    epg = EPG(addon)
    epg.load()
    show_epg = _epg_enabled()
    hide_adult = addon.getSetting('hide_adult_categories').lower() == 'true'

    live_streams = _get_cached_xtream_streams(url, user, pwd, 'live')
    live_results = [s for s in live_streams if qlower in s.get('name', '').lower()]
    if hide_adult:
        live_results = [s for s in live_results if not _is_adult_category(s.get('name', '') + ' ' + s.get('category_name', ''))]
    if live_results:
        li = xbmcgui.ListItem(label='[COLOR yellow]--- Live TV ---[/COLOR]')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url='', listitem=li, isFolder=False)
        for s in live_results:
            name = s.get('name', 'Unknown')
            sid = str(s.get('stream_id', ''))
            epg_id = s.get('epg_channel_id') or sid
            play_url = _live_url(IPTV.build_xtream_stream_url(url, user, pwd, s, 'live'))
            plot, display_name, current_title = _make_epg_info(epg, epg_id, name) if show_epg else ('', name, '')
            li = xbmcgui.ListItem(label=display_name)
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType('video')
            info_tag.setTitle(current_title or name)
            info_tag.setPlot(plot)
            if s.get('stream_icon'):
                li.setArt({'icon': s['stream_icon'], 'thumb': s['stream_icon'], 'poster': s['stream_icon']})
            li.setProperty('IsPlayable', 'true')
            _prepare_playback_item(li)
            _set_live_props(li)
            action = 'Add to Favorites' if not fav.is_favorite(sid) else 'Remove from Favorites'
            ctx = [(
                action,
                f'RunPlugin({build_url({"mode": "toggle_fav", "id": sid, "name": name, "stype": "live", "icon": s.get("stream_icon", ""), "url": play_url, "epg_id": epg_id})})'
            )]
            li.addContextMenuItems(ctx)
            q = {'mode': 'play_stream', 'url': play_url, 'name': name, 'title': current_title or name, 'plot': plot, 'icon': s.get('stream_icon', '')}
            xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=False)

    movie_streams = _get_cached_xtream_streams(url, user, pwd, 'movie')
    movie_results = [s for s in movie_streams if qlower in s.get('name', '').lower()]
    if hide_adult:
        movie_results = [s for s in movie_results if not _is_adult_category(s.get('name', '') + ' ' + s.get('category_name', ''))]
    if movie_results:
        li = xbmcgui.ListItem(label='[COLOR yellow]--- Movies ---[/COLOR]')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url='', listitem=li, isFolder=False)
        for s in movie_results:
            name = s.get('name', 'Unknown')
            sid = str(s.get('stream_id', ''))
            play_url = IPTV.build_xtream_stream_url(url, user, pwd, s, 'movie')
            info = _enrich_movie_info(s, url, user, pwd)
            li = xbmcgui.ListItem(label=name)
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType('movie')
            info_tag.setTitle(name)
            info_tag.setPlot(info['plot'])
            if info['rating']:
                try:
                    info_tag.setRating(float(info['rating']))
                except Exception:
                    pass
            if info['year']:
                try:
                    info_tag.setYear(int(info['year']))
                except Exception:
                    pass
            art = {}
            if info['poster_url']:
                art['icon'] = info['poster_url']
                art['thumb'] = info['poster_url']
                art['poster'] = info['poster_url']
            li.setArt(art)
            li.setProperty('IsPlayable', 'true')
            _prepare_playback_item(li)
            action = 'Add to Favorites' if not fav.is_favorite(sid) else 'Remove from Favorites'
            ctx = [(
                action,
                f'RunPlugin({build_url({"mode": "toggle_fav", "id": sid, "name": name, "stype": "movie", "icon": info.get("poster_url", ""), "url": play_url})})'
            )]
            li.addContextMenuItems(ctx)
            xbmcplugin.addDirectoryItem(handle=addon_handle, url=play_url, listitem=li, isFolder=False)

    series_streams = _get_cached_xtream_streams(url, user, pwd, 'series')
    series_results = [s for s in series_streams if qlower in s.get('name', '').lower()]
    if hide_adult:
        series_results = [s for s in series_results if not _is_adult_category(s.get('name', '') + ' ' + s.get('category_name', ''))]
    if series_results:
        li = xbmcgui.ListItem(label='[COLOR yellow]--- Series ---[/COLOR]')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url='', listitem=li, isFolder=False)
        for s in series_results:
            name = s.get('name', 'Unknown')
            sid = str(s.get('series_id', ''))
            series_url = build_url({'mode': 'xtream_series', 'series_id': sid})
            li = xbmcgui.ListItem(label=name)
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType('tvshow')
            info_tag.setTitle(name)
            info_tag.setPlot(s.get('plot', ''))
            if s.get('cover'):
                li.setArt({'icon': s['cover'], 'thumb': s['cover']})
            action = 'Add to Favorites' if not fav.is_favorite(sid) else 'Remove from Favorites'
            ctx = [(
                action,
                f'RunPlugin({build_url({"mode": "toggle_fav", "id": sid, "name": name, "stype": "series", "icon": s.get("cover", ""), "url": series_url})})'
            )]
            li.addContextMenuItems(ctx)
            xbmcplugin.addDirectoryItem(handle=addon_handle, url=series_url, listitem=li, isFolder=True)

    if not live_results and not movie_results and not series_results:
        li = xbmcgui.ListItem(label='No results found.')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url='', listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(addon_handle)


def search_m3u(query=None):
    if query is None:
        kb = xbmcgui.Dialog().input('Search M3U Channels', type=xbmcgui.INPUT_ALPHANUM)
        if not kb:
            return
        query = kb
    show_epg = _epg_enabled()
    epg = EPG(addon)
    if show_epg:
        epg.load()
    creds = _get_credentials()
    channels = _get_cached_m3u_channels(creds.get('m3u_url', ''))
    qlower = query.lower()
    for ch in channels:
        if qlower not in ch.get('name', '').lower() and qlower not in ch.get('group', '').lower():
            continue
        name = ch.get('name', 'Unknown')
        tvg_id = ch.get('tvg_id') or name
        url = ch.get('url')
        item_id = ch.get('tvg_id') or url
        plot, display_name, current_title = _make_epg_info(epg, tvg_id, name) if show_epg else ('', name, '')
        li = xbmcgui.ListItem(label=display_name)
        info_tag = li.getVideoInfoTag()
        info_tag.setMediaType('video')
        info_tag.setTitle(current_title or name)
        info_tag.setPlot(plot)
        if ch.get('logo'):
            li.setArt({'icon': ch['logo'], 'thumb': ch['logo']})
        li.setProperty('IsPlayable', 'true')
        _prepare_playback_item(li)
        _set_live_props(li)
        action = 'Add to Favorites' if not fav.is_favorite(item_id) else 'Remove from Favorites'
        ctx = [(
            action,
            f'RunPlugin({build_url({"mode": "toggle_fav", "id": item_id, "name": name, "stype": "live", "icon": ch.get("logo", ""), "url": url, "epg_id": tvg_id})})'
        )]
        li.addContextMenuItems(ctx)
        q = {'mode': 'play_stream', 'url': url, 'name': name, 'title': current_title or name, 'plot': plot, 'icon': ch.get('logo', '')}
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=False)
    _apply_sort()

    xbmcplugin.endOfDirectory(addon_handle)


def search_xtream(stype, query=None):
    if query is None:
        kb = xbmcgui.Dialog().input(f'Search {stype.capitalize()}', type=xbmcgui.INPUT_ALPHANUM)
        if not kb:
            return
        query = kb
    creds = _get_credentials()
    url = creds.get('xtream_url', '')
    user = creds.get('xtream_username', '')
    pwd = creds.get('xtream_password', '')
    streams = _get_cached_xtream_streams(url, user, pwd, stype)
    qlower = query.lower()
    filtered = [s for s in streams if qlower in s.get('name', '').lower()]
    show_epg = _epg_enabled() if stype == 'live' else False
    epg = EPG(addon)
    if show_epg:
        epg.load()

    for s in filtered:
        name = s.get('name', 'Unknown')
        if stype == 'live':
            sid = str(s.get('stream_id', ''))
            epg_id = s.get('epg_channel_id') or sid
            play_url = _live_url(IPTV.build_xtream_stream_url(url, user, pwd, s, 'live'))
            plot, display_name, current_title = _make_epg_info(epg, epg_id, name) if show_epg else ('', name, '')
            li = xbmcgui.ListItem(label=display_name)
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType('video')
            info_tag.setTitle(current_title or name)
            info_tag.setPlot(plot)
            if s.get('stream_icon'):
                li.setArt({'icon': s['stream_icon'], 'thumb': s['stream_icon'], 'poster': s['stream_icon']})
            li.setProperty('IsPlayable', 'true')
            _prepare_playback_item(li)
            _set_live_props(li)
            action = 'Add to Favorites' if not fav.is_favorite(sid) else 'Remove from Favorites'
            ctx = [(
                action,
                f'RunPlugin({build_url({"mode": "toggle_fav", "id": sid, "name": name, "stype": "live", "icon": s.get("stream_icon", ""), "url": play_url, "epg_id": epg_id})})'
            )]
            li.addContextMenuItems(ctx)
            q = {'mode': 'play_stream', 'url': play_url, 'name': name, 'title': current_title or name, 'plot': plot, 'icon': s.get('stream_icon', '')}
            xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=False)
        elif stype == 'movie':
            sid = str(s.get('stream_id', ''))
            play_url = IPTV.build_xtream_stream_url(url, user, pwd, s, 'movie')
            li = xbmcgui.ListItem(label=name)
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType('movie')
            info_tag.setTitle(name)
            info_tag.setPlot(s.get('plot', ''))
            if s.get('stream_icon'):
                li.setArt({'icon': s['stream_icon'], 'thumb': s['stream_icon']})
            li.setProperty('IsPlayable', 'true')
            _prepare_playback_item(li)
            action = 'Add to Favorites' if not fav.is_favorite(sid) else 'Remove from Favorites'
            ctx = [(
                action,
                f'RunPlugin({build_url({"mode": "toggle_fav", "id": sid, "name": name, "stype": "movie", "icon": s.get("stream_icon", ""), "url": play_url})})'
            )]
            li.addContextMenuItems(ctx)
            xbmcplugin.addDirectoryItem(handle=addon_handle, url=play_url, listitem=li, isFolder=False)
        elif stype == 'series':
            sid = str(s.get('series_id', ''))
            q = {'mode': 'xtream_series', 'series_id': sid}
            series_url = build_url(q)
            li = xbmcgui.ListItem(label=name)
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType('tvshow')
            info_tag.setTitle(name)
            info_tag.setPlot(s.get('plot', ''))
            if s.get('cover'):
                li.setArt({'icon': s['cover'], 'thumb': s['cover']})
            action = 'Add to Favorites' if not fav.is_favorite(sid) else 'Remove from Favorites'
            ctx = [(
                action,
                f'RunPlugin({build_url({"mode": "toggle_fav", "id": sid, "name": name, "stype": "series", "icon": s.get("cover", ""), "url": series_url})})'
            )]
            li.addContextMenuItems(ctx)
            xbmcplugin.addDirectoryItem(handle=addon_handle, url=series_url, listitem=li, isFolder=True)
    _apply_sort()

    xbmcplugin.endOfDirectory(addon_handle)


def history_menu():
    items = watch_history.get_all()
    if not items:
        li = xbmcgui.ListItem(label='No watch history yet')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url='', listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(addon_handle)
        return
    for entry in items:
        name = entry.get('name', '')
        url = entry.get('url', '')
        icon = entry.get('icon', '')
        stype = entry.get('stype', 'live')
        ts = entry.get('timestamp', 0)
        import datetime
        when = datetime.datetime.fromtimestamp(ts).strftime('%d/%m %H:%M') if ts else ''
        label = f'{name}  [COLOR gray]({when})[/COLOR]' if when else name
        li = xbmcgui.ListItem(label=label)
        if icon:
            li.setArt({'icon': icon, 'thumb': icon})
        li.setProperty('IsPlayable', 'true')
        _prepare_playback_item(li)
        q = {'mode': 'play_stream', 'url': url, 'name': name, 'icon': icon, 'stype': stype}
        li.addContextMenuItems([
            ('Remove from History', f'RunPlugin({build_url({"mode": "history_remove", "name": name, "stype": stype})})'),
            ('Clear All History', f'RunPlugin({build_url({"mode": "history_clear"})})'),
        ])
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=False)
    xbmcplugin.endOfDirectory(addon_handle)


def favorites_menu(filter_stype=None, folder=None):
    folders = fav.get_folders()
    # If no folder specified and there are multiple folders, show folder list
    if folder is None and len(folders) > 1:
        for fname in folders:
            count = len(fav.get_all(fname))
            li = xbmcgui.ListItem(label=f'{fname}  [COLOR gray]({count})[/COLOR]')
            li.setArt({'icon': 'DefaultFavourites.png'})
            ctx_items = [
                ('Export as M3U', f'RunPlugin({build_url({"mode": "export_favorites", "folder": fname})})'),
            ]
            if fname != 'Favorites':
                ctx_items.append(('Delete Folder', f'RunPlugin({build_url({"mode": "fav_delete_folder", "folder": fname})})'))
            li.addContextMenuItems(ctx_items)
            q = {'mode': 'favorites_menu', 'folder': fname}
            xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url(q), listitem=li, isFolder=True)
        # Add "New Folder" option
        li = xbmcgui.ListItem(label='[COLOR yellow]+ New Folder[/COLOR]')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=build_url({'mode': 'fav_new_folder'}), listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(addon_handle)
        return

    # Show items in a specific folder (or the only folder)
    if folder is None:
        folder = folders[0] if folders else 'Favorites'
    items = fav.get_all(folder)
    if filter_stype:
        items = [i for i in items if i.get('stype') == filter_stype]
    if not items:
        li = xbmcgui.ListItem(label='No favorites yet.')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url='', listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(addon_handle)
        return
    epg = EPG(addon)
    epg.load()
    for item in items:
        stype = item.get('stype', 'live')
        name = item.get('name', 'Unknown')
        li = xbmcgui.ListItem(label=name)
        info_tag = li.getVideoInfoTag()
        info_tag.setMediaType('video')
        info_tag.setTitle(name)
        if item.get('icon'):
            li.setArt({'icon': item['icon'], 'thumb': item['icon']})
        li.setProperty('IsPlayable', 'true')
        _prepare_playback_item(li)
        plot = ''
        current_title = ''
        if stype == 'live':
            _set_live_props(li)
            plot, display_name, current_title = _make_epg_info(epg, item.get('epg_id', ''), name) if _epg_enabled() else ('', name, '')
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType('video')
            info_tag.setTitle(current_title or name)
            info_tag.setPlot(plot)
        ctx = [
            ('Remove from Favorites',
             f'RunPlugin({build_url({"mode": "toggle_fav", "id": item.get("id"), "name": name, "stype": stype, "icon": item.get("icon", ""), "url": item.get("url", ""), "epg_id": item.get("epg_id", "")})})'),
        ]
        # Move to folder option
        if len(folders) > 1:
            ctx.append(('Move to Folder',
                f'RunPlugin({build_url({"mode": "fav_move", "id": item.get("id"), "name": name, "stype": stype, "icon": item.get("icon", ""), "url": item.get("url", ""), "epg_id": item.get("epg_id", ""), "from_folder": folder})})'))
        li.addContextMenuItems(ctx)
        if stype == 'live':
            q = {'mode': 'play_stream', 'url': item.get('url', ''), 'name': name, 'title': current_title or name, 'plot': plot, 'icon': item.get('icon', '')}
            item_url = build_url(q)
        else:
            item_url = item.get('url')
        xbmcplugin.addDirectoryItem(handle=addon_handle, url=item_url, listitem=li, isFolder=stype == 'series')

    xbmcplugin.endOfDirectory(addon_handle)


def toggle_favorite(item_id, name, stype, icon, url, epg_id=''):
    is_now_fav = fav.toggle({'id': item_id, 'name': name, 'stype': stype, 'icon': icon, 'url': url, 'epg_id': epg_id})
    xbmcgui.Dialog().notification('XStream Player', 'Added to Favorites' if is_now_fav else 'Removed from Favorites')
    xbmc.executebuiltin('Container.Refresh')


def switch_profile():
    current = addon.getSetting('active_profile') or 'Profile 1'
    profiles = []
    for i in range(1, 11):
        name = addon.getSetting(f'profile_{i}_name') or f'Profile {i}'
        marker = ' [COLOR green]*[/COLOR]' if f'Profile {i}' == current else ''
        profiles.append((f'Profile {i}', f'{name}{marker}'))
    labels = [p[1] for p in profiles]
    idx = xbmcgui.Dialog().select('Switch Profile', labels)
    if idx < 0:
        return
    selected = profiles[idx][0]
    if selected == current:
        xbmcgui.Dialog().notification('XStream Player', 'Already on this profile')
        return
    addon.setSetting('active_profile', selected)
    _cache_clear_all()
    xbmcgui.Dialog().notification('XStream Player', f'Switched to {profiles[idx][1].replace("[COLOR green]*[/COLOR]", "").strip()}')
    xbmc.executebuiltin('Container.Refresh')


def test_connection():
    creds = _get_credentials()
    url = creds.get('xtream_url', '')
    user = creds.get('xtream_username', '')
    pwd = creds.get('xtream_password', '')
    if not url or not user or not pwd:
        xbmcgui.Dialog().ok('Test Connection', 'No Xtream credentials configured.')
        return
    pd = xbmcgui.DialogProgress()
    pd.create('XStream Player', 'Testing connection...')
    info = IPTV.validate_xtream(url, user, pwd)
    pd.close()
    if info is None:
        xbmcgui.Dialog().ok('Test Connection', 'Connection FAILED.\nCheck your server URL, username, and password.')
        return
    import datetime
    exp = info.get('exp_date', '')
    exp_str = 'Never'
    if exp:
        try:
            exp_dt = datetime.datetime.fromtimestamp(int(exp))
            exp_str = exp_dt.strftime('%Y-%m-%d %H:%M')
            days_left = (exp_dt - datetime.datetime.now()).days
            exp_str += f' ({days_left} days left)'
        except (ValueError, TypeError):
            exp_str = str(exp)
    msg = (f'Status: {info.get("status", "active")}\n'
           f'Expires: {exp_str}\n'
           f'Max connections: {info.get("max_connections", "N/A")} | Active: {info.get("active_cons", "0")}')
    xbmcgui.Dialog().ok('Connection OK', msg)


def account_info():
    creds = _get_credentials()
    url = creds.get('xtream_url', '')
    user = creds.get('xtream_username', '')
    pwd = creds.get('xtream_password', '')
    if not url or not user or not pwd:
        xbmcgui.Dialog().ok('Account Info', 'No Xtream credentials configured.')
        return
    info = IPTV.validate_xtream(url, user, pwd)
    if info is None:
        xbmcgui.Dialog().ok('Account Info', 'Could not retrieve account info.')
        return
    import datetime
    exp = info.get('exp_date', '')
    exp_str = 'Never'
    if exp:
        try:
            exp_dt = datetime.datetime.fromtimestamp(int(exp))
            exp_str = exp_dt.strftime('%Y-%m-%d %H:%M')
        except (ValueError, TypeError):
            exp_str = str(exp)
    lines = [
        f'Status: {info.get("status", "unknown")}',
        f'Expires: {exp_str}',
        f'Max connections: {info.get("max_connections", "N/A")}',
        f'Active connections: {info.get("active_cons", "0")}',
        f'Trial: {"Yes" if info.get("is_trial") == "1" else "No"}',
        f'Server: {info.get("server_url", "N/A")}',
    ]
    xbmcgui.Dialog().textviewer('Account Info', '\n'.join(lines))


def _validate_settings():
    """Validate settings after user closes settings dialog."""
    creds = _get_credentials()
    xt_url = creds.get('xtream_url', '')
    m3u_url = creds.get('m3u', '')
    # Validate URLs
    for label, url in [('Xtream Server URL', xt_url), ('M3U URL', m3u_url)]:
        if url and not url.startswith(('http://', 'https://')):
            xbmcgui.Dialog().notification('XStream Player',
                f'{label} must start with http:// or https://',
                xbmcgui.NOTIFICATION_WARNING, 5000)


def _check_credentials_pin():
    if addon.getSetting('lock_credentials').lower() != 'true':
        return True
    pin = addon.getSetting('credentials_pin') or '0000'
    kb = xbmcgui.Dialog().input('Enter PIN to access settings', type=xbmcgui.INPUT_NUMERIC)
    if kb != pin:
        xbmcgui.Dialog().notification('XStream Player', 'Incorrect PIN', xbmcgui.NOTIFICATION_WARNING)
        return False
    return True


def settings():
    xbmcplugin.endOfDirectory(addon_handle, succeeded=False)
    if not _check_credentials_pin():
        return
    addon.openSettings()
    _validate_settings()
    xbmc.executebuiltin('Container.Refresh')





def _make_epg_info(epg, channel_id, channel_name):
    if not _epg_enabled():
        return '', channel_name, ''
    matched_id = epg._find_channel_id(channel_id, channel_name)
    if not matched_id:
        return '', channel_name, ''
    now = time.time()
    upcoming = []
    current_title = ''
    current_desc = ''
    for prog in epg.programs[matched_id]:
        start = epg._apply_offset(_parse_xmltv_time(prog['start']))
        stop = epg._apply_offset(_parse_xmltv_time(prog['stop']))
        if start and start > now:
            upcoming.append(prog)
        elif start and start <= now < stop:
            upcoming.insert(0, prog)
            current_title = prog['title']
            current_desc = prog.get('desc', '')
    upcoming = upcoming[:8]
    if current_title:
        if current_desc:
            clean_desc = current_desc.replace('\n', ' ').replace('\r', ' ').strip()
            if len(clean_desc) > 150:
                clean_desc = clean_desc[:147] + '...'
            display_name = f"[B]{channel_name}[/B]  [COLOR yellow]{current_title}[/COLOR]: [COLOR gray]{clean_desc}[/COLOR]"
        else:
            display_name = f"[B]{channel_name}[/B]  [COLOR yellow]{current_title}[/COLOR]"
    else:
        display_name = f"[B]{channel_name}[/B]"
    plot_lines = []
    for idx, prog in enumerate(upcoming):
        t = _parse_xmltv_time(prog['start'])
        stop_t = _parse_xmltv_time(prog['stop'])
        is_current = (idx == 0 and t and stop_t and t <= now < stop_t)
        if t:
            time_str = time.strftime('%H:%M', time.localtime(t))
            line = f"{time_str} - {prog['title']}"
        else:
            line = prog['title']
        if is_current:
            line = f"[B][COLOR yellow]> {line}[/COLOR][/B]"
        plot_lines.append(line)
    plot = '\n'.join(plot_lines)
    _log(f'EPG info for {channel_name}: found {len(upcoming)} programs, current={current_title}')
    return plot, display_name, current_title


def backup_settings():
    import zipfile
    profile_path = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    backup_path = os.path.join(profile_path, 'xtream_m3u_addon_backup.zip')
    try:
        with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            settings_file = os.path.join(profile_path, 'settings.xml')
            if os.path.exists(settings_file):
                zf.write(settings_file, 'settings.xml')
            fav_file = os.path.join(profile_path, 'favorites.json')
            if os.path.exists(fav_file):
                zf.write(fav_file, 'favorites.json')
            for fname in os.listdir(profile_path):
                if fname.startswith('epg_cache_profile_') or fname.startswith('refresh_'):
                    fpath = os.path.join(profile_path, fname)
                    zf.write(fpath, fname)
        xbmcgui.Dialog().notification('XStream Player', 'Backup saved')
    except Exception as e:
        _log(f'Backup failed: {e}')
        xbmcgui.Dialog().notification('XStream Player', 'Backup failed')


def restore_settings():
    import zipfile
    dialog = xbmcgui.Dialog()
    zip_path = dialog.browse(1, 'Select backup ZIP', 'files', '.zip', False, False, '')
    if not zip_path:
        return
    profile_path = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for name in zf.namelist():
                if '..' in name or name.startswith('/') or name.startswith('\\'):
                    _log(f'Skipped unsafe backup entry: {name}')
                    continue
                zf.extract(name, profile_path)
        xbmcgui.Dialog().notification('XStream Player', 'Restore complete')
    except Exception as e:
        _log(f'Restore failed: {e}')
        xbmcgui.Dialog().notification('XStream Player', 'Restore failed')


def clear_all_caches():
    count = _cache_clear_all()
    profile_path = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    for fname in os.listdir(profile_path):
        if fname.startswith('epg_cache_profile_') or fname == 'epg_cache.json' or fname == 'view_prefs.json':
            try:
                os.remove(os.path.join(profile_path, fname))
                count += 1
            except Exception:
                pass
    xbmcgui.Dialog().notification('XStream Player', f'Cleared {count} cache files')


def clear_epg_cache():
    profile_path = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    count = 0
    for fname in os.listdir(profile_path):
        if fname.startswith('epg_cache_profile_') or fname == 'epg_cache.json' or fname.startswith('data_cache_epg'):
            try:
                os.remove(os.path.join(profile_path, fname))
                count += 1
            except Exception:
                pass
    xbmcgui.Dialog().notification('XStream Player', f'EPG cache cleared ({count} files)')


def clear_channel_cache():
    profile_path = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    count = 0
    for fname in os.listdir(profile_path):
        if (fname.startswith('data_cache_xtream_') or fname.startswith('data_cache_m3u')
                or fname.startswith('vod_info_')):
            try:
                os.remove(os.path.join(profile_path, fname))
                count += 1
            except Exception:
                pass
    xbmcgui.Dialog().notification('XStream Player', f'Channel cache cleared ({count} files)')


def clear_tmdb_cache():
    profile_path = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    count = 0
    for fname in os.listdir(profile_path):
        if fname.startswith('data_cache_tmdb_') or fname.startswith('tmdb_'):
            try:
                os.remove(os.path.join(profile_path, fname))
                count += 1
            except Exception:
                pass
    xbmcgui.Dialog().notification('XStream Player', f'TMDB cache cleared ({count} files)')


mode = args.get('mode', [None])[0]

if mode is None:
    main_menu()
elif mode == 'live_menu':
    live_menu()
elif mode == 'm3u_live':
    m3u_live()
elif mode == 'm3u_group':
    m3u_group(args.get('group', [''])[0])
elif mode == 'xtream_categories':
    xtream_categories(args.get('type', ['live'])[0])
elif mode == 'xtream_streams':
    try:
        page = int(args.get('page', ['1'])[0])
    except ValueError:
        page = 1
    xtream_streams(args.get('type', ['live'])[0], args.get('cat_id', [''])[0], page)
elif mode == 'xtream_series':
    xtream_series(args.get('series_id', [''])[0])
elif mode == 'xtream_season':
    xtream_season(args.get('series_id', [''])[0], args.get('season_num', [''])[0])
elif mode == 'movies_menu':
    movies_menu()
elif mode == 'series_menu':
    series_menu()
elif mode == 'replay_menu':
    replay_menu()
elif mode == 'replay_channel':
    replay_channel(args.get('stream_id', [''])[0], args.get('epg_id', [''])[0], args.get('name', [''])[0])
elif mode == 'replay_play':
    replay_play(args.get('stream_id', [''])[0], args.get('start', [''])[0], args.get('duration', [''])[0])
elif mode == 'search_global':
    search_global(args.get('query', [None])[0])
elif mode == 'search_m3u':
    search_m3u(args.get('query', [None])[0])
elif mode == 'search_xtream':
    search_xtream(args.get('type', ['live'])[0], args.get('query', [None])[0])
elif mode == 'history_menu':
    history_menu()
elif mode == 'history_remove':
    watch_history.remove(args.get('name', [''])[0], args.get('stype', [None])[0])
    xbmcgui.Dialog().notification('XStream Player', 'Removed from history')
    xbmc.executebuiltin('Container.Refresh')
elif mode == 'history_clear':
    watch_history.clear()
    xbmcgui.Dialog().notification('XStream Player', 'History cleared')
    xbmc.executebuiltin('Container.Refresh')
elif mode == 'favorites_menu':
    favorites_menu(args.get('stype', [None])[0], args.get('folder', [None])[0])
elif mode == 'fav_new_folder':
    name = xbmcgui.Dialog().input('New Folder Name')
    if name:
        fav.create_folder(name)
        xbmcgui.Dialog().notification('XStream Player', f'Folder "{name}" created')
        xbmc.executebuiltin('Container.Refresh')
elif mode == 'fav_delete_folder':
    fname = args.get('folder', [''])[0]
    if fname and fname != 'Favorites':
        if xbmcgui.Dialog().yesno('Delete Folder', f'Delete folder "{fname}" and all its items?'):
            fav.delete_folder(fname)
            xbmcgui.Dialog().notification('XStream Player', f'Folder "{fname}" deleted')
            xbmc.executebuiltin('Container.Refresh')
elif mode == 'fav_move':
    item_id = args.get('id', [''])[0]
    from_folder = args.get('from_folder', ['Favorites'])[0]
    folders = [f for f in fav.get_folders() if f != from_folder]
    if not folders:
        xbmcgui.Dialog().notification('XStream Player', 'No other folders to move to')
    else:
        idx = xbmcgui.Dialog().select('Move to Folder', folders)
        if idx >= 0:
            # Find the item in the source folder
            source_items = fav.get_all(from_folder)
            item_data = next((i for i in source_items if i.get('id') == item_id), None)
            if item_data:
                fav.remove(item_id, from_folder)
                fav.add(item_data, folders[idx])
                xbmcgui.Dialog().notification('XStream Player', f'Moved to {folders[idx]}')
                xbmc.executebuiltin('Container.Refresh')
elif mode == 'export_favorites':
    fname = args.get('folder', [None])[0]
    path = xbmcgui.Dialog().browseSingle(3, 'Export M3U - Select folder', 'files')
    if path:
        export_name = f'favorites_{fname}.m3u' if fname else 'favorites.m3u'
        full_path = os.path.join(path, export_name)
        count = fav.export_m3u(full_path, fname)
        xbmcgui.Dialog().notification('XStream Player', f'Exported {count} items to M3U')
elif mode == 'toggle_fav':
    toggle_favorite(
        args.get('id', [''])[0],
        args.get('name', [''])[0],
        args.get('stype', ['live'])[0],
        args.get('icon', [''])[0],
        args.get('url', [''])[0],
        args.get('epg_id', [''])[0]
    )
elif mode == 'refresh_data':
    refresh_data()
elif mode == 'sync_pvr':
    sync_pvr()
elif mode == 'open_pvr':
    open_pvr()
elif mode == 'open_pvr_guide':
    open_pvr_guide()
elif mode == 'tools_menu':
    tools_menu()

elif mode == 'play_stream':
    play_stream(args.get('url', [''])[0], args.get('name', [''])[0], args.get('title', [''])[0], args.get('plot', [''])[0], args.get('icon', [''])[0], args.get('stype', ['live'])[0],
                args.get('series_id', [''])[0], args.get('season_num', [''])[0], args.get('ep_id', [''])[0])
elif mode == 'toggle_watched':
    s_id = args.get('series_id', [''])[0]
    s_num = args.get('season_num', [''])[0]
    e_id = args.get('ep_id', [''])[0]
    action = args.get('action', ['watched'])[0]
    if action == 'watched':
        watched_db.mark_watched(s_id, s_num, e_id)
        xbmcgui.Dialog().notification('XStream Player', 'Marked as watched')
    else:
        watched_db.mark_unwatched(s_id, s_num, e_id)
        xbmcgui.Dialog().notification('XStream Player', 'Marked as unwatched')
    xbmc.executebuiltin('Container.Refresh')
elif mode == 'settings':
    settings()
elif mode == 'switch_profile':
    switch_profile()
elif mode == 'test_connection':
    test_connection()
elif mode == 'account_info':
    account_info()
elif mode == 'toggle_setting':
    key = args.get('key', [''])[0]
    if key:
        current = addon.getSetting(key).lower() == 'true'
        addon.setSetting(key, 'false' if current else 'true')
        xbmcgui.Dialog().notification('XStream Player', f'{key.replace("_", " ").title()}: {"OFF" if current else "ON"}')
        xbmc.executebuiltin('Container.Refresh')
elif mode == 'manage_visible_cats':
    manage_visible_cats()
elif mode == 'backup_settings':
    backup_settings()
elif mode == 'restore_settings':
    restore_settings()
elif mode == 'clear_all_caches':
    clear_all_caches()
elif mode == 'clear_epg_cache':
    clear_epg_cache()
elif mode == 'clear_channel_cache':
    clear_channel_cache()
elif mode == 'clear_tmdb_cache':
    clear_tmdb_cache()
