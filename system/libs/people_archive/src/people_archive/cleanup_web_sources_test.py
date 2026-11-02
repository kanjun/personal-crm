import base64

import httpx

from people_archive.cleanup_models import Citation, Fact, PublicResearch
from people_archive.cleanup_web_sources import (
    fetch_source,
    page_text,
    public_addresses,
    validate_public_research,
)


def _public(host: str, port: int) -> list[str]:
    return ['93.184.216.34']


def test_refuses_private_addresses_and_paid_enrichment() -> None:
    assert not public_addresses('file:///etc/passwd', _public)
    assert not public_addresses('https://apollo.io/person', _public)
    assert not public_addresses('https://example.com:9000/', _public)
    assert not public_addresses('http://internal/', lambda host, port: ['127.0.0.1'])
    assert not public_addresses('http://internal/', lambda host, port: ['93.184.216.34', '10.1.2.3'])
    assert public_addresses('https://example.com/person', _public)


def test_preserves_original_and_pins_connection_address() -> None:
    html = '<h1>Alex Smith</h1><p>Director &amp; researcher.</p><script>ignored()</script>'
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == '93.184.216.34'
        assert request.headers['host'] == 'example.com'
        assert request.extensions['sni_hostname'] == 'example.com'
        return httpx.Response(200, text=html)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        source = fetch_source('https://example.com/person', client, _public)
    assert source.status == 'fetched'
    assert source.raw_html == html
    assert base64.b64decode(source.raw_body_base64).decode() == html
    assert source.text == 'Alex Smith Director & researcher.'
    assert source.url == source.final_url == 'https://example.com/person'


def test_redirect_to_private_destination_is_never_requested() -> None:
    requests: list[str] = []
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={'Location': 'http://internal/'})
    def resolver(host: str, port: int) -> list[str]:
        return ['127.0.0.1'] if host == 'internal' else ['93.184.216.34']
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        source = fetch_source('https://example.com/person', client, resolver)
    assert source.status == 'blocked'
    assert len(requests) == 1


def test_large_page_is_not_validated() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'x' * 2_000_001))) as client:
        source = fetch_source('https://example.com/', client, _public)
    assert source.status == 'too_large'
    assert not source.text


def test_text_preserves_unicode_and_normalizes_whitespace() -> None:
    assert page_text('<p>李 明</p>\n <p>Director\t of Research</p>') == '李 明 Director of Research'


def test_quotes_must_exist_in_fetched_source() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text='<p>Alex Smith is a director.</p>'))) as client:
        source = fetch_source('https://example.com/', client, _public)
    good = Fact(field='title', value='director', confidence='high', citations=[Citation(source_id=source.url, quote='Alex Smith is a director.')])
    bad = Fact(field='title', value='CEO', confidence='high', citations=[Citation(source_id=source.url, quote='Alex Smith is CEO.')])
    result = validate_public_research(PublicResearch(facts=[good, bad], reason='Public proposals'), [source])
    assert result.facts == [good]
