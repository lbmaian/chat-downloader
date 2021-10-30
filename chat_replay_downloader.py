#!/usr/bin/env python3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
import loggingutils
import ioutils
import random
import json
from datetime import datetime, timedelta
import re
import argparse
import csv
import emoji
import time
import os
import numbers
import collections.abc
from http.cookiejar import MozillaCookieJar
import sys
import signal
from urllib import parse
import enum
import textwrap


logging.TRACE, logging.trace, logging.Logger.trace, logging.LoggerAdapter.trace = loggingutils.addLevelName(logging.DEBUG // 2, 'TRACE')


class CallbackFunction(Exception):
    """Raised when the callback function does not have (only) one required positional argument"""
    pass


class VideoNotFound(Exception):
    """Raised when video cannot be found."""
    pass


class ParsingError(Exception):
    """Raised when video data cannot be parsed."""
    pass


class VideoUnavailable(Exception):
    """Raised when video is unavailable (e.g. if video is private)."""
    pass


class NoChatReplay(Exception):
    """Raised when the video does not contain a chat replay."""
    pass


class InvalidURL(Exception):
    """Raised when the url given is invalid (neither YouTube nor Twitch)."""
    pass


class TwitchError(Exception):
    """Raised when an error occurs with a Twitch video."""
    pass


class NoContinuation(Exception):
    """Raised when there are no more messages to retrieve (in a live stream)."""
    pass


class CookieError(Exception):
    """Raised when an error occurs while loading a cookie file."""
    pass


class AbortConditionsSatisfied(Exception):
    """"Raised when all abort conditions are satisfied."""
    pass


# ideally this would be defined as a class variable within SignalAbortType, but that can't be done since SignalAbortType is an enum
DEFAULT_SIGNAL_ABORT_NAMES = [signal_name for signal_name in (
    'SIGINT',
    'SIGBREAK', # Windows-only (ctrl+break)
                # warning: if running multiple background jobs in a console shell, ctrl+break signals all those jobs rather than just the current job
    'SIGQUIT', # Unix-only
    'SIGTERM',
    'SIGABRT',
) if hasattr(signal, signal_name)]
class SignalAbortType(enum.Enum):
    """Determines whether given signal aborts the application."""
    default  = ("Same as 'enable' for signal if it's one of: " + ', '.join(DEFAULT_SIGNAL_ABORT_NAMES) + ".\n"
                "Otherwise, signals are handled as-is (unless overriden, a noop by default).") + ("" if os.name != 'nt' else (
                "\nWindows technical limitations:\n"
                "* SIGINT:default (ctrl+c)\n"
                "  SIGINT only aborts when this application is NOT started in a background job (e.g. via bash '&'),\n"
                "  even if that job is later restored to the foreground.\n"
                "* SIGBREAK:default (Windows-only ctrl+break)\n"
                "  Same as SIGBREAK:enable."))
    disable  =  "Never abort on this signal."
    enable   =  "Always abort on this signal." + ("" if os.name != 'nt' else (
                "\nWindows technical limitations:\n"
                "* SIGINT:enable (ctrl+c)\n"
                "  SIGINT aborts regardless of whether this application is running in the background or foreground job.\n"
                "  Using bash terminology, this also means that ctrl+c aborts all the current session's background and foreground jobs\n"
                "  running this application with SIGINT:enable.\n"
                "* SIGBREAK:enable  (Windows-only ctrl+break)\n"
                "  SIGBREAK also aborts all the current sessions' jobs running this application with either SIGBREAK:enable or SIGBREAK:default."))

class ChatReplayDownloader:
    """A simple tool used to retrieve YouTube/Twitch chat from past broadcasts/VODs. No authentication needed!"""

    DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'

    # since sys.stdout may change (see main() below), using loggingutils.StdoutHandler to always use latest sys.stdout
    logger = loggingutils.loggerProperty(logger_init=lambda self, *_: loggingutils.FormatLoggerAdapter(self, style='{'),
        propagate=False, handlers=[loggingutils.StdoutHandler()],
        lenient=True, format='[%(levelname)s][%(asctime)s]%(context)s %(message)s', datefmt=DATETIME_FORMAT)

    __HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.72 Safari/537.36',
        'Accept-Language': 'en-US, en'
    }

    __YT_HOME = 'https://www.youtube.com'
    __YT_REGEX = re.compile(r'(?:/|%3D|v=|vi=)([0-9A-Za-z-_]{11})(?:[%#?&]|$)')
    __YT_WATCH_TEMPLATE = __YT_HOME + '/watch?v={}'
    __YT_INIT_CONTINUATION_TEMPLATE = __YT_HOME + '/{}?continuation={}'
    __YT_CONTINUATION_TEMPLATE = __YT_HOME + '/youtubei/{}/live_chat/get_{}?key={}'
    __YT_HEARTBEAT_TEMPLATE = __YT_HOME + '/youtubei/{}/player/heartbeat?alt=json&key={}'

    __TWITCH_REGEX = re.compile(r'(?:/videos/|/v/)(\d+)')
    __TWITCH_CLIENT_ID = 'kimne78kx3ncx6brgo4mv6wki5h1ko'  # public client id
    __TWITCH_API_TEMPLATE = 'https://api.twitch.tv/v5/videos/{}/comments?client_id={}'

    __TYPES_OF_MESSAGES = {
        'ignore': [
            # message saying Live Chat replay is on
            'liveChatViewerEngagementMessageRenderer',
            'liveChatPurchasedProductMessageRenderer',  # product purchased
            'liveChatPlaceholderItemRenderer',  # placeholder
            'liveChatModeChangeMessageRenderer'  # e.g. slow mode enabled
        ],
        'message': [
            'liveChatTextMessageRenderer'  # normal message
        ],
        'superchat': [
            # superchat messages which appear in chat
            'liveChatMembershipItemRenderer',
            'liveChatPaidMessageRenderer',
            'liveChatPaidStickerRenderer',
            # superchat messages which appear ticker (at the top)
            'liveChatTickerPaidStickerItemRenderer',
            'liveChatTickerPaidMessageItemRenderer',
            'liveChatTickerSponsorItemRenderer',
        ]
    }

    # used for debugging
    __TYPES_OF_KNOWN_MESSAGES = []
    for key in __TYPES_OF_MESSAGES:
        __TYPES_OF_KNOWN_MESSAGES.extend(__TYPES_OF_MESSAGES[key])

    __IMPORTANT_KEYS_AND_REMAPPINGS = {
        'timestampUsec': 'timestamp',
        'authorExternalChannelId': 'author_id',
        'authorName': 'author',
        'message': 'message',
        'timestampText': 'time_text',
        'purchaseAmountText': 'amount', # in liveChatPaidMessageRenderer, liveChatPaidStickerRenderer
        'headerBackgroundColor': 'header_color', # in liveChatPaidMessageRenderer
        'bodyBackgroundColor': 'body_color', # in liveChatPaidMessageRenderer
        'amount': 'amount', # in addLiveChatTickerItemAction
        'startBackgroundColor': 'body_color', # in addLiveChatTickerItemAction
        'durationSec': 'ticker_duration', # in liveChatTickerSponsorItemRenderer
        'detailText': 'message', # in liveChatTickerSponsorItemRenderer
        'headerPrimaryText': 'header_primary_text', # in liveChatMembershipItemRenderer
        'headerSubtext': 'header_subtext', # in liveChatMembershipItemRenderer
        'sticker': 'sticker', # in liveChatPaidStickerRenderer
        'backgroundColor': 'body_color', # in liveChatPaidStickerRenderer
    }

    __MAX_RETRIES = 10

    def __init__(self, cookies=None):
        """Initialise a new session for making requests."""
        self.session = requests.Session()
        self.session.headers = self.__HEADERS

        Retry.BACKOFF_MAX = 2 ** 5
        http_adapter = HTTPAdapter(max_retries=Retry(
            total=self.__MAX_RETRIES,
            # Retry doesn't have jitter functionality; following random usage is a poor man's version that only jitters backoff_factor across sessions.
            backoff_factor=random.uniform(1.0, 1.5),
            status_forcelist=[413, 429, 500, 502, 503, 504], # also retries on connection/read timeouts
            allowed_methods=False)) # retry on any HTTP method (including GET and POST)
        self.session.mount('https://', http_adapter)
        self.session.mount('http://', http_adapter)

        cj = MozillaCookieJar(cookies)
        if cookies is not None:
            # Only attempt to load if the cookie file exists.
            if os.path.exists(cookies):
                self.logger.debug("loading cookies from {!r}", cookies)
                cj.load(ignore_discard=True, ignore_expires=True)
            else:
                raise CookieError(f"The file {cookies!r} could not be found.")
        self.session.cookies = cj

    def save_cookies(self, cookies):
        self.logger.debug("saving cookies to {!r}", cookies)
        self.session.cookies.save(cookies)

    def __session_get(self, url, post_payload=None):
        """Make a request using the current session."""
        if post_payload is not None:
            if self.logger.isEnabledFor(logging.TRACE): # guard since json.dumps is expensive
                self.logger.trace("HTTP POST {!r} <= payload JSON (pretty-printed):\n{}", url, _debug_dump(post_payload)) # too verbose
            post_payload = json.dumps(post_payload)
        connection_read_timeout_try_ct = 1
        while True:
            try:
                if post_payload is None:
                    response = self.session.get(url, timeout=10)
                    break
                else:
                    response = self.session.post(url, data=post_payload, timeout=10)
                    break
            # Workaround for https://stackoverflow.com/questions/67614642/python-requests-urrllib3-retry-on-read-timeout-after-200-header-received
            except requests.exceptions.ConnectionError as e:
                if str(e).endswith('Read timed out.') and connection_read_timeout_try_ct <= self.__MAX_RETRIES:
                    _print_stacktrace(f"{e!r}: try {connection_read_timeout_try_ct}", self.logger.warning) # and continue to next try
                    connection_read_timeout_try_ct += 1
                else:
                    raise
        return response

    def __session_get_json(self, url, post_payload=None):
        """Make a request using the current session and get json data."""
        try:
            data = self.__session_get(url, post_payload).json()
        except json.JSONDecodeError as e:
            raise ParsingError("Could not parse JSON from response to {!r}:\n{}".format(url, e.doc)) from e
        if self.logger.isEnabledFor(logging.TRACE): # guard since json.dumps is expensive
            self.logger.trace("HTTP {} {!r} => response JSON:\n{}", 'GET' if post_payload is None else 'POST', url, _debug_dump(data))
        return data

    @staticmethod
    def __timestamp_to_microseconds(timestamp):
        """
        Convert RFC3339 timestamp to microseconds.
        This is needed as datetime.strptime() does not support nanosecond precision.
        """
        info = list(filter(None, re.split(r'[\.|Z]{1}', timestamp))) + [0]
        return round((datetime.strptime('{}Z'.format(info[0]), '%Y-%m-%dT%H:%M:%SZ').timestamp() + float('0.{}'.format(info[1])))*1e6)

    @staticmethod
    def __time_to_seconds(time):
        """Convert timestamp string of the form 'hh:mm:ss' to seconds."""
        return sum(abs(int(x)) * 60 ** i for i, x in enumerate(reversed(time.replace(',', '').split(':')))) * (-1 if time[0] == '-' else 1)

    @staticmethod
    def __seconds_to_time(seconds):
        """Convert seconds to timestamp."""
        time_text = str(timedelta(seconds=seconds))
        return time_text if time_text != '0:0' else ''

    @classmethod
    def __ensure_seconds(cls, time, default=0):
        """Ensure time is returned in seconds."""
        try:
            return int(time)
        except ValueError:
            return cls.__time_to_seconds(time)
        except:
            return default

    @classmethod
    def __timestamp_microseconds_to_datetime_str(cls, timestamp_microseconds):
        """Convert unix timestamp in microseconds to datetime string."""
        return datetime.fromtimestamp(timestamp_microseconds // 1_000_000).strftime(cls.DATETIME_FORMAT)

    @staticmethod
    def __fromisoformat(date_str):
        if date_str is None:
            return None
        return datetime.fromisoformat(date_str)

    @staticmethod
    def __arbg_int_to_rgba(argb_int):
        """Convert ARGB integer to RGBA array."""
        red = (argb_int >> 16) & 255
        green = (argb_int >> 8) & 255
        blue = argb_int & 255
        alpha = (argb_int >> 24) & 255
        return [red, green, blue, alpha]

    @staticmethod
    def __rgba_to_hex(colours):
        """Convert RGBA array to hex colour."""
        return '#{:02x}{:02x}{:02x}{:02x}'.format(*colours)

    @classmethod
    def __get_colours(cls, argb_int):
        """Given an ARGB integer, return both RGBA and hex values."""
        rgba_colour = cls.__arbg_int_to_rgba(argb_int)
        hex_colour = cls.__rgba_to_hex(rgba_colour)
        return {
            'rgba': rgba_colour,
            'hex': hex_colour
        }

    def message_to_string(self, item):
        """
        Format item for printing to standard output.
        [datetime] (author_type) *money* author: message,
        where (author_type) and *money* are optional.
        """
        return '[{}] {}{}{}:\t{}'.format(
            item['datetime'] if 'datetime' in item else (
                item['time_text'] if 'time_text' in item else ''),
            '({}) '.format(item['author_type'].lower()) if 'author_type' in item else '',
            '*{}* '.format(item['amount']) if 'amount' in item else '',
            item.get('author', ''),
            item.get('message', '')
        )

    def print_item(self, item):
        """
        Ensure printing to standard output can be done safely (especially on Windows).
        There are usually issues with printing emojis and non utf-8 characters.
        """
        # Don't print if it is a ticker message (prevents duplicates)
        if 'ticker_duration' in item:
            return

        message = emoji.demojize(self.message_to_string(item))

        try:
            safe_string = message.encode(
                'utf-8', 'ignore').decode('utf-8', 'ignore')
            print(safe_string, flush=True)
        except UnicodeEncodeError:
            # in the rare case that standard output does not support utf-8
            safe_string = message.encode(
                'ascii', 'ignore').decode('ascii', 'ignore')
            print(safe_string, flush=True)

    def __parse_youtube_link(self, text):
        if text.startswith(('/redirect', 'https://www.youtube.com/redirect')):  # is a redirect link
            info = dict(parse.parse_qsl(parse.urlsplit(text).query))
            return info.get('q') or ''
        elif text.startswith('//'):
            return 'https:' + text
        elif text.startswith('/'):  # is a youtube link e.g. '/watch','/results'
            return self.__YT_HOME + text
        else:  # is a normal link
            return text

    def __parse_message_runs(self, message):
        """ Reads and parses YouTube formatted messages (i.e. runs). """
        if isinstance(message, str):
            message_text = message
        else:
            message_text = ''
            for run in message.get('runs', ()):
                if 'text' in run:
                    if 'navigationEndpoint' in run:  # is a link
                        try:
                            url = run['navigationEndpoint']['commandMetadata']['webCommandMetadata']['url']
                            message_text += self.__parse_youtube_link(url)
                        except:
                            # if something fails, use default text
                            message_text += run['text']

                    else:  # is a normal message
                        message_text += run['text']
                elif 'emoji' in run:
                    emoji_info = run['emoji']
                    if 'shortcuts' in emoji_info:
                        message_text += emoji_info['shortcuts'][0]
                    else:
                        message_text += emoji_info['emojiId']
                else:
                    message_text += str(run)

        return message_text

    def __parse_sticker(self, sticker):
        """Reads and parses YouTube sticker messages."""
        message_text = sticker.get('accessibility', {}).get('accessibilityData', {}).get('label', '')
        if message_text:
            message_text = '<<' + message_text + '>>'
        return message_text

    '''
    Notes on JSON contents from various URLs:

    watch?v=<video_id> => HTML => ytInitialData JSON
    contents.twoColumnWatchNextResults.conversationBar.liveChatRenderer.continuations[0].reloadContinuationData.continuation
    contents.twoColumnWatchNextResults.conversationBar.liveChatRenderer.header.liveChatHeaderRenderer.viewSelector.sortFilterSubMenuRenderer.subMenuItems[i].continuation.reloadContinuationData.continuation
    contents.twoColumnWatchNextResults.conversationBar.conversationBarRenderer.availabilityMessage.messageRenderer.text.runs (if chat N/A)

    live_chat[_replay]?v=<video_id> (unused) => HTML => ytInitialData JSON
    contents.liveChatRenderer.continuations[0].timedContinuationData.continuation
    contents.liveChatRenderer.header.liveChatHeaderRenderer.viewSelector.sortFilterSubMenuRenderer.subMenuItems[i].continuation.reloadContinuationData.continuation

    live_chat[_replay]?continuation=<continuation> => HTML => ytInitialData JSON
    continuationContents.liveChatContinuation.continuations[0].*ContinuationData.continuation
    continuationContents.liveChatContinuation.header.liveChatHeaderRenderer.viewSelector.sortFilterSubMenuRenderer.subMenuItems[i].continuation.reloadContinuationData.continuation
    continuationContents.liveChatContinuation.actions

    get_live_chat[_replay]?key=<api_key> (plus POST data) => JSON
    continuationContents.liveChatContinuation.continuations[0].*ContinuationData.continuation
    continuationContents.liveChatContinuation.header.liveChatHeaderRenderer.viewSelector.sortFilterSubMenuRenderer.subMenuItems[i].continuation.reloadContinuationData.continuation
    continuationContents.liveChatContinuation.actions
    contents.messageRenderer.text.runs.text (if chat N/A)
    '''

    __YT_HTML_REGEXES = {
        'ytcfg': re.compile(r'\bytcfg\s*\.\s*set\(\s*({.*})\s*\)\s*;'),
        'ytInitialPlayerResponse': re.compile(r'\bytInitialPlayerResponse\s*=\s*({.+?})\s*;'),
        'ytInitialData': re.compile(r'(?:\bwindow\s*\[\s*["\']ytInitialData["\']\s*\]|\bytInitialData)\s*=\s*(\{.+\})\s*;'),
    }
    __json_decoder = json.JSONDecoder() # for more lenient raw_decode usage
    def __parse_video_text(self, regex_key, html):
        m = self.__YT_HTML_REGEXES[regex_key].search(html)
        if not m:
            self.logger.debug("video HTML (failed parse):\n{}", html)
            raise ParsingError('Unable to parse video data. Please try again.')
        data, _ = self.__json_decoder.raw_decode(m.group(1))
        if self.logger.isEnabledFor(logging.TRACE): # guard since json.dumps is expensive
            self.logger.trace("{}:\n{}", regex_key, _debug_dump(data))
        return data

    def __get_initial_youtube_info(self, video_id):
        """ Get initial YouTube video information from its watch page. """
        self.logger.debug("get_initial_youtube_info: video_id={}", video_id)
        url = self.__YT_WATCH_TEMPLATE.format(video_id)
        html = self.__session_get(url).text

        try:
            ytInitialPlayerResponse = self.__parse_video_text('ytInitialPlayerResponse', html)
            config = {
                **self.__extract_video_details(ytInitialPlayerResponse),
                **self.__extract_playability_info(ytInitialPlayerResponse),
                **self.__extract_video_microformat(ytInitialPlayerResponse), # note: data currently unused
                **self.__extract_heartbeat_params(ytInitialPlayerResponse),
            }

            ytInitialData = self.__parse_video_text('ytInitialData', html)
            contents = ytInitialData.get('contents')
            if(not contents):
                raise VideoUnavailable('Video is unavailable (may be private).')
            contents = contents.get('twoColumnWatchNextResults', {}).get('conversationBar', {})
            try:
                container = contents['liveChatRenderer']
                viewselector_submenuitems = container['header']['liveChatHeaderRenderer'][
                    'viewSelector']['sortFilterSubMenuRenderer']['subMenuItems']
                continuation_by_title_map = {
                    x['title']: x['continuation']['reloadContinuationData']['continuation']
                    for x in viewselector_submenuitems
                }
                if self.logger.isEnabledFor(logging.DEBUG): # guard since json.dumps is expensive
                    self.logger.debug("continuation_by_title_map:\n{}", _debug_dump(continuation_by_title_map))
            except LookupError:
                error_message = 'Video does not have a chat replay.'
                try:
                    error_message = self.__parse_message_runs(
                        contents['conversationBarRenderer']['availabilityMessage']['messageRenderer']['text'])
                except LookupError:
                    pass
                config['no_chat_error'] = error_message
                continuation_by_title_map = {}

            self.logger.trace("video HTML (succeeded parse):\n{}", html)
            return config, continuation_by_title_map
        except ParsingError:
            if "window.ERROR_PAGE" in html:
                self.logger.info("HTML error page encountered, likely due to stream changing members-only or private status - retrying")
                return self.__get_initial_youtube_info(video_id)
            else:
                raise

    def __get_initial_continuation_info(self, config, continuation, is_live):
        """Get continuation info via non-API continuation page for a YouTube video. Used to get the first continuation and get config."""
        self.logger.debug("get_initial_continuation_info: continuation={}, is_live={}", continuation, is_live)
        url = self.__YT_INIT_CONTINUATION_TEMPLATE.format('live_chat' if is_live else 'live_chat_replay', continuation)
        html = self.__session_get(url).text

        ytcfg = self.__parse_video_text('ytcfg', html)
        config.update({
            'api_version': ytcfg['INNERTUBE_API_VERSION'],
            'api_key': ytcfg['INNERTUBE_API_KEY'],
            'context': ytcfg['INNERTUBE_CONTEXT'],
        })

        ytInitialData = self.__parse_video_text('ytInitialData', html)
        info = self.__extract_continuation_info(ytInitialData)
        config['logged_out'] = self.__extract_logged_out(ytInitialData)
        if self.logger.isEnabledFor(logging.DEBUG): # guard since json.dumps is expensive
            self.logger.debug("config:\n{}", _debug_dump(config))
        if info is None:
            raise NoContinuation
        return info

    # see "fall back" comment in __get_continuation_info
    def __get_fallback_continuation_info(self, continuation, is_live):
        """Get continuation info via non-API continuation page for a YouTube video. Used as a fallback."""
        self.logger.debug("get_fallback_continuation_info: continuation={}, is_live={}", continuation, is_live)
        url = self.__YT_INIT_CONTINUATION_TEMPLATE.format('live_chat' if is_live else 'live_chat_replay', continuation)
        html = self.__session_get(url).text
        try:
            ytInitialData = self.__parse_video_text('ytInitialData', html)
            info = self.__extract_continuation_info(ytInitialData)
            self.logger.trace("video HTML (succeeded parse):\n{}", html)
            if info is None:
                raise NoContinuation
            return info
        except ParsingError:
            if "window.ERROR_PAGE" in html:
                self.logger.info("HTML error page encountered, likely due to stream changing members-only or private status - retrying")
                return self.__get_fallback_continuation_info(continuation, is_live)
            else:
                raise

    def __extract_video_details(self, info):
        """Extract video details (including title and whether upcoming) from ytInitialPlayerResponse JSON."""
        videoDetails = info.get('videoDetails', {})
        video_details = {
            'title': videoDetails.get('title'),
            'is_live': videoDetails.get('isLive', False),
            'is_upcoming': videoDetails.get('isUpcoming', False),
        }
        if self.logger.isEnabledFor(logging.TRACE): # guard since json.dumps is expensive
            self.logger.trace("video_details:\n{}", _debug_dump(video_details))
        return video_details

    def __extract_playability_info(self, info):
        """Extract playability status info (including scheduled start time) from either API heartbeat JSON or ytInitialPlayerResponse JSON."""
        playabilityStatus = info.get('playabilityStatus', {})
        try:
            timestamp = int(playabilityStatus['liveStreamability']['liveStreamabilityRenderer']['offlineSlate']['liveStreamOfflineSlateRenderer']['scheduledStartTime'])
            scheduled_start_time = datetime.fromtimestamp(timestamp)
        except LookupError:
            scheduled_start_time = None
        playability_info = {
            'playability_status': playabilityStatus.get('status'),
            'scheduled_start_time': scheduled_start_time,
        }
        if self.logger.isEnabledFor(logging.TRACE): # guard since json.dumps is expensive
            self.logger.trace("playability_info:\n{}", _debug_dump(playability_info))
        return playability_info

    def __extract_video_microformat(self, info):
        """Extract video "microformat" info (including whether unlisted, actual start/end times) from ytInitialPlayerResponse JSON."""
        playerMicroformatRenderer = info.get('microformat', {}).get('playerMicroformatRenderer', {})
        video_microformat = {
            'is_unlisted': playerMicroformatRenderer.get('isUnlisted', False),
            'is_live': playerMicroformatRenderer.get('liveBroadcastDetails', {}).get('isLiveNow', False),
            'start_time': self.__fromisoformat(playerMicroformatRenderer.get('liveBroadcastDetails', {}).get('startTimestamp')),
            'end_time': self.__fromisoformat(playerMicroformatRenderer.get('liveBroadcastDetails', {}).get('endTimestamp')),
        }
        if self.logger.isEnabledFor(logging.TRACE): # guard since json.dumps is expensive
            self.logger.trace("video_microformat:\n{}", _debug_dump(video_microformat))
        return video_microformat

    def __extract_heartbeat_params(self, info):
        heartbeatParams = info.get('heartbeatParams', {})
        heartbeat_params = {
            'heartbeat_params': {
                'heartbeatToken': heartbeatParams.get('heartbeatToken'),
                'heartbeatServerData': heartbeatParams.get('heartbeatServerData'),
                'heartbeatRequestParams': {
                    'heartbeatChecks': ['HEARTBEAT_CHECK_TYPE_LIVE_STREAM_STATUS'],
                },
            },
            'heartbeat_interval_secs': float(heartbeatParams['intervalMilliseconds']) / 1000 if 'intervalMilliseconds' in heartbeatParams else None,
        }
        heartbeat_params = _prune_none_values(heartbeat_params)
        if self.logger.isEnabledFor(logging.TRACE): # guard since json.dumps is expensive
            self.logger.trace("heartbeat_params:\n{}", _debug_dump(heartbeat_params))
        return heartbeat_params

    def __get_continuation_info(self, config, continuation, is_live, player_offset_ms=None):
        """Get continuation info via API for a YouTube video."""
        self.logger.debug("get_continuation_info: continuation={}, is_live={}, player_offset_ms={}", continuation, is_live, player_offset_ms)
        url = self.__YT_CONTINUATION_TEMPLATE.format(config['api_version'], 'live_chat' if is_live else 'live_chat_replay', config['api_key'])
        payload = {
            'context': config['context'],
            'continuation': continuation,
        }
        if not is_live and player_offset_ms is not None:
            payload['currentPlayerState'] = {
                'playerOffsetMs': str(player_offset_ms),
            }
        data = self.__get_youtube_json(url, payload)
        info = self.__extract_continuation_info(data)
        if info is None:
            # YouTube API does not return continuation info (but still returns responseContext, incl loggedOut status) for live (non-replay)
            # members-only streams that have become (or are already) unlisted, even if user is a member and cookies have us logged into YouTube,
            # possibly because we lack a client screen nonce (CSN) (which would be difficult to replicate, since both generation and
            # publishing-to-server of the CSN is in obfuscated live_chat_polymer.js, which in turn may require something like Selenium to
            # simulate a web browser to fetch generated/published CSN).
            # Workaround is to fall back to the non-API continuation endpoint that's used to get the first continuation, which somehow still
            # works for such live streams.
            # This condition is detected by initial continuation indicating we're logged in, and the YouTube API indicating we're not.
            # Unfortunately this condition also can trigger at the end of a live stream (last continuation has loggedOut=true for some reason),
            # but since this only results in one additional request to the non-API continuation endpoint, this is acceptable.
            if not config['logged_out'] and self.__extract_logged_out(data):
                self.logger.debug('initial continuation has loggedOut=false while next continuation has loggedOut=true - '
                    'falling back to always using non-API continuation endpoint')
                # continue to return None
            else:
                raise NoContinuation
        return info

    def __extract_continuation_info(self, data):
        """Extract continuation info from ytInitialData JSON or API continuation JSON."""
        try:
            info = data['continuationContents']['liveChatContinuation']
        except LookupError:
            info = None
        return info

    def __extract_logged_out(self, data):
        """Extract logged out status from ytInitialData JSON or API continuation JSON."""
        try:
            logged_out = data['responseContext']['mainAppWebResponseContext']['loggedOut']
        except LookupError:
            logged_out = None
        self.logger.trace("responseContext.mainAppWebResponseContext.loggedOut: {}", logged_out)
        return True if logged_out is None else logged_out # if loggedOut is somehow missing, assume it's true

    def __get_playability_info(self, config, video_id):
        """Get playability info (including scheduled start date) via API heartbeat for a YouTube video."""
        self.logger.debug("get_playability_info: video_id={}", video_id)
        url = self.__YT_HEARTBEAT_TEMPLATE.format(config['api_version'], config['api_key'])
        sequence_number = config.get('heartbeat_sequence_number', 0) # stored in config for convenience
        config['heartbeat_sequence_number'] = sequence_number + 1
        payload = {
            'context': config['context'],
            **config['heartbeat_params'],
            'videoId': video_id,
            'sequenceNumber': sequence_number,
        }
        return self.__extract_playability_info(self.__get_youtube_json(url, payload))

    def __get_fallback_playability_info(self, video_id):
        """Get playability info (including scheduled start date) from watch page for a YouTube video. Used as a fallback."""
        self.logger.debug("get_fallback_playability_info: video_id={}", video_id)
        # TODO: use https://www.youtube.com/get_video_info endpoint
        # see https://github.com/Tyrrrz/YoutubeExplode/blob/master/YoutubeExplode/Bridge/YoutubeController.cs
        # and https://github.com/pytube/pytube/blob/master/pytube/extract.py
        url = self.__YT_WATCH_TEMPLATE.format(video_id)
        html = self.__session_get(url).text
        try:
            ytInitialPlayerResponse = self.__parse_video_text('ytInitialPlayerResponse', html)
            info = self.__extract_playability_info(ytInitialPlayerResponse)
            self.logger.trace("video HTML (succeeded parse):\n{}", html)
            return info
        except ParsingError:
            if "window.ERROR_PAGE" in html:
                self.logger.info("HTML error page encountered, likely due to stream changing members-only or private status - retrying")
                return self.__get_fallback_playability_info(video_id)
            else:
                raise

    def __get_youtube_json(self, url, payload):
        """Get JSON for a YouTube API url"""
        data = self.__session_get_json(url, payload)
        error = data.get('error')
        if error:
            # Error code 403 'The caller does not have permission' error likely means the stream was privated immediately while the chat is still active.
            error_code = error.get('code')
            if error_code == 403:
                raise VideoUnavailable
            elif error_code == 404:
                raise VideoNotFound
            else:
                raise ParsingError("JSON response to {!r} is error:\n{}".format(url, _debug_dump(data)))
        return data

    __AUTHORTYPE_ORDER_MAP = {value: index for index, value in enumerate(('', 'VERIFIED', 'MEMBER', 'MODERATOR', 'OWNER'))}
    def __parse_item(self, item):
        """Parse YouTube item information."""
        data = {}
        index = list(item.keys())[0]
        item_info = item[index]

        # Never before seen index, may cause error (used for debugging)
        if(index not in self.__TYPES_OF_KNOWN_MESSAGES):
            self.logger.warning("unknown message type: {}", index)

        important_item_info = {key: value for key, value in item_info.items(
        ) if key in self.__IMPORTANT_KEYS_AND_REMAPPINGS}

        data.update(important_item_info)

        for key in important_item_info:
            new_key = self.__IMPORTANT_KEYS_AND_REMAPPINGS[key]
            data[new_key] = data.pop(key)

            # get simpleText if it exists
            if(type(data[new_key]) is dict and 'simpleText' in data[new_key]):
                data[new_key] = data[new_key]['simpleText']

        author_badges = item_info.get('authorBadges')
        if author_badges:
            badges = []
            author_type = ''
            for badge in author_badges:
                badge_renderer = badge.get('liveChatAuthorBadgeRenderer')
                if badge_renderer:
                    tooltip = badge_renderer.get('tooltip')
                    icon_type = badge_renderer.get('icon', {}).get('iconType')
                    if tooltip:
                        badges.append(tooltip)
                        if not icon_type:
                            icon_type = 'MEMBER'
                    if icon_type and (author_type == '' or self.__AUTHORTYPE_ORDER_MAP.get(icon_type, 0) >= self.__AUTHORTYPE_ORDER_MAP.get(author_type, 0)):
                        author_type = icon_type
            data['badges'] = ', '.join(badges)
            data['author_type'] = author_type

        if('showItemEndpoint' in item_info):  # has additional information
            data.update(self.__parse_item(
                item_info['showItemEndpoint']['showLiveChatItemEndpoint']['renderer']))
            return data

        message = None
        if 'header_primary_text' in data: # indicates "member for <x> months" item with optional message
            message = self.__parse_message_runs(data.pop('header_primary_text'))
            if 'header_subtext' in data: # membership level name
                message = f"{message} ({self.__parse_message_runs(data.pop('header_subtext'))})"
            if 'message' in data:
                message = f"{message}: {self.__parse_message_runs(data['message'])}"
        elif 'header_subtext' in data: # indicates "welcome to <x>!" or "upgraded membership to <x>!" item
            message = self.__parse_message_runs(data.pop('header_subtext'))
        elif 'sticker' in data: # indicates paid sticker item (with optional message?)
            message = self.__parse_sticker(data.pop('sticker'))
            if 'message' in data:
                message = f"{message}: {self.__parse_message_runs(data['message'])}"
        elif 'amount' in data: # indicates superchat item with optional message (also in sticker but handled above)
            if 'message' in data:
                message = self.__parse_message_runs(data['message'])
            else:
                message = '<<no message>>'
        elif 'message' in data: # indicates normal chat item
            message = self.__parse_message_runs(data['message'])
        if message is None:
            self.logger.warning("could not extract message from item:\n{}", _debug_dump(item))
        data['message'] = message

        timestamp = data.get('timestamp')
        if timestamp:
            timestamp = int(timestamp)
            data['timestamp'] = timestamp
            data['datetime'] = self.__timestamp_microseconds_to_datetime_str(timestamp)

        if('time_text' in data):
            data['time_in_seconds'] = int(
                self.__time_to_seconds(data['time_text']))

        for colour_key in ('header_color', 'body_color'):
            if(colour_key in data):
                data[colour_key] = self.__get_colours(data[colour_key])

        return data

    # used to construct each args.abort_condition option
    # resulting args.abort_condition structure:
    # list of condition groups, where condition group is a list of (condition string (name:arg), condition function) tuples
    # conditions are ANDed within a condition group, and condition groups are ORed
    # any boolean formula can be converted into this OR of ANDs form (a.k.a. disjunctive normal form)
    @classmethod
    def parse_abort_condition_group(cls, raw_cond_group, abort_signals=None, error_gen=ValueError):
        cond_group = []
        cond_name_dict = {} # for ensuring uniqueness

        raw_conds = list(cls._tokenize_abort_condition_group(raw_cond_group, error_gen))
        for raw_cond in raw_conds:
            raw_cond = raw_cond.strip()
            cond_name, cond_arg = raw_cond.split(':', 1) if ':' in raw_cond else (raw_cond, None)
            if cond_name in cond_name_dict:
                raise error_gen(f"({raw_cond_group}) multiple {cond_name} conditions cannot exist within in the option argument "
                    f"(cannot have both {cond_name_dict[cond_name]!r} and {raw_cond!r})")
            cond_name_dict[cond_name] = raw_cond

            if cond_name == 'changed_scheduled_start_time':
                datetime_format = cond_arg
                if datetime_format.startswith('+'):
                    datetime_format = datetime_format[1:]
                    change_type = 'increased'
                elif datetime_format.startswith('-'):
                    datetime_format = datetime_format[1:]
                    change_type = 'decreased'
                else:
                    change_type = 'changed'
                # test format round-trip
                try:
                    sample_formatted = datetime.now().strftime(datetime_format)
                    datetime.strptime(sample_formatted, datetime_format)
                except ValueError as e:
                    raise error_gen(f"({raw_cond_group}) {e}")
                cls.logger.debug("abort condition {}: {!r} => type {!r}, format {!r} (e.g. {!r})", cond_name, cond_arg,
                    change_type, datetime_format, sample_formatted)
                def changed_scheduled_start_time(orig_scheduled_start_time, scheduled_start_time,
                        # trick to 'fix' the value of variable for this function, since variable changes over loop iterations
                        datetime_format=datetime_format, change_type=change_type, **_):
                    if not orig_scheduled_start_time or not scheduled_start_time:
                        return None # falsy
                    if change_type == 'increased':
                        if orig_scheduled_start_time >= scheduled_start_time: # only consider increases in scheduled start date
                            return None
                    elif change_type == 'decreased':
                        if orig_scheduled_start_time <= scheduled_start_time: # only consider decreases in scheduled start date
                            return None
                    else: # if change_type == 'changed'
                        if orig_scheduled_start_time == scheduled_start_time: # consider any changes in scheduled start date
                            return None
                    orig_formatted = orig_scheduled_start_time.strftime(datetime_format)
                    curr_formatted = scheduled_start_time.strftime(datetime_format)
                    if orig_formatted != curr_formatted:
                        return "scheduled start time formatted as {!r} {} to {:{}} (originally {:{}})".format(datetime_format,
                            change_type, scheduled_start_time, datetime_format, orig_scheduled_start_time, datetime_format)
                cond_group.append((raw_cond, changed_scheduled_start_time))

            elif cond_name == 'min_time_until_scheduled_start_time':
                m = re.fullmatch(r'(\d+):(\d+)$', cond_arg)
                if not m:
                    raise error_gen(f"({raw_cond_group}) {cond_name} argument must be in format <hours>:<minutes>, e.g. 01:30")
                min_secs = int(m[1]) * 3600 + int(m[2]) * 60
                cls.logger.debug("abort condition {}: {!r} => min {} secs", cond_name, cond_arg, min_secs)
                def min_time_until_scheduled_start_time(scheduled_start_time,
                        # trick to 'fix' the value of variable for this function, since variable changes over loop iterations
                        min_secs=min_secs, **_):
                    if not scheduled_start_time:
                        return None # falsy
                    current_time = time.time()
                    secs_until_scheduled_start_time = scheduled_start_time.timestamp() - current_time
                    if secs_until_scheduled_start_time > min_secs:
                        return "current time ({:{}}) until scheduled start time ({:{}}): {} secs >= {} secs".format(
                            datetime.fromtimestamp(current_time), cls.DATETIME_FORMAT, scheduled_start_time, cls.DATETIME_FORMAT,
                            secs_until_scheduled_start_time, min_secs)
                cond_group.append((raw_cond, min_time_until_scheduled_start_time))

            elif cond_name == 'file_exists':
                cls.logger.debug("abort condition {}: file {!r}", cond_name, cond_arg)
                def file_exists(
                        # trick to 'fix' the value of variable for this function, since variable changes over loop iterations
                        path=cond_arg, **_):
                    if os.path.isfile(path):
                        fstat = os.stat(path)
                        return "file {!r} exists with ctime {} and mtime {}".format(path,
                            datetime.fromtimestamp(fstat.st_ctime).strftime(cls.DATETIME_FORMAT),
                            datetime.fromtimestamp(fstat.st_mtime).strftime(cls.DATETIME_FORMAT))
                    return None
                cond_group.append((raw_cond, file_exists))

            elif cond_name.startswith('SIG'):
                try:
                    abort_signal = getattr(signal, cond_name)
                except AttributeError:
                    raise error_gen(f"({raw_cond_group}) unrecognized signal name: {cond_name}")
                if len(raw_conds) > 1:
                    raise error_gen(f"({raw_cond_group}) signal condition must be only condition in the option argument")
                try:
                    abort_signal_type = SignalAbortType[cond_arg]
                except LookupError:
                    raise error_gen("({}) signal condition argument must be one of: {}".format(
                        raw_cond_group, ', '.join(abort_type.name for abort_type in SignalAbortType)))
                cls.logger.debug("abort condition {}: {!r} => {}", cond_name, abort_signal, abort_signal_type)
                abort_signals[abort_signal] = abort_signal_type
            else:
                raise error_gen(f"({raw_cond_group}) unrecognized condition: {raw_cond}")

        return cond_group

    # splits on + while handling escapes \\ and \&
    _ABORT_CONDITION_TOKEN_REGEX = re.compile(r'((?:\\.|[^&\\])*)([&]|$)')
    @classmethod
    def _tokenize_abort_condition_group(cls, raw_cond_group, error_gen):
        prev_pos = 0
        for m in cls._ABORT_CONDITION_TOKEN_REGEX.finditer(raw_cond_group):
            if prev_pos != m.start(): # means invalid string (trailing backslash)
                raise error_gen(f"({raw_cond_group}) invalid string (unexpected end of string): {raw_cond_group[prev_pos:]}")
            raw_cond = m[1]
            if len(raw_cond) == 0:
                raise error_gen(f"({raw_cond_group}) condition cannot be empty")
            else:
                yield raw_cond.replace('\\&', '&').replace('\\\\', '\\')
            prev_pos = m.end()
            if len(m[2]) == 0: # matched end of string
                break # next iteration would start at end of string and match empty string, which we want to avoid

    class AbortConditionChecker:
        def __init__(self, logger, cond_groups, *state_funcs, state=None):
            self.logger = logger
            self.cond_groups = cond_groups
            self.state_funcs = state_funcs
            self.state = loggingutils.ChangelogMapWrapper({} if state is None else state)

        def check(self):
            # TODO: move state updating out of AbortConditionChecker since should be done regardless of abort conditions for logging purposes
            # update state first
            for prereq_func in self.state_funcs:
                prereq_func(self.state)
            for record in self.state.changelog:
                if self.logger.isEnabledFor(record.level):
                    if record.type is loggingutils.ChangelogRecord.added:
                        self.logger.log(record.level, "Video {} is {}",
                            record.key.replace('_', ' '), _debug_dump(record.new))
                    elif record.type is loggingutils.ChangelogRecord.changed:
                        self.logger.log(record.level, "Video {} changed from {} to {}",
                            record.key.replace('_', ' '), _debug_dump(record.old), _debug_dump(record.new))
                    elif record.type is loggingutils.ChangelogRecord.deleted:
                        self.logger.log(record.level, "Video {} changed from {} to (unset)",
                            record.key.replace('_', ' '), _debug_dump(record.old))
            self.state.changelog.clear()

            # then the actual cond checks
            for cond_group_idx, cond_group in enumerate(self.cond_groups):
                cond_group_result = self._check_cond_group(cond_group_idx, cond_group)
                if cond_group_result: # ANY cond group must evaluate to be truthy
                    raise AbortConditionsSatisfied(cond_group_result)

        def _check_cond_group(self, cond_group_idx, cond_group):
            cond_results = []
            for raw_cond, cond_func in cond_group:
                # note: following will error if state is missing a key that's a required parameter for the cond_func
                cond_result = cond_func(**self.state)
                self.logger.trace("abort condition [group {}] {}(**{!r}) => {}",
                    cond_group_idx, raw_cond, self.state, cond_result)
                if not cond_result: # ALL conditions in a group must evaluate to be truthy
                    return None
                cond_results.append(cond_result)
            return ' AND '.join(cond_results) or None

    @loggingutils.contextdecorator
    def _log_with_video_id(self, video_id, *args, **kwargs):
        cls_logger = loggingutils.getLogger(self.__class__.logger) # ensure we get the Logger and not a LoggerAdapter
        self.logger = loggingutils.FormatLoggerAdapter(cls_logger, style='{', extra={'context': f"[{kwargs.get('log_base_context', '')}{video_id}]"})
        try:
            yield
        finally:
            del self.logger

    @_log_with_video_id
    def get_youtube_messages(self, video_id, start_time=None, end_time=None, message_type='messages', chat_type='live', callback=None, output_messages=None, **kwargs):
        """ Get chat messages for a YouTube video. """
        start_time = self.__ensure_seconds(start_time, None)
        end_time = self.__ensure_seconds(end_time, None)
        self.logger.trace("video_id={}, start_time={}, end_time={}, message_type={}, chat_type={}, kwargs={}",
            video_id, start_time, end_time, message_type, chat_type, kwargs)
        abort_cond_groups = kwargs.get('abort_condition')

        messages = [] if output_messages is None else output_messages

        player_offset_ms = start_time * 1000 if isinstance(start_time, numbers.Number) else None

        # Top chat replay - Some messages, such as potential spam, may not be visible
        # Live chat replay - All messages are visible
        chat_type_field = chat_type.title()
        chat_replay_field = '{} chat replay'.format(chat_type_field)
        chat_live_field = '{} chat'.format(chat_type_field)

        abort_cond_state = {
            'orig_scheduled_start_time': None,
        }

        try:
            abort_cond_checker = None
            continuation_title = None
            attempt_ct = 0
            while True:
                attempt_ct += 1
                config, continuation_by_title_map = self.__get_initial_youtube_info(video_id)

                if(chat_replay_field in continuation_by_title_map):
                    is_live = False
                    continuation_title = chat_replay_field
                elif(chat_live_field in continuation_by_title_map):
                    is_live = True
                    continuation_title = chat_live_field

                if continuation_title is None:
                    error_message = config.get('no_chat_error', 'Video does not have a chat replay.')
                    if config['is_upcoming'] or config['is_live']:
                        if abort_cond_groups:
                            if abort_cond_checker is None:
                                def nochat_abort_cond_state_updater(state):
                                    state.info['playability_status'] = config['playability_status']
                                    scheduled_start_time = config['scheduled_start_time']
                                    if state.get('orig_scheduled_start_time') is None:
                                        state.debug['orig_scheduled_start_time'] = scheduled_start_time
                                    state.info['scheduled_start_time'] = scheduled_start_time
                                abort_cond_checker = self.AbortConditionChecker(self.logger, abort_cond_groups,
                                    nochat_abort_cond_state_updater, state=abort_cond_state)
                            abort_cond_checker.check()

                        retry_wait_secs = random.randint(45, 60) # jitter
                        if self.logger.isEnabledFor(logging.INFO):
                            self.logger.info("Upcoming {} Retrying in {} secs (attempt {})",
                                _trans_first_char(error_message, str.lower), retry_wait_secs, attempt_ct)
                        time.sleep(retry_wait_secs)
                    else:
                        raise NoChatReplay(error_message)
                else:
                    break
            continuation = continuation_by_title_map[continuation_title]
            # TODO: get local title from microformat, poll request https://www.youtube.com/youtubei/v1/updated_metadata for local title updates
            # and https://www.youtube.com/youtubei/v1/updated_metadata for page title updates,
            # fallback to https://www.youtube.com/get_video_info for both and scheduled start time updates
            if self.logger.isEnabledFor(logging.INFO):
                self.logger.info("Downloading {} for video: {}", _trans_first_char(continuation_title, str.lower), config['title'])

            abort_cond_checker = None
            first_time = True
            use_non_api_fallback = False
            while True:
                if abort_cond_groups:
                    if abort_cond_checker is None:
                        def abort_cond_state_updater(state):
                            poll_timestamp = state.get('poll_timestamp')
                            # if first call, init with config
                            if poll_timestamp is None:
                                state.trace['poll_timestamp'] = time.time()
                                state.info['playability_status'] = config['playability_status']
                                scheduled_start_time = config['scheduled_start_time']
                                if state.get('orig_scheduled_start_time') is None:
                                    state.debug['orig_scheduled_start_time'] = scheduled_start_time
                                state.info['scheduled_start_time'] = scheduled_start_time
                            # if playability status is already OK, video has started (is no longer upcoming), so stop updating state
                            elif state['playability_status'] != 'OK':
                                now_timestamp = time.time()
                                if now_timestamp > poll_timestamp + config.get('heartbeat_interval_secs', 60.0):
                                    state.trace['poll_timestamp'] = now_timestamp
                                    if use_non_api_fallback:
                                        playability_info = self.__get_fallback_playability_info(video_id)
                                    else:
                                        playability_info = self.__get_playability_info(config, video_id)
                                    state.info.update(playability_info)
                        abort_cond_checker = self.AbortConditionChecker(self.logger, abort_cond_groups,
                            abort_cond_state_updater, state=abort_cond_state)
                    abort_cond_checker.check()

                try:
                    if first_time:
                        # note: first_time is toggled off at end of this iteration in case first_time is used elsewhere
                        info = self.__get_initial_continuation_info(config, continuation, is_live) # note: updates config
                    elif use_non_api_fallback:
                        info = self.__get_fallback_continuation_info(continuation, is_live)
                    else:
                        info = self.__get_continuation_info(config, continuation, is_live, player_offset_ms)
                        # if above returns None yet doesn't throw NoContinuation, that means fallback to always use fallback continuation endpoint
                        if info is None:
                            use_non_api_fallback = True
                            continue
                except NoContinuation:
                    print('No continuation found, stream may have ended.')
                    break
                except VideoUnavailable:
                    print('Video not unavailable, stream may have been privated while live chat was still active.')
                    break
                except VideoNotFound:
                    print('Video not found, stream may have been deleted while live chat was still active.')
                    break

                if('actions' in info):
                    for action in info['actions']:
                        try:
                            data = {}

                            if('replayChatItemAction' in action):
                                replay_chat_item_action = action['replayChatItemAction']
                                if('videoOffsetTimeMsec' in replay_chat_item_action):
                                    data['video_offset_time_msec'] = int(
                                        replay_chat_item_action['videoOffsetTimeMsec'])
                                action = replay_chat_item_action['actions'][0]

                            action.pop('clickTrackingParams', None)
                            action_name = list(action.keys())[0]
                            if('item' not in action[action_name]):
                                # not a valid item to display (usually message deleted)
                                continue

                            item = action[action_name]['item']
                            index = list(item.keys())[0]

                            if(index in self.__TYPES_OF_MESSAGES['ignore']):
                                # can ignore message (not a chat message)
                                continue

                            # user wants everything, keep going
                            if(message_type == 'all'):
                                pass

                            # user does not want superchat + message is superchat
                            elif(message_type != 'superchat' and index in self.__TYPES_OF_MESSAGES['superchat']):
                                continue

                            # user does not want normal messages + message is normal
                            elif(message_type != 'messages' and index in self.__TYPES_OF_MESSAGES['message']):
                                continue

                            data = dict(self.__parse_item(item), **data)

                            time_in_seconds = data['time_in_seconds'] if 'time_in_seconds' in data else None

                            valid_seconds = time_in_seconds is not None
                            if(end_time is not None and valid_seconds and time_in_seconds > end_time):
                                return messages

                            if(is_live or start_time is None or (valid_seconds and time_in_seconds >= start_time)):
                                messages.append(data)

                                if(callback is None):
                                    self.print_item(data)

                                elif(callable(callback)):
                                    try:
                                        callback(data)
                                    except TypeError:
                                        raise CallbackFunction(
                                            'Incorrect number of parameters for function '+callback.__name__)
                        except Exception:
                            _print_stacktrace('Error encountered handling item:\n' + _debug_dump(action))
                else:
                    # no more actions to process in a chat replay
                    if(not is_live):
                        break

                if('continuations' in info):
                    continuation_info = info['continuations'][0]
                    # possible continuations:
                    # invalidationContinuationData, timedContinuationData,
                    # liveChatReplayContinuationData, reloadContinuationData
                    continuation_info = continuation_info[next(
                        iter(continuation_info))]

                    if 'continuation' in continuation_info:
                        continuation = continuation_info['continuation']
                    if 'timeoutMs' in continuation_info:
                        # must wait before calling again
                        # prevents 429 errors (too many requests)
                        self.logger.trace("continuation timeoutMs={}", continuation_info['timeoutMs'])
                        time.sleep(continuation_info['timeoutMs']/1000)
                else:
                    break

                first_time = False

            return messages

        except AbortConditionsSatisfied as e:
            print('[Abort conditions satisfied]', e, flush=True)
            return messages
        except KeyboardInterrupt:
            print('[Interrupted]', flush=True)
            return messages

    @_log_with_video_id
    def get_twitch_messages(self, video_id, start_time=None, end_time=None, callback=None, output_messages=None, **kwargs):
        """ Get chat messages for a Twitch stream. """
        start_time = self.__ensure_seconds(start_time, 0)
        end_time = self.__ensure_seconds(end_time, None)

        messages = [] if output_messages is None else output_messages

        api_url = self.__TWITCH_API_TEMPLATE.format(
            video_id, self.__TWITCH_CLIENT_ID)

        cursor = ''
        try:
            while True:
                url = '{}&cursor={}&content_offset_seconds={}'.format(
                    api_url, cursor, start_time)
                info = self.__session_get_json(url)

                if('error' in info):
                    raise TwitchError(info['message'])

                for comment in info['comments']:
                    time_in_seconds = float(comment['content_offset_seconds'])
                    if(start_time is not None and time_in_seconds < start_time):
                        continue

                    if(end_time is not None and time_in_seconds > end_time):
                        return messages

                    created_at = comment['created_at']

                    data = {
                        'timestamp': self.__timestamp_to_microseconds(created_at),
                        'time_text': self.__seconds_to_time(int(time_in_seconds)),
                        'time_in_seconds': time_in_seconds,
                        'author': comment['commenter']['display_name'],
                        'message': comment['message']['body']
                    }

                    messages.append(data)

                    if(callback is None):
                        self.print_item(data)

                    elif(callable(callback)):
                        try:
                            callback(data)
                        except TypeError:
                            raise CallbackFunction(
                                'Incorrect number of parameters for function '+callback.__name__)

                if '_next' in info:
                    cursor = info['_next']
                else:
                    return messages

        except KeyboardInterrupt:
            print('[Interrupted]', flush=True)
            return messages

    def get_chat_replay(self, url, start_time=None, end_time=None, message_type='messages', chat_type='live', callback=None, output_messages=None, **kwargs):
        match = self.__YT_REGEX.search(url)
        if(match):
            return self.get_youtube_messages(match.group(1), start_time, end_time, message_type, chat_type, callback, output_messages, **kwargs)

        match = self.__TWITCH_REGEX.search(url)
        if(match):
            return self.get_twitch_messages(match.group(1), start_time, end_time, callback, output_messages, **kwargs)

        raise InvalidURL('The url provided ({}) is invalid.'.format(url))


def get_chat_replay(url, start_time=None, end_time=None, message_type='messages', chat_type='live', callback=None, output_messages=None, **kwargs):
    return ChatReplayDownloader().get_chat_replay(url, start_time, end_time, message_type, chat_type, callback, output_messages, **kwargs)

def get_youtube_messages(url, start_time=None, end_time=None, message_type='messages', chat_type='live', callback=None, output_messages=None, **kwargs):
    return ChatReplayDownloader().get_youtube_messages(url, start_time, end_time, message_type, chat_type, callback, output_messages, **kwargs)

def get_twitch_messages(url, start_time=None, end_time=None, callback=None, output_messages=None, **kwargs):
    return ChatReplayDownloader().get_twitch_messages(url, start_time, end_time, callback, output_messages, **kwargs)

# json.dumps with more suitable default arguments
def _debug_dump(obj, *, ensure_ascii=False, indent=4, default=str, **kwargs):
    return json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, default=default, **kwargs)

