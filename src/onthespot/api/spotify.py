import json
import os
import re
import requests
import threading
import time
import traceback
import uuid
from librespot.audio.decoders import AudioQuality
from librespot.core import Session
from librespot.zeroconf import ZeroconfServer
from PyQt6.QtCore import QObject
from ..otsconfig import config, cache_dir
from ..runtimedata import get_logger, account_pool, pending, download_queue, pending_lock
from ..utils import make_call, conv_list_format

logger = get_logger("api.spotify")
BASE_URL = "https://api.spotify.com/v1"


class SpotifyRateLimitError(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f"Spotify rate limit exceeded, retry after {retry_after}s")


class MirrorSpotifyPlayback(QObject):
    def __init__(self):
        super().__init__()
        self.thread = None
        self.is_running = False

    def start(self):
        if self.thread is None:
            logger.info('Starting SpotifyMirrorPlayback')
            self.is_running = True
            self.thread = threading.Thread(target=self.run)
            self.thread.start()
        else:
            logger.warning('SpotifyMirrorPlayback is already running.')

    def stop(self):
        if self.thread is not None:
            logger.info('Stopping SpotifyMirrorPlayback')
            self.is_running = False
            self.thread.join()
            self.thread = None
        else:
            logger.warning('SpotifyMirrorPlayback is not running.')

    def run(self):
        # Circular Import
        from ..accounts import get_account_token
        poll_interval = 30
        while self.is_running:
            time.sleep(poll_interval)
            try:
                token = get_account_token('spotify').tokens()
            except (AttributeError, IndexError):
                # Account pool hasn't been filled yet
                continue
            url = f"{BASE_URL}/me/player/currently-playing"
            try:
                resp = requests.get(url, headers={"Authorization": f"Bearer {token.get('user-read-currently-playing')}"})
            except:
                logger.info("Session Expired, reinitializing...")
                parsing_index = config.get('active_account_number')
                spotify_re_init_session(account_pool[parsing_index])
                token = account_pool[parsing_index]['login']['session']
                continue
            if resp.status_code == 429:
                retry_after = int(resp.headers.get('Retry-After', 300))
                # Enforce minimum 5-minute backoff to avoid Spotify's escalating ban
                backoff = max(retry_after, 300)
                logger.warning(f"MirrorSpotifyPlayback rate limited, backing off for {backoff}s")
                poll_interval = backoff
                continue
            poll_interval = 30
            if resp.status_code == 200:
                data = resp.json()
                if data['currently_playing_type'] == 'track':
                    item_id = data['item']['id']
                    if item_id not in pending and item_id not in download_queue:
                        parent_category = 'track'
                        playlist_name = ''
                        playlist_by = ''
                        if data['context'] is not None:
                            if data['context'].get('type') == 'playlist':
                                match = re.search(r'spotify:playlist:(\w+)', data['context']['uri'])
                                if match:
                                    playlist_id = match.group(1)
                                else:
                                    continue
                                token = get_account_token('spotify')
                                playlist_name, playlist_by = spotify_get_playlist_data(token, playlist_id)
                                parent_category = 'playlist'
                            elif data['context'].get('type') == 'collection':
                                playlist_name = 'Liked Songs'
                                playlist_by = 'me'
                                parent_category = 'playlist'
                            elif data['context'].get('type') in ('album', 'artist'):
                                parent_category = 'album'
                        # Use item id to prevent duplicates
                        #local_id = format_local_id(item_id)
                        with pending_lock:
                            pending[item_id] = {
                                'local_id': item_id,
                                'item_service': 'spotify',
                                'item_type': 'track',
                                'item_id': item_id,
                                'parent_category': parent_category,
                                'playlist_name': playlist_name,
                                'playlist_by': playlist_by,
                                'playlist_number': '?'
                            }
                        logger.info(f'Mirror Spotify Playback added track to download queue: https://open.spotify.com/track/{item_id}')
                        continue
                else:
                    logger.info('Spotify API does not return enough data to parse currently playing episodes.')
                    continue
            else:
                continue


