from search_console_mcp.errors import redact

def test_embedded_google_oauth_token_is_never_returned():
    assert 'ya29.FIXTURESECRET' not in redact('prefixya29.FIXTURESECRET')
