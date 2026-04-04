# -*- coding: utf-8 -*-
import time as _time
import requests
import unicodedata
import urllib.parse
import xbmc
import xbmcaddon

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
MAX_RETRIES = 3
RETRY_DELAYS = [1, 3, 5]


def _log(msg):
    xbmc.log(f'[XStream Player] {msg}', xbmc.LOGINFO)


def _notify(msg):
    import xbmcgui
    xbmcgui.Dialog().notification('XStream Player', msg, xbmcgui.NOTIFICATION_ERROR, 3000)


def _request_with_retry(url, params=None, headers=None, timeout=15):
    """Make an HTTP GET request with retry and exponential backoff."""
    if headers is None:
        headers = _get_headers()
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.exceptions.ConnectionError as e:
            last_error = e
            _log(f'Connection error (attempt {attempt+1}/{MAX_RETRIES}): {e}')
        except requests.exceptions.Timeout as e:
            last_error = e
            _log(f'Timeout (attempt {attempt+1}/{MAX_RETRIES}): {e}')
        except requests.exceptions.HTTPError as e:
            # Don't retry on 4xx errors (auth failures, not found, etc.)
            if e.response is not None and 400 <= e.response.status_code < 500:
                _log(f'HTTP {e.response.status_code}: {e}')
                raise
            last_error = e
            _log(f'HTTP error (attempt {attempt+1}/{MAX_RETRIES}): {e}')
        except Exception as e:
            last_error = e
            _log(f'Request error (attempt {attempt+1}/{MAX_RETRIES}): {e}')
        if attempt < MAX_RETRIES - 1:
            delay = RETRY_DELAYS[attempt]
            _log(f'Retrying in {delay}s...')
            _time.sleep(delay)
    _log(f'All {MAX_RETRIES} attempts failed for {url}')
    if last_error:
        raise last_error
    raise requests.exceptions.ConnectionError(f'Failed after {MAX_RETRIES} attempts')


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


def _clean_base_url(url):
    if not url:
        return url
    url = url.rstrip('/')
    if url.endswith('/player_api.php'):
        url = url[:-len('/player_api.php')]
    return url.rstrip('/')