def spotify_new_session():
    os.makedirs(os.path.join(cache_dir(), 'sessions'), exist_ok=True)

    uuid_uniq = str(uuid.uuid4())
    session_json_path = os.path.join(os.path.join(cache_dir(), 'sessions'),
                 f"ots_login_{uuid_uniq}.json")

    CLIENT_ID: str = "65b708073fc0480ea92a077233ca87bd"
    ZeroconfServer._ZeroconfServer__default_get_info_fields['clientID'] = CLIENT_ID
    zs_builder = ZeroconfServer.Builder()
    zs_builder.device_name = 'OnTheSpot'
    zs_builder.conf.stored_credentials_file = session_json_path
    zs = zs_builder.create()
    logger.info("Zeroconf login service started")

    while True:
        time.sleep(1)
        if zs.has_valid_session():
            logger.info(f"Grabbed {zs._ZeroconfServer__session} for {zs._ZeroconfServer__session.username()}")
            if zs._ZeroconfServer__session.username() in config.get('accounts'):
                logger.info("Account already exists")
                return False
            else:
                # I wish there was a way to get credentials without saving to
                # a file and parsing it but not currently sure how.
                try:
                    with open(session_json_path, 'r') as file:
                        zeroconf_login = json.load(file)
                except FileNotFoundError as e:
                    logger.error(f"Error: {str(e)} The file {session_json_path} was not found.\nTraceback: {traceback.format_exc()}")
                except json.JSONDecodeError as e:
                    logger.error(f"Error: {str(e)} Failed to decode JSON from the file.\nTraceback: {traceback.format_exc()}")
                except Exception as e:
                    logger.error(f"Unknown Error: {str(e)}\nTraceback: {traceback.format_exc()}")
                cfg_copy = config.get('accounts').copy()
                new_user = {
                    "uuid": uuid_uniq,
                    "service": "spotify",
                    "active": True,
                    "login": {
                        "username": zeroconf_login["username"],
                        "credentials": zeroconf_login["credentials"],
                        "type": zeroconf_login["type"],
                    }
                }
                zs.close()
                cfg_copy.append(new_user)
                config.set('accounts', cfg_copy)
                config.save()
                logger.info("New account added to config.")
                return True


def spotify_login_user(account):
    try:
        # I'd prefer to use 'Session.Builder().stored(credentials).create but
        # I can't get it to work, loading from credentials file instead.
        uuid = account['uuid']
        username = account['login']['username']

        session_dir = os.path.join(cache_dir(), "sessions")
        os.makedirs(session_dir, exist_ok=True)
        session_json_path = os.path.join(session_dir, f"ots_login_{uuid}.json")
        try:
            with open(session_json_path, 'w') as file:
                json.dump(account['login'], file)
            logger.info(f"Login information for '{username[:4]}*******' written to {session_json_path}")
        except IOError as e:
            logger.error(f"Error writing to file {session_json_path}: {str(e)}\nTraceback: {traceback.format_exc()}")

        config = Session.Configuration.Builder().set_stored_credential_file(session_json_path).build()
        # For some reason initialising session as None prevents premature application exit
        session = None
        try:
            session = Session.Builder(conf=config).stored_file(session_json_path).create()
        except Exception:
            time.sleep(3)
            session = Session.Builder(conf=config).stored_file(session_json_path).create()
        logger.debug("Session created")
        logger.info(f"Login successful for user '{username[:4]}*******'")
        account_type = session.get_user_attribute("type")
        bitrate = "160k"
        if account_type == "premium":
            bitrate = "320k"
        account_pool.append({
            "uuid": uuid,
            "username": username,
            "service": "spotify",
            "status": "active",
            "account_type": account_type,
            "bitrate": bitrate,
            "login": {
                "session": session,
                "session_path": session_json_path,
            }
        })
        return True
    except Exception as e:
        logger.error(f"Unknown Exception: {str(e)}\nTraceback: {traceback.format_exc()}")
        account_pool.append({
            "uuid": uuid,
            "username": username,
            "service": "spotify",
            "status": "error",
            "account_type": "N/A",
            "bitrate": "N/A",
            "login": {
                "session": "",
                "session_path": "",
            }
        })
        return False