def _trans_first_char(text, func):
    return func(text[:1]) + text[1:]

# recursively filter out None values from iterables
# note: does not handle cyclic structures
def _prune_none_values(obj):
    if isinstance(obj, collections.abc.Mapping):
        d = {k: _prune_none_values(v) for k, v in obj.items() if v is not None}
        if obj.__class__ is not dict:
            d = obj.__class__(d)
        return d
    elif isinstance(obj, collections.abc.Iterable) and not isinstance(obj, (str, collections.abc.ByteString)):
        l = [_prune_none_values(v) for v in obj if v is not None]
        if obj.__class__ is not list:
            l = obj.__class__(l)
        return l
    else:
        return obj

# print full stack trace (rather than only up to the containing method)
def _print_stacktrace(message=None, log=None):
    import traceback
    # using print by default rather than logger in case logging system somehow failed
    if log is None:
        log_prefix = f"[ERROR][{datetime.now():{ChatReplayDownloader.DATETIME_FORMAT}}]"
        log = lambda x: print(log_prefix, x, file=sys.stderr)
    stacklines = traceback.format_exc().splitlines(keepends=True)
    # first line of stacklines is always "Traceback (most recent call last):", so insert after this
    # exclude the last 2 frames from traceback.extract_stack(), which are the call to extract_stack() itself and _print_stacktrace
    stacklines[1:1] = traceback.format_list(traceback.extract_stack()[:-2])
    if message is not None:
        log(message)
    log(''.join(stacklines).rstrip())

