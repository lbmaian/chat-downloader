import os.path
import re
import logging
from http.cookiejar import MozillaCookieJar


class CookieError(Exception):
    """Raised when an error occurs while loading a cookie file."""
    pass


class CookieJarProvider():
    def __init__(self, cookie_file, cookie_browser, logger):
        self.logger = logger.getChild('cookies')
        try:
            if cookie_browser:
                import yt_dlp.cookies
                # yt_dlp.cookies debug logs potentially sensitive file paths,
                # so bump up log level to INFO if above TRACE log level
                log_level = self.logger.getEffectiveLevel()
                if log_level < logging.INFO and log_level > getattr(logging, 'TRACE', 0):
                    self.logger.setLevel(logging.INFO)
                # copied from yt_dlp/__init__.py opts.cookiesfrombrowser handling
                container = None
                mobj = re.fullmatch(r'''(?x)
                    (?P<name>[^+:]+)
                    (?:\s*\+\s*(?P<keyring>[^:]+))?
                    (?:\s*:\s*(?!:)(?P<profile>.+?))?
                    (?:\s*::\s*(?P<container>.+))?
                ''', cookie_browser)
                if mobj is None:
                    raise ValueError(f'invalid cookies from browser arguments: {cookie_browser}')
                browser_name, keyring, profile, container = mobj.group('name', 'keyring', 'profile', 'container')
                browser_name = browser_name.lower()
                # copied from yt_dlp/cookies.py load_cookies
                self.browser_spec = yt_dlp.cookies._parse_browser_specification(browser_name, profile, keyring, container)
                logger.info(f"yt-dlp cookie browser specification: {self.browser_spec!r}")
                self.file = None
                return
        except Exception as e:
            raise CookieError from e

        self.browser_spec = None
        self.file = cookie_file
        if cookie_file and not os.path.exists(cookie_file):
            raise CookieError(f"The file {cookie_file!r} could not be found.")

    def load(self):
        log_level = self.logger.getEffectiveLevel()
        if self.browser_spec:
            import yt_dlp.cookies
            browser_name, profile, keyring, container = self.browser_spec
            return yt_dlp.cookies.extract_cookies_from_browser(browser_name, profile, self.logger, keyring=keyring, container=container)
        else:
            cookies = MozillaCookieJar(self.file)
            if self.file:
                self.logger.info("Loading cookies from {self.file!r}")
                cookies.load(ignore_discard=True, ignore_expires=True)
        if log_level > getattr(logging, 'TRACE', 0):
            self.logger.trace("Loaded cookies: {cookies!r}")
        return cookies