def spotify_re_init_session(account):
    session_json_path = os.path.join(cache_dir(), "sessions", f"ots_login_{account['uuid']}.json")
    try:
        config = Session.Configuration.Builder().set_stored_credential_file(session_json_path).build()
        logger.debug("Session config created")
        session = Session.Builder(conf=config).stored_file(session_json_path).create()
        logger.debug("Session re init done")
        account['login']['session_path'] = session_json_path
        account['login']['session'] = session
        account['status'] = 'active'
        account['account_type'] = session.get_user_attribute("type")
        bitrate = "160k"
        account_type = session.get_user_attribute("type")
        if account_type == "premium":
            bitrate = "320k"
        account['bitrate'] = bitrate
    except:
        logger.error('Failed to re init session !')


def spotify_get_token(parsing_index):
    try:
        token = account_pool[parsing_index]['login']['session']
    except (OSError, AttributeError):
        logger.info(f'Failed to retreive token for {account_pool[parsing_index]["username"]}, attempting to reinit session.')
        spotify_re_init_session(account_pool[parsing_index])
        token = account_pool[parsing_index]['login']['session']
    return token


def spotify_get_artist_album_ids(token, artist_id):
    logger.info(f"Getting album ids for artist: '{artist_id}'")
    items = []
    offset = 0
    limit = 50
    while True:
        headers = {}
        headers['Authorization'] = f"Bearer {token.tokens().get('user-read-email')}"

        url = f'{BASE_URL}/artists/{artist_id}/albums?include_groups=album%2Csingle&limit={limit}&offset={offset}' #%2Cappears_on%2Ccompilation
        artist_data = make_call(url, headers=headers)

        offset += limit
        items.extend(artist_data['items'])

        if artist_data['total'] <= offset:
            break

    item_ids = []
    for album in items:
        item_ids.append(album['id'])
    return item_ids


def spotify_get_playlist_data(token, playlist_id):
    logger.info(f"Get playlist data for playlist: {playlist_id}")
    from librespot.proto import Playlist4External_pb2
    from ..utils import RateLimitedError

    headers = {
        'Authorization': f"Bearer {token.tokens().get('user-read-email')}",
        'app-platform': 'WebPlayer',
    }
    resp = requests.get(
        f"https://spclient.wg.spotify.com/playlist/v2/playlist/{playlist_id}",
        headers=headers
    )
    if resp.status_code == 429:
        raise RateLimitedError(retry_after=int(resp.headers.get('Retry-After', 30)), url=resp.url)
    if resp.status_code != 200:
        logger.error(f"spclient playlist data returned status {resp.status_code}")
        return '', ''

    contents = Playlist4External_pb2.SelectedListContent()
    contents.ParseFromString(resp.content)

    name = contents.attributes.name
    owner = contents.owner_username
    return name, owner


