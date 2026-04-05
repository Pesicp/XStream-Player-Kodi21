# -*- coding: utf-8 -*-
import requests
import xbmc


class TMDB:
    BASE = 'https://api.themoviedb.org/3'

    def __init__(self, api_key):
        self.api_key = api_key

    def search_movie(self, title):
        if not self.api_key or not title:
            return None
        url = f"{self.BASE}/search/movie"
        params = {'api_key': self.api_key, 'query': title, 'language': 'en-US', 'page': 1}
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            results = data.get('results', [])
            return results[0] if results else None
        except Exception as e:
            xbmc.log(f'[XStream Player] TMDB error: {e}', xbmc.LOGWARNING)
            return None

    def get_movie_details(self, tmdb_id):
        if not self.api_key or not tmdb_id:
            return None
        url = f"{self.BASE}/movie/{tmdb_id}"
        params = {'api_key': self.api_key, 'language': 'en-US'}
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            xbmc.log(f'[XStream Player] TMDB error: {e}', xbmc.LOGWARNING)
            return None

    def enrich(self, title):
        result = {'plot': '', 'poster_url': '', 'rating': '', 'year': ''}
        if not self.api_key or not title:
            return result
        search = self.search_movie(title)
        if not search:
            return result
        tmdb_id = search.get('id')
        details = self.get_movie_details(tmdb_id) if tmdb_id else None
        src = details or search
        result['plot'] = src.get('overview') or ''
        result['rating'] = str(src.get('vote_average', ''))
        result['year'] = str(src.get('release_date', '')[:4]) if src.get('release_date') else ''
        poster = src.get('poster_path') or search.get('poster_path')
        if poster:
            result['poster_url'] = f"https://image.tmdb.org/t/p/w500{poster}"
        return result