class IPTV:
    @staticmethod
    def get_m3u_channels(m3u_url):
        channels = []
        if not m3u_url:
            return channels
        try:
            resp = requests.get(m3u_url, headers=_get_headers(), timeout=_get_timeout())
            resp.raise_for_status()
            lines = resp.text.splitlines()
        except Exception as e:
            _log(f'M3U fetch error: {e}')
            _notify('Failed to load M3U playlist')
            return channels

        current = {}
        for line in lines:
            line = line.strip()
            if line.startswith('#EXTINF:'):
                current = {
                    'name': '',
                    'group': 'General',
                    'logo': '',
                    'tvg_id': '',
                    'catchup': '',
                    'catchup_source': '',
                    'catchup_days': ''
                }
                if ',' in line:
                    current['name'] = line.split(',', 1)[1]
                current['name'] = _extract_attr(line, 'tvg-name') or current['name']
                current['logo'] = _extract_attr(line, 'tvg-logo') or ''
                current['group'] = _extract_attr(line, 'group-title') or 'General'
                current['tvg_id'] = _extract_attr(line, 'tvg-id') or ''
                current['catchup'] = _extract_attr(line, 'catchup') or ''
                current['catchup_source'] = _extract_attr(line, 'catchup-source') or ''
                current['catchup_days'] = _extract_attr(line, 'catchup-days') or ''
            elif line and not line.startswith('#') and current:
                current['url'] = line
                channels.append(current)
                current = {}
        return channels

    @staticmethod
    def validate_xtream(base_url, username, password):
        """Validate Xtream credentials. Returns dict with account info or None on failure."""
        url = f"{_clean_base_url(base_url)}/player_api.php"
        try:
            resp = requests.get(
                url, headers=_get_headers(),
                params={'username': username, 'password': password},
                timeout=_get_timeout())
            resp.raise_for_status()
            data = resp.json()
            user_info = data.get('user_info', {})
            server_info = data.get('server_info', {})
            if user_info.get('auth') == 0:
                return None
            return {
                'status': user_info.get('status', 'unknown'),
                'exp_date': user_info.get('exp_date', ''),
                'max_connections': user_info.get('max_connections', ''),
                'active_cons': user_info.get('active_cons', '0'),
                'is_trial': user_info.get('is_trial', '0'),
                'created_at': user_info.get('created_at', ''),
                'server_url': server_info.get('url', ''),
                'timezone': server_info.get('timezone', ''),
            }
        except Exception:
            return None

    @staticmethod
    def get_xtream_categories(base_url, username, password, stype='live'):
        url = f"{_clean_base_url(base_url)}/player_api.php"
        action = {
            'live': 'get_live_categories',
            'movie': 'get_vod_categories',
            'series': 'get_series_categories'
        }.get(stype, 'get_live_categories')
        _log(f'Calling Xtream categories URL: {url}')
        try:
            resp = _request_with_retry(url, params={'username': username, 'password': password, 'action': action}, timeout=_get_timeout())
            data = resp.json()
            _log(f'Xtream categories ({stype}): got {len(data)} items')
            return data or []
        except Exception as e:
            _log(f'Xtream categories error ({stype}): {e}')
            _notify(f'Failed to load categories: {e}')
            return []

    @staticmethod
    def get_xtream_streams(base_url, username, password, stype='live', category_id=None):
        url = f"{_clean_base_url(base_url)}/player_api.php"
        action = {
            'live': 'get_live_streams',
            'movie': 'get_vod_streams',
            'series': 'get_series'
        }.get(stype, 'get_live_streams')
        params = {'username': username, 'password': password, 'action': action}
        if category_id:
            params['category_id'] = category_id
        try:
            resp = _request_with_retry(url, params=params, timeout=_get_timeout())
            data = resp.json()
            _log(f'Xtream streams ({stype}): got {len(data)} items')
            return data or []
        except Exception as e:
            _log(f'Xtream streams error ({stype}): {e}')
            return []

    @staticmethod
    def get_xtream_series_info(base_url, username, password, series_id):
        url = f"{_clean_base_url(base_url)}/player_api.php"
        try:
            resp = _request_with_retry(
                url,
                params={'username': username, 'password': password, 'action': 'get_series_info', 'series_id': series_id},
                timeout=_get_timeout())
            return resp.json() or {}
        except Exception as e:
            _log(f'Xtream series info error: {e}')
            return {}

    @staticmethod
    def get_vod_info(base_url, username, password, vod_id):
        url = f"{_clean_base_url(base_url)}/player_api.php"
        try:
            resp = _request_with_retry(url, params={'username': username, 'password': password, 'action': 'get_vod_info', 'vod_id': vod_id}, timeout=_get_timeout())
            return resp.json() or {}
        except Exception as e:
            _log(f'Xtream vod info error: {e}')
            return {}

    @staticmethod
    def build_xtream_stream_url(base_url, username, password, stream, stype='live'):
        base = _clean_base_url(base_url)
        if stype == 'live':
            sid = stream.get('stream_id')
            ext = 'ts'
            return f"{base}/live/{username}/{password}/{sid}.{ext}"
        elif stype == 'movie':
            sid = stream.get('stream_id')
            container = stream.get('container_extension', 'mp4')
            return f"{base}/movie/{username}/{password}/{sid}.{container}"
        elif stype == 'series':
            sid = stream.get('id')
            container = stream.get('container_extension', 'mp4')
            return f"{base}/series/{username}/{password}/{sid}.{container}"
        return ''

    @staticmethod
    def get_xtream_epg(base_url, username, password, stream_id):
        url = f"{_clean_base_url(base_url)}/player_api.php"
        try:
            resp = requests.get(
                url,
                headers=_get_headers(),
                params={'username': username, 'password': password, 'action': 'get_short_epg', 'stream_id': stream_id},
                timeout=_get_timeout()
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get('epg_listings', [])
        except Exception as e:
            _log(f'Xtream EPG error for stream {stream_id}: {e}')
            return []

    @staticmethod
    def build_catchup_url(base_url, username, password, stream_id, start_timestamp, duration_sec):
        import datetime
        base = _clean_base_url(base_url)
        if len(start_timestamp) == 16:
            start_fmt = start_timestamp
        else:
            # Handle ISO formats like 2023-10-01T12:30:00
            try:
                dt = datetime.datetime.strptime(start_timestamp.replace('T', ' '), '%Y-%m-%d %H:%M:%S')
                start_fmt = dt.strftime('%Y-%m-%d:%H-%M')
            except ValueError:
                start_fmt = start_timestamp
        return (
            f"{base}/streaming/timeshift.php?"
            f"username={urllib.parse.quote(username)}&"
            f"password={urllib.parse.quote(password)}&"
            f"stream={stream_id}&"
            f"start={urllib.parse.quote(start_fmt)}&"
            f"duration={duration_sec}"
        )


def _extract_attr(line, attr):
    try:
        start = line.index(f'{attr}="') + len(attr) + 2
        end = line.index('"', start)
        return line[start:end]
    except ValueError:
        return ''


def _m3u_safe(val):
    if not val:
        return ''
    val = str(val).replace('"', '').replace('\n', ' ').replace('\r', ' ')
    allowed = set(" -_.():&/+'|`")
    val = ''.join(
        c for c in val
        if unicodedata.category(c)[0] in 'LNZP' or c in allowed
    )
    return val.strip()


def build_m3u_content(channels):
    lines = ['#EXTM3U']
    for ch in channels:
        url = _m3u_safe(ch.get('url', ''))
        if not url:
            continue
        name = _m3u_safe(ch.get('name', 'Unknown'))
        tvg_id = _m3u_safe(ch.get('tvg_id')) or name or 'unknown'
        logo = _m3u_safe(ch.get('logo') or ch.get('stream_icon'))
        group = _m3u_safe(ch.get('group')) or 'General'
        catchup = _m3u_safe(ch.get('catchup', ''))
        catchup_source = _m3u_safe(ch.get('catchup_source', ''))
        catchup_days = _m3u_safe(ch.get('catchup_days', ''))
        attrs = [
            f'tvg-id="{tvg_id}"',
            f'tvg-name="{name}"',
        ]
        if logo:
            attrs.append(f'tvg-logo="{logo}"')
        if group:
            attrs.append(f'group-title="{group}"')
        # Only emit catchup attributes if we have a valid source pattern;
        # PVR IPTV Simple Client rejects catchup="default" without catchup-source.
        if catchup and catchup_source:
            attrs.append(f'catchup="{catchup}"')
            attrs.append(f'catchup-source="{catchup_source}"')
            if catchup_days:
                attrs.append(f'catchup-days="{catchup_days}"')
        lines.append(f"#EXTINF:-1 {' '.join(attrs)},{name}")
        lines.append(url)
    return '\n'.join(lines)