def spotify_get_lyrics(token, item_id, item_type, metadata, filepath):
    if config.get('download_lyrics'):
        lyrics = []
        try:
            if item_type == "track":
                url = f'https://spclient.wg.spotify.com/color-lyrics/v2/track/{item_id}?format=json&market=from_token'
            elif item_type == "episode":
                url = f"https://spclient.wg.spotify.com/transcript-read-along/v2/episode/{item_id}?format=json&market=from_token"

            headers = {}
            headers['app-platform'] = 'WebPlayer'
            headers['Authorization'] = f'Bearer {token.tokens().get("user-read-email")}'
            headers['user-agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/77.0.3865.90 Safari/537.36'

            resp = make_call(url, headers=headers)
            if resp == None:
                logger.info(f"Failed to find lyrics for {item_type}: {item_id}")
                return None

            if not config.get('only_download_plain_lyrics'):
                if config.get("embed_branding"):
                    lyrics.append('[re:OnTheSpot]')

                for key in metadata.keys():
                    value = metadata[key]
                    if key in ['title', 'track_title', 'tracktitle'] and config.get("embed_name"):
                        title = value
                        lyrics.append(f'[ti:{title}]')
                    elif key == 'artists' and config.get("embed_artist"):
                        artist = value
                        lyrics.append(f'[ar:{artist}]')
                    elif key in ['album_name', 'album'] and config.get("embed_album"):
                        album = value
                        lyrics.append(f'[al:{album}]')
                    elif key in ['writers'] and config.get("embed_writers"):
                        author = value
                        lyrics.append(f'[au:{author}]')

                if item_type == "track":
                    lyrics.append(f'[by:{resp["lyrics"]["provider"]}]')

                if config.get("embed_length"):
                    l_ms = int(metadata['length'])
                    if round((l_ms/1000)/60) < 10:
                        digit="0"
                    else:
                        digit=""
                    lyrics.append(f'[length:{digit}{round((l_ms/1000)/60)}:{round((l_ms/1000)%60)}]\n')

            default_length = len(lyrics)

            if item_type == "track":
                if resp["lyrics"]["syncType"] == "LINE_SYNCED":
                    for line in resp["lyrics"]["lines"]:
                        minutes, seconds = divmod(int(line['startTimeMs']) / 1000, 60)
                        if not config.get('only_download_plain_lyrics'):
                            lyrics.append(f'[{minutes:0>2.0f}:{seconds:05.2f}] {line["words"]}')
                        else:
                            lyrics.append(line["words"])
                elif resp["lyrics"]["syncType"] == "UNSYNCED" and not config.get("only_download_synced_lyrics"):
                    lyrics = [line['words'] for line in resp['lyrics']['lines']]

            elif item_type == "episode":
                if resp["timeSyncedStatus"] == "SYLLABLE_SYNCED":
                    for line in resp["section"]:
                        try:
                            minutes, seconds = divmod(int(line['startMs']) / 1000, 60)
                            lyrics.append(f'[{minutes:0>2.0f}:{seconds:05.2f}] {line["text"]["sentence"]["text"]}')
                        except KeyError as e:
                            logger.debug(f"Invalid line: {str(e)} likely title, skipping..")
                else:
                    logger.info("Unsynced episode lyrics, please open a bug report.")

        except (KeyError, IndexError) as e:
            logger.error(f'KeyError/Index Error. Failed to get lyrics for {item_id}: {str(e)}\nTraceback: {traceback.format_exc()}')

        merged_lyrics = '\n'.join(lyrics)

        if lyrics:
            logger.debug(lyrics)
            if len(lyrics) <= default_length:
                return False
            if config.get('save_lrc_file'):
                with open(filepath + '.lrc', 'w', encoding='utf-8') as f:
                    f.write(merged_lyrics)
            if config.get('embed_lyrics'):
                if item_type == "track":
                    return {"lyrics": merged_lyrics, "language": resp['lyrics']['language']}
                if item_type == "episode":
                    return {"lyrics": merged_lyrics}
            else:
                return True
    else:
        return False


def spotify_get_playlist_items(token, playlist_id):
    logger.info(f"Getting items in playlist: '{playlist_id}'")
    from librespot.proto import Playlist4External_pb2
    from ..utils import RateLimitedError

    headers = {
        'Authorization': f"Bearer {token.tokens().get('user-read-email')}",
        'app-platform': 'WebPlayer',
    }
    resp = requests.get(
        f"https://spclient.wg.spotify.com/playlist/v2/playlist/{playlist_id}",
        headers=headers
    )
    if resp.status_code == 429:
        raise RateLimitedError(retry_after=int(resp.headers.get('Retry-After', 30)), url=resp.url)
    if resp.status_code != 200:
        logger.error(f"spclient playlist returned status {resp.status_code}")
        return []

    contents = Playlist4External_pb2.SelectedListContent()
    contents.ParseFromString(resp.content)

    items = []
    for item in contents.contents.items:
        uri = item.uri
        if not uri:
            continue
        parts = uri.split(':')
        if len(parts) < 3:
            continue
        uri_type = parts[1]
        uri_id = parts[2]
        if uri_type == 'track':
            items.append({'track': {'id': uri_id, 'type': 'track'}})
        elif uri_type == 'episode':
            items.append({'track': {'id': uri_id, 'type': 'episode'}})
    return items


def spotify_get_liked_songs(token):
    logger.info("Getting liked songs")
    items = []
    offset = 0
    limit = 50

    while True:
        url = f'{BASE_URL}/me/tracks?offset={offset}&limit={limit}'
        headers = {}
        headers['Authorization'] = f"Bearer {token.tokens().get('user-library-read')}"

        resp = make_call(url, headers=headers, skip_cache=True)

        offset += limit
        items.extend(resp['items'])

        if resp['total'] <= offset:
            break
    return items


def spotify_get_your_episodes(token):
    logger.info("Getting your episodes")
    items = []
    offset = 0
    limit = 50

    while True:
        headers = {}
        headers['Authorization'] = f"Bearer {token.tokens().get('user-library-read')}"
        url = f'{BASE_URL}/me/episodes?offset={offset}&limit={limit}'

        resp = make_call(url, headers=headers, skip_cache=True)

        offset += limit
        items.extend(resp['items'])

        if resp['total'] <= offset:
            break
    return items


def spotify_get_album_track_ids(token, album_id):
    logger.info(f"Getting tracks from album: {album_id}")
    from librespot.metadata import AlbumId, TrackId
    from librespot.proto import Metadata_pb2
    from ..utils import RateLimitedError

    headers = {
        'Authorization': f"Bearer {token.tokens().get('user-read-email')}",
        'app-platform': 'WebPlayer',
    }
    album_gid = AlbumId.from_base62(album_id).hex_id()
    resp = requests.get(
        f"https://spclient.wg.spotify.com/metadata/4/album/{album_gid}",
        headers=headers
    )
    if resp.status_code == 429:
        raise RateLimitedError(retry_after=int(resp.headers.get('Retry-After', 30)), url=resp.url)
    if resp.status_code != 200:
        logger.error(f"spclient album metadata returned status {resp.status_code}")
        return []

    album = Metadata_pb2.Album()
    album.ParseFromString(resp.content)

    item_ids = []
    for disc in album.disc:
        for track in disc.track:
            track_id = TrackId.from_hex(track.gid.hex()).to_spotify_uri().split(':')[-1]
            item_ids.append(track_id)
    return item_ids


def spotify_get_search_results(token, search_term, content_types):
    logger.info(f"Get search result for term '{search_term}'")

    from urllib.parse import quote
    from hashlib import md5

    headers = {
        'Authorization': f"Bearer {token.tokens().get('user-read-email')}",
        'app-platform': 'WebPlayer',
    }

    limit = config.get("max_search_results")
    encoded_query = quote(search_term)
    search_url = (
        f"https://spclient.wg.spotify.com/searchview/km/v4/search/{encoded_query}"
        f"?limit={limit}&imageSize=default&catalogue=&country=&locale=en"
        f"&platform=zelda&entity-version=v2"
    )

    cache_key = md5(search_url.encode()).hexdigest()
    cache_file = os.path.join(config.get('_cache_dir'), 'reqcache', cache_key + '.json')
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)

    if os.path.isfile(cache_file):
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        response = requests.get(search_url, headers=headers)
        if response.status_code == 429:
            raise SpotifyRateLimitError(int(response.headers.get('Retry-After', 30)))
        if response.status_code != 200:
            logger.error(f"spclient search returned status {response.status_code}: {response.text[:500]}")
            return []
        data = response.json()
        logger.debug(f"spclient search raw response keys: {list(data.keys())}")
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    results_data = data.get('results', {})
    if not results_data:
        logger.error(f"spclient search returned no 'results' key. Top-level keys: {list(data.keys())}")
        return []

    # Map spclient section names → item_type strings
    section_type_map = {
        'tracks': 'track',
        'albums': 'album',
        'artists': 'artist',
        'playlists': 'playlist',
        'podcasts': 'podcast',
        'episodes': 'podcast_episode',
    }

    def _extract_id(uri):
        return uri.split(':')[-1] if uri else ''

    search_results = []
    for section_key, item_type in section_type_map.items():
        hits = results_data.get(section_key, {}).get('hits', [])
        for item in hits:
            uri = item.get('uri', '')
            item_id = _extract_id(uri)
            if not item_id:
                continue

            # spclient returns image as a plain CDN URL string at the top level
            item_thumbnail_url = item.get('image', '')

            try:
                if item_type == 'track':
                    item_name = f"{config.get('explicit_label') if item.get('explicit') else ''} {item['name']}".strip()
                    item_by = config.get('metadata_separator').join(a['name'] for a in item.get('artists', []))
                elif item_type == 'album':
                    year = item.get('year', '')
                    track_count = item.get('trackCount', '?')
                    item_name = f"[Y:{year}] [T:{track_count}] {item['name']}"
                    item_by = config.get('metadata_separator').join(a['name'] for a in item.get('artists', []))
                elif item_type == 'playlist':
                    item_name = item['name']
                    item_by = item.get('owner', {}).get('name', '')
                elif item_type == 'artist':
                    genres = '/'.join(item.get('genres', []))
                    item_name = item['name'] + (f"  |  GENRES: {genres}" if genres else '')
                    item_by = item['name']
                elif item_type in ('podcast', 'podcast_episode'):
                    item_name = f"{config.get('explicit_label') if item.get('explicit') else ''} {item['name']}".strip()
                    item_by = item.get('publisher', item.get('show', {}).get('name', ''))
                else:
                    continue
            except (KeyError, TypeError) as e:
                logger.warning(f"spclient search: skipping malformed {item_type} hit ({e}): {item}")
                continue

            search_results.append({
                'item_id': item_id,
                'item_name': item_name,
                'item_by': item_by,
                'item_type': item_type,
                'item_service': 'spotify',
                'item_url': f"https://open.spotify.com/{item_type.replace('_', '-')}/{item_id}",
                'item_thumbnail_url': item_thumbnail_url,
            })
    return search_results


