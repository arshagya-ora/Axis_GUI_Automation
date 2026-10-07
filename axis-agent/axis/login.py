"""Narrow authorization for a user-supplied login to a named application."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

@dataclass(frozen=True)
class LoginIntent:
    app_url: str
    username: str
    password: str
    auth_url: str | None = None

    @property
    def app_host(self) -> str | None:
        return urlsplit(self.app_url).hostname


def parse_login_intent(instruction: str) -> LoginIntent | None:
    """Require an explicit login, one app URL, and both supplied credentials."""
    if not re.search(r"\b(?:log[ -]?in|sign[ -]?in|authenticate)\b", instruction, re.I):
        return None
    urls = re.findall(r"https?://[^\s<>\"'`]+", instruction, re.I)
    if not urls:
        ip = re.search(r"(?<![\w./:])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\w.])", instruction)
        urls = ['http://' + ip.group()] if ip else []
    urls = list(dict.fromkeys(url.rstrip('.,);]') for url in urls))
    if len(urls) != 1:
        return None
    user = re.search(r"(?im)^\s*(?:user(?:name)?|email)\s*:\s*(\S+)\s*$", instruction)
    password = re.search(r"(?im)^\s*password\s*:\s*(\S+)\s*$", instruction)
    if not user or not password or not urlsplit(urls[0]).hostname:
        return None
    return LoginIntent(urls[0], user.group(1), password.group(1))


def login_destination_matches(intent: LoginIntent, current_url: str) -> bool:
    """Trust the requested application and its runtime-observed login origin."""
    parsed, authorized = urlsplit(current_url), urlsplit(intent.app_url)
    def origin(url):
        return (url.scheme.lower(), url.hostname, url.port or (443 if url.scheme == 'https' else 80))
    return (parsed.scheme in {'http', 'https'} and bool(parsed.hostname)
            and origin(parsed) in {origin(authorized), *([origin(urlsplit(intent.auth_url))] if intent.auth_url else [])})


def authorized_login_action(intent: LoginIntent, detail: dict, operation: str) -> bool:
    current_url = str(detail.get('current_url') or '')
    if not login_destination_matches(intent, current_url):
        return False
    if detail.get('target_type') not in {'ref', 'locator'}:
        return False
    target = str(detail.get('target') or '').casefold()
    observed = detail.get('observed_target') or {}
    destination = observed.get('formAction') or observed.get('href')
    if destination:
        from urllib.parse import urljoin
        resolved = urljoin(current_url, destination)
        if not login_destination_matches(intent, resolved):
            return False
    command = (detail.get('arguments') or {}).get('command') or {}
    value = command.get('value')
    if operation == 'fill':
        if re.search(r'\b(?:password|passwd)\b', target):
            return value == intent.password
        if re.search(r'\b(?:user(?:name)?|email|account)\b', target):
            return value == intent.username
    if operation == 'click' and re.match(r'^link\b', target):
        from urllib.parse import urljoin
        href = observed.get('href')
        if not href:
            match = re.search(r'->\s*(\S+)', str(detail.get('target') or ''))
            href = match.group(1) if match else None
        if href:
            reached = urlsplit(urljoin(current_url, href))
            if login_destination_matches(intent, reached.geturl()) and re.search(
                    r'/oauth2/authorization/|/openid-connect/auth(?:$|/)|/(?:login|signin)(?:$|/)', reached.path):
                return True
    if operation == 'click' and re.search(r'\b(?:log[ -]?in|sign[ -]?in)\b', target):
        return True
    return False


