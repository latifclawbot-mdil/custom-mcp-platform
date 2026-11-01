"""Authentication must refresh stale credentials before issuing a request."""
from types import SimpleNamespace
import httpx
from play_console_mcp.google_client import GoogleApiClient


def test_expired_cached_token_is_refreshed_before_request():
    class Creds:
        token = 'expired-fixture'
        valid = False
        def refresh(self, request):
            self.token = 'fresh-fixture'
            self.valid = True

    seen = []
    def handler(req):
        seen.append(req.headers['authorization'])
        return httpx.Response(200, json={})

    client = GoogleApiClient(settings=SimpleNamespace(request_timeout=2), credentials=Creds(), transport=httpx.MockTransport(handler))
    client.get('/androidpublisher/v3/applications/{packageName}/reviews', params={'packageName': 'org.example.app'})
    assert seen == ['Bearer fresh-fixture']