def spotify_get_track_metadata(token, item_id):
    from librespot.metadata import TrackId
    from librespot.proto import Metadata_pb2
    from ..utils import RateLimitedError

    headers = {
        'Authorization': f"Bearer {token.tokens().get('user-read-email')}",
        'app-platform': 'WebPlayer',
    }

    track_gid = TrackId.from_base62(item_id).hex_id()
    resp = requests.get(
        f"https://spclient.wg.spotify.com/metadata/4/track/{track_gid}",
        headers=headers
    )
    if resp.status_code == 429:
        retry_after = int(resp.headers.get('Retry-After', 30))
        raise RateLimitedError(retry_after=retry_after, url=resp.url)
    if resp.status_code != 200:
        logger.error(f"spclient track metadata returned {resp.status_code}: {resp.text[:200]}")
        return None

    track = Metadata_pb2.Track()
    track.ParseFromString(resp.content)

    def file_id_to_url(fid):
        return f"https://i.scdn.co/image/{fid.hex()}" if fid else ''

    # Best quality cover image
    image_url = ''
    covers = list(track.album.cover_group.image) or list(track.album.cover)
    if covers:
        image_url = file_id_to_url(sorted(covers, key=lambda i: i.size, reverse=True)[0].file_id)

    # ISRC
    isrc = next((e.id for e in track.external_id if e.type == 'isrc'), '')

    # Album type
    album_type = {1: 'album', 2: 'single', 3: 'compilation', 4: 'ep'}.get(track.album.type, 'album')

    # Total tracks/discs from disc structure
    total_tracks = sum(len(d.track) for d in track.album.disc) or 1
    total_discs = max((d.number for d in track.album.disc), default=1)

    info = {
        'artists': conv_list_format([a.name for a in track.artist]),
        'album_name': track.album.name,
        'album_type': album_type,
        'album_artists': track.album.artist[0].name if track.album.artist else '',
        'title': track.name,
        'image_url': image_url,
        'release_year': str(track.album.date.year) if track.album.date.year else '',
        'track_number': track.number,
        'total_tracks': total_tracks,
        'disc_number': track.disc_number,
        'total_discs': total_discs,
        'genre': conv_list_format(list(track.album.genre)),
        'label': track.album.label,
        'copyright': conv_list_format([c.text for c in track.album.copyright]),
        'explicit': track.explicit,
        'isrc': isrc,
        'length': str(track.duration),
        'item_url': f"https://open.spotify.com/track/{item_id}",
        'item_id': item_id,
        'is_playable': True,
    }

    try:
        credits_data = make_call(
            f'https://spclient.wg.spotify.com/track-credits-view/v0/experimental/{item_id}/credits',
            headers=headers
        )
        if credits_data:
            credits = {}
            for block in credits_data.get('roleCredits', []):
                role = block.get('roleTitle', '').lower()
                credits[role] = [a.get('name') for a in block.get('artists', [])]
            info['performers'] = conv_list_format([x for x in credits.get('performers', []) if isinstance(x, str)])
            info['producers'] = conv_list_format([x for x in credits.get('producers', []) if isinstance(x, str)])
            info['writers'] = conv_list_format([x for x in credits.get('writers', []) if isinstance(x, str)])
    except Exception:
        pass

    return info


