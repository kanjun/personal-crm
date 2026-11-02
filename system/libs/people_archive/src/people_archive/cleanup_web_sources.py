"""Preserve public research sources without treating their identity claims as verified."""
import base64
import ipaddress
import socket
import time
from collections.abc import Callable
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from imbue.imbue_common.frozen_model import FrozenModel

from people_archive.cleanup_models import PublicResearch

_MAX_BYTES = 2_000_000
_BLOCKED_DOMAINS = ('apollo.io', 'zoominfo.com', 'lusha.com', 'rocketreach.co', 'clearbit.com', 'people-data-labs.com', 'peopledatalabs.com', 'hunter.io')


class WebSource(FrozenModel):
    url: str
    final_url: str
    fetched_at: datetime
    status: str
    raw_html: str = ''
    raw_body_base64: str = ''
    text: str = ''
    reason: str = ''


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {'script', 'style'}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def page_text(html: str) -> str:
    parser = _PageText()
    parser.feed(html)
    return ' '.join(' '.join(parser.parts).split())


def resolve_host(host: str, port: int) -> list[str]:
    return list({str(item[4][0]) for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def public_addresses(url: str, resolver: Callable[[str, int], list[str]] = resolve_host) -> list[str]:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return []
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        return []
    host = parsed.hostname.lower().rstrip('.')
    if any(host == domain or host.endswith('.' + domain) for domain in _BLOCKED_DOMAINS):
        return []
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        return []
    if port not in {80, 443}:
        return []
    try:
        addresses = resolver(host, port)
    except (socket.gaierror, UnicodeError):
        return []
    return addresses if addresses and all(ipaddress.ip_address(address).is_global for address in addresses) else []


def fetch_source(url: str, client: httpx.Client, resolver: Callable[[str, int], list[str]] = resolve_host) -> WebSource:
    current = url
    timestamp = datetime.now(timezone.utc)
    began = time.monotonic()
    for _ in range(6):
        if time.monotonic() - began > 45:
            return WebSource(url=url, final_url=current, fetched_at=timestamp, status='unavailable', reason='Source exceeded total time budget.')
        addresses = public_addresses(current, resolver)
        if not addresses:
            return WebSource(url=url, final_url=current, fetched_at=timestamp, status='blocked', reason='Only public HTTP(S) websites outside enrichment providers are allowed.')
        original_url = httpx.URL(current)
        pinned_url = original_url.copy_with(host=addresses[0])
        # Connect to the validated address, preserving HTTP Host and TLS SNI.
        # Never resolve again between validation and connection.
        try:
            with client.stream('GET', pinned_url, headers={'Host': original_url.netloc.decode(), 'Connection': 'close'},
                               extensions={'sni_hostname': original_url.host}, follow_redirects=False, timeout=20) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    current = urljoin(current, response.headers.get('location', ''))
                    continue
                content = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() - began > 45:
                        return WebSource(url=url, final_url=current, fetched_at=timestamp, status='unavailable', reason='Source exceeded total time budget.')
                    if len(content) + len(chunk) > _MAX_BYTES:
                        return WebSource(url=url, final_url=current, fetched_at=timestamp, status='too_large', reason='Source exceeds two megabytes; no claims validated.')
                    content.extend(chunk)
                raw = bytes(content)
                html = raw.decode(response.encoding or 'utf-8', errors='replace')
                return WebSource(url=url, final_url=current, fetched_at=timestamp,
                                 status='fetched' if response.is_success else 'http_error',
                                 raw_html=html, raw_body_base64=base64.b64encode(raw).decode(), text=page_text(html),
                                 reason='' if response.is_success else f'HTTP {response.status_code}')
        except httpx.HTTPError as error:
            return WebSource(url=url, final_url=current, fetched_at=timestamp, status='unavailable', reason=str(error))
    return WebSource(url=url, final_url=current, fetched_at=timestamp, status='blocked', reason='Too many redirects.')


def preserve_public_sources(research: PublicResearch) -> tuple[PublicResearch, list[WebSource]]:
    """Fetch source originals and retain only quote-supported proposals, not verified identities."""
    urls = sorted({citation.source_id for fact in research.facts for citation in fact.citations})
    with httpx.Client(trust_env=False, limits=httpx.Limits(max_keepalive_connections=0), headers={'User-Agent': 'Mozilla/5.0 (compatible; CRMSourceArchive/1.0)'}) as client:
        sources = [fetch_source(url, client) for url in urls]
    return validate_public_research(research, sources), sources


def validate_public_research(research: PublicResearch, sources: list[WebSource]) -> PublicResearch:
    by_url = {source.url: source for source in sources}
    supported = [fact for fact in research.facts if fact.citations and all(
        citation.source_id in by_url and by_url[citation.source_id].status == 'fetched'
        and ' '.join(citation.quote.split()) in by_url[citation.source_id].text
        for citation in fact.citations)]
    return research.model_copy(update={'facts': supported})
