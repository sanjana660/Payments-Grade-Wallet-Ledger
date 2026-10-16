from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health():
    assert client.get('/health').json() == {'status': 'ok'}

def test_missing_idempotency_key_is_rejected():
    response = client.post('/users/1/wallets', json={'name': 'Main'})
    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'MISSING_IDEMPOTENCY_KEY'