# if adding as a subparser, pass `parser_type=subparsers.add_parser, cmd_name`
# if adding to an existing parser or argument group, pass as parser parameter
def gen_arg_parser(abort_signals=None, add_positional_arguments=True, parser=None, parser_type=argparse.ArgumentParser, *parser_type_args, **parser_type_kwargs):
    if not parser:
        parser = parser_type(
            *parser_type_args,
            description='A simple tool used to retrieve YouTube/Twitch chat from past broadcasts/VODs. No authentication needed!',
            formatter_class=argparse.RawTextHelpFormatter,
            **parser_type_kwargs)

    if add_positional_arguments:
        parser.add_argument('url', help='YouTube/Twitch video URL')

    parser.add_argument('--start_time', '--from', default=None,
                        help='start time in seconds or hh:mm:ss [no effect on non-replay YouTube videos]\n(default: from the start)')
    parser.add_argument('--end_time', '--to', default=None,
                        help='end time in seconds or hh:mm:ss [no effect on non-replay YouTube videos]\n(default: until the end)')

    parser.add_argument('--message_type', choices=['messages', 'superchat', 'all'], default='messages',
                        help='types of messages to include [YouTube only]\n(default: %(default)r)')

    parser.add_argument('--chat_type', choices=['live', 'top'], default='live',
                        help='which chat to get messages from [YouTube only]\n(default: %(default)r)')

    parser.add_argument('--output', '-o', default=None,
                        help='name of output file\n(default: no output file)\n'
                             'Note: logging, which also includes chat messages, may still output to stdout and/or file depending on --log_file)')

    parser.add_argument('--cookies', '-c', default=None,
                        help='name of cookies file to load from\n(default: no cookies loaded)')

    parser.add_argument('--save_cookies', default=None,
                        help='name of cookies file to save to, which can be the same value as --cookies\n(default: no cookies saved)')

    if abort_signals is None:
        abort_cond_type = str # assume this means we don't want to parse the abort conditions themselves
    else:
        abort_cond_type = lambda raw_cond_group: ChatReplayDownloader.parse_abort_condition_group(
                                                    raw_cond_group, abort_signals, lambda msg: argparse.ArgumentError(abort_cond_action, msg))
    abort_cond_action = parser.add_argument('--abort_condition', action='append', type=abort_cond_type,
                        help="a condition on which this application aborts (note: ctrl+c is such a condition by default)\n"
                             "Available conditions for upcoming streams:\n"
                             "* changed_scheduled_start_time:<strftime format e.g. %%Y%%m%%d> [YouTube-only]\n"
                             "  True if datetime.strftime(<strftime format>) changes between initially fetched scheduled start datetime\n"
                             "  and latest fetched scheduled start datetime.\n"
                             "  If <strftime format> starts with a plus sign (+), only considers increases in scheduled start datetime.\n"
                             "  If <strftime format> starts with a minus sign (-), only considers decreases in scheduled start datetime.\n"
                             "* min_time_until_scheduled_start_time:<hours>:<minutes> [YouTube-only]\n"
                             "  True if (latest fetched scheduled start datetime - current datetime) >= timedelta(hours=<hours>, minutes=<minutes>).\n"
                             "Other available conditions:\n"
                             "* file_exists:<path>\n"
                             "  True if <path>, given as either relative to working directory or absolute, exists (whether before or during execution).\n"
                             "  Note: argument may need to be quoted if <path> contains e.g. whitespace.\n"
                             "* <signal name e.g. SIGINT>:<{}>\n".format('|'.join(abort_type.name for abort_type in SignalAbortType)) +
                             "  {}\n".format(SignalAbortType.__doc__) +
                             ''.join(f"  * {abort_type.name}\n{textwrap.indent(abort_type.value, '    ')}\n" for abort_type in SignalAbortType) +
                             "  Note: this cannot be grouped with other abort conditions within a single --abort_condition option (see below).\n"
                             "Multiple abort conditions (excluding the signal abort condition) can be specified within a single --abort_condition option,\n"
                             "delimited by & (whitespace allowed before and after; whole argument may need to be quoted depending on shell),\n"
                             "and such abort conditions are ANDed together as a 'condition group'.\n"
                             "In case a condition argument itself must contain &, & can be escaped as \\& (and \\ can be escaped as \\\\).\n"
                             "Multiple --abort_condition options can be specified, and the condition groups represented by each option are ORed together.\n"
                             "Example:\n"
                             "  --abort_condition 'changed_scheduled_start_time:%%Y%%m%%d + min_time_until_scheduled_start_time:00:10'\n"
                             "  --abort_condition min_time_until_scheduled_start_time:24:00\n"
                             "  --abort_condition SIGINT:disable\n"
                             "means abort if:\n"
                             "  (both scheduled start datetime changes date AND current time until scheduled start datetime is at least 10 minutes)\n"
                             "  OR current time until scheduled start datetime is at least 24 hours\n"
                             "  IN ADDITION to disabling the application-aborting SIGINT handler\n"
                             "Any combination of ORs and ANDs can be represented by this system, since abort conditions are effectively a boolean formula,\n"
                             "and any boolean formula can be converted into this OR of ANDs form (a.k.a. disjunctive normal form).")

    parser.add_argument('--hide_output', action='store_true',
                        help="if specified, changes the default of --log_file to ':none:',\n"
                             "i.e. hide both stdout and stderr unless user specifies --log_file x, where x is not :none:\n"
                             "(deprecated - instead use: --log_file :none:)")

    parser.add_argument('--log_file', action='append',
                        help="file (or console) to log output to, including redirecting stdout and stderr to it\n"
                             "(default: ':console:')\n"
                             "If ':console:', outputs stdout and stderr to console as normal.\n"
                            f"If ':none:', hides both stdout and stderr (redirects them to {os.devnull!r}).\n"
                             "Else, redirects stdout and stderr to the given log file.\n"
                             "Multiple --log_file options can be specified, allowing output to multiple log files and/or console.")

    parser.add_argument('--log_level',
                        choices=[name for level, name in logging._levelToName.items() if level != 0],
                        default=logging._levelToName[logging.WARNING],
                        help='log level, logged to standard output\n(default: %(default)r)')

    parser.add_argument('--log_base_context', default='',
                        help='lines logged to standard output are formatted as:\n'
                             '"[<log_level>][<datetime>][<log_base_context><video_id>] <message>" (without the quotes)\n'
                             "(default: %(default)r)")

    parser.add_argument('--newline', default='',
                        help='newline terminator as a Python-escaped string, e.g. \\r\\n for Windows-style CRLF\n'
                             '(default: empty string, which means use the system default, e.g. CRLF on Windows)')

    return parser