def spotify_get_podcast_episode_metadata(token, episode_id):
    logger.info(f"Get episode info for episode by id '{episode_id}'")
    headers = {}
    headers['Authorization'] = f"Bearer {token.tokens().get('user-read-email')}"
    episode_data = make_call(f"{BASE_URL}/episodes/{episode_id}", headers=headers)
    show_episode_ids = spotify_get_podcast_episode_ids(token, episode_data.get('show', {}).get('id'))
    # I believe audiobook ids start with a 7 but to verify you can use https://api.spotify.com/v1/audiobooks/{id}
    # the endpoint could possibly be used to mark audiobooks in genre but it doesn't really provide any additional
    # metadata compared to show_data beyond abridged and unabridged.

    track_number = ''
    for index, episode in enumerate(show_episode_ids):
        if episode == episode_id:
            track_number = index + 1
            break

    copyrights = []
    for copyright in episode_data.get('show', {}).get('copyrights', []):
        text = copyright.get('text')
        copyrights.append(text)

    info = {}
    info['album_name'] = episode_data.get('show', {}).get('name')
    info['title'] = episode_data.get('name')
    info['image_url'] = episode_data.get('images', [{}])[0].get('url')
    info['release_year'] = episode_data.get('release_date').split('-')[0]
    info['track_number'] = track_number
    # Not accurate
    #info['total_tracks'] = episode_data.get('show', {}).get('total_episodes', 0)
    info['total_tracks'] = len(show_episode_ids)
    info['artists'] = conv_list_format([episode_data.get('show', {}).get('publisher')])
    info['album_artists'] = conv_list_format([episode_data.get('show', {}).get('publisher')])
    info['language'] = conv_list_format(episode_data.get('languages', []))
    description = episode_data.get('description')
    info['description'] = str(description if description else episode_data.get('show', {}).get('description', ""))
    info['copyright'] = conv_list_format(copyrights)
    info['length'] = str(episode_data.get('duration_ms'))
    info['explicit'] = episode_data.get('explicit')
    info['is_playable'] = episode_data.get('is_playable')
    info['item_url'] = episode_data.get('external_urls', {}).get('spotify')
    info['item_id'] = episode_data.get('id')

    return info


def spotify_get_podcast_episode_ids(token, show_id):
    logger.info(f"Getting show episodes: {show_id}'")
    episodes = []
    offset = 0
    limit = 50

    while True:
        url = f'{BASE_URL}/shows/{show_id}/episodes?offset={offset}&limit={limit}'
        headers = {}
        headers['Authorization'] = f"Bearer {token.tokens().get('user-read-email')}"
        resp = make_call(url, headers=headers)

        offset += limit
        episodes.extend(resp['items'])

        if resp['total'] <= offset:
            break

    item_ids = []
    for episode in episodes:
        if episode:
            item_ids.append(episode['id'])
    return item_ids