def main(args):
    logger = ChatReplayDownloader.logger

    abort_signals = {getattr(signal, signal_name): SignalAbortType.default for signal_name in DEFAULT_SIGNAL_ABORT_NAMES}

    # preprocess any long-form '-' args into '--' args
    args = ['-' + arg if len(arg) >= 3 and arg[0] == '-' and arg[1] != '-' else arg for arg in args]

    parser = gen_arg_parser(abort_signals)
    args = parser.parse_args(args)

    # to be passed to open/reconfigure function as newline argument
    if args.newline:
        # if args.newline is not empty, it's a Python-escaped string, so need to unescape it
        import ast
        newline = ast.literal_eval('"' + args.newline + '"')
    else:
        # if args.newline is empty, ensure newline=None is passed to open function for universal newlines
        newline = None

    # ensure utf8 encoding and newline setting for stdout and stderr
    orig_stdout_encoding = sys.stdout.encoding
    if orig_stdout_encoding != 'utf-8' or newline:
        sys.stdout.reconfigure(encoding='utf-8', newline=newline)
    orig_stderr_encoding = sys.stderr.encoding
    if orig_stderr_encoding != 'utf-8' or newline:
        sys.stderr.reconfigure(encoding='utf-8', newline=newline)

    def open_log_file(log_file):
        if log_file == ':console:':
            return None
        elif log_file == ':none:':
            return open(os.devnull, 'w')
        else:
            return open(log_file, 'w', encoding='utf-8-sig', newline=newline)
    if args.log_file:
        log_files = [open_log_file(log_file) for log_file in args.log_file]
    else:
        log_files = [open_log_file(':none:' if args.hide_output else ':console:')]
    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    out_log_files = [log_file if log_file else sys.stdout for log_file in log_files]
    err_log_files = [log_file if log_file else sys.stderr for log_file in log_files]
    if len(log_files) == 1:
        sys.stdout = out_log_files[0]
        sys.stderr = err_log_files[0]
    else:
        sys.stdout = ioutils.MultiFile(*out_log_files)
        sys.stderr = ioutils.MultiFile(*err_log_files)

    # this has to go after stdout/stderr are modified
    logging.basicConfig(force=True, level=args.log_level, stream=sys.stdout,
                        format='[%(levelname)s][%(asctime)s][%(name)s] %(message)s', datefmt=ChatReplayDownloader.DATETIME_FORMAT)

    num_of_messages = 0
    chat_messages = []

    orig_signal_handlers = {}
    def print_signal_received(signum, msg):
        name = signal.Signals(signum).name # pylint: disable=no-member # pylint lies - signal.Signals does exist
        print(f"[Signal Received: {name}] {msg}", flush=True)

    called_finalize_output=False
    def finalize_output(signum=None, frame=None):
        if signum:
            print_signal_received(signum, 'Aborting')

        nonlocal called_finalize_output
        if called_finalize_output:
            return
        else:
            called_finalize_output = True

        nonlocal num_of_messages
        try:
            if chat_messages and args.output:
                if(args.output.endswith('.json')):
                    num_of_messages = len(chat_messages)
                    with open(args.output, 'w', encoding='utf-8-sig', newline=newline) as f:
                        json.dump(chat_messages, f, sort_keys=True)

                elif(args.output.endswith('.csv')):
                    num_of_messages = len(chat_messages)
                    fieldnames = set()
                    for message in chat_messages:
                        fieldnames.update(message.keys())
                    fieldnames = sorted(fieldnames)

                    csv_dialect = csv.excel
                    if newline:
                        class NewlineDialect(csv.excel):
                            lineterminator = newline
                        csv_dialect = NewlineDialect
                    with open(args.output, 'w', encoding='utf-8-sig', newline='') as f:
                        fc = csv.DictWriter(f, fieldnames=fieldnames, dialect=csv_dialect)
                        fc.writeheader()
                        fc.writerows(chat_messages)

                print('Finished writing', num_of_messages,
                    'messages to', args.output, flush=True)
        finally:
            try:
                for orig_signal, orig_handler in orig_signal_handlers.items():
                    signal.signal(orig_signal, orig_handler)
                for log_file in log_files:
                    if log_file: # if an actual file (not sys.__stdout__ or sys.__stderr__)
                        #print(f"Closing {log_file}", flush=True)
                        log_file.close()
                sys.stdout = orig_stdout
                sys.stderr = orig_stderr
                # note: no way to get original newline setting, so assume it was None (default)
                if orig_stdout_encoding != 'utf-8' or newline:
                    sys.stdout.reconfigure(encoding=orig_stdout_encoding, newline=None)
                if orig_stderr_encoding != 'utf-8' or newline:
                    sys.stderr.reconfigure(encoding=orig_stderr_encoding, newline=None)
            finally:
                if signum:
                    sys.exit()

    def noop_handler(signum, frame):
        print_signal_received(signum, 'Ignored')

    def register_handler(abort_signal, handler):
        orig_signal_handlers[abort_signal] = signal.getsignal(abort_signal)
        signal.signal(abort_signal, handler)
        logger.debug("registered {} for {!r}", handler.__name__, abort_signal)

    # depending on SignalAbortType for each abort signal, either allow graceful exit or noop that signal
    for abort_signal, abort_type in abort_signals.items():
        if abort_signal is signal.SIGINT: # own case since SIGINT's default handler throws KeyboardInterrupt (and also Windows-specific stuff)
            # The low-level Windows ctrl+c handler prevents Python SIGINT signal handler for any job launched in the background,
            # even if such a job is later restored to the foreground. Furthermore, ctrl+c is sent even to background jobs.
            # Thus, if this low-level handler is disabled and we handle a SIGINT, we can't accurately determine whether this is
            # being run in a foreground (should abort) or a background (should NOT abort) job.
            # As a workaround, 'default' and 'enable' have different behaviors to allow user to choose which tradeoff is best for them:
            # 'default' abort type: can abort foreground-launched job, but cannot abort background-launched job that's later restored to foreground
            # 'enable' abort type:  can abort foreground job, whether background-launched, but also aborts background job
            # 'disable' abort type: never aborts (unchanged with respect to how other signals are handled)
            if abort_type is not SignalAbortType.default:
                # Disable the low-level Windows ctrl+c handler only if abort behavior between whether foreground and background job is the same,
                # i.e. always abort or never abort.
                try:
                    import ctypes
                    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
                    logger.debug("disabled low-level Windows {} handler", signal.CTRL_C_EVENT)
                except:
                    pass
            if abort_type is SignalAbortType.disable:
                register_handler(abort_signal, noop_handler)
            # else, let SIGINT's default handler throw KeyboardInterrupt, which we already handle gracefully
        else:
            if abort_type is SignalAbortType.disable:
                register_handler(abort_signal, noop_handler)
            elif abort_type is SignalAbortType.enable:
                register_handler(abort_signal, finalize_output)
            elif abort_signal.name in DEFAULT_SIGNAL_ABORT_NAMES: # and abort_type is SignalAbortType.default
                register_handler(abort_signal, finalize_output)

    try:
        chat_downloader = ChatReplayDownloader(cookies=args.cookies)

        def print_item(item):
            chat_downloader.print_item(item)

        def write_to_file(item):
            nonlocal num_of_messages

            # Don't print if it is a ticker message (prevents duplicates)
            if 'ticker_duration' in item:
                return

            # only file format capable of appending properly
            with open(args.output, 'a', encoding='utf-8-sig', newline=newline) as f:
                num_of_messages += 1
                print_item(item)
                text = chat_downloader.message_to_string(item)
                print(text, file=f)

        callback = None if args.output is None else print_item
        if(args.output is not None):
            if(args.output.endswith('.json')):
                pass
            elif(args.output.endswith('.csv')):
                pass
            else: # assume text file
                open(args.output, 'w').close()  # empty the file
                callback = write_to_file

        # using output_messages arg rather than return value, in case of uncaught exception or caught signal within the call
        chat_downloader.get_chat_replay(callback=callback, output_messages=chat_messages, **vars(args))

        if args.save_cookies:
            chat_downloader.save_cookies(args.save_cookies)

    except InvalidURL as e:
        print('[ERROR][Invalid URL]', e, flush=True)
    except ParsingError as e:
        print('[ERROR][Parsing Error]', e, flush=True)
    except NoChatReplay as e:
        print('[ERROR][No Chat Replay]', e, flush=True)
    except VideoUnavailable as e:
        print('[ERROR][Video Unavailable]', e, flush=True)
    except TwitchError as e:
        print('[ERROR][Twitch Error]', e, flush=True)
    except CookieError as e:
        print('[ERROR][Cookies Error]', e, flush=True)
    except KeyboardInterrupt: # this should already be caught within get_chat_replay, but keeping this just in case
        print('[Interrupted]', flush=True)
    except SystemExit: # finalize_output may call sys.exit() which raises SystemExit
        pass # in case main() is being called from another module, don't actually exit the app
    except Exception:
        _print_stacktrace()
    finally:
        finalize_output()

if __name__ == '__main__':
    main(sys.argv[1:])
