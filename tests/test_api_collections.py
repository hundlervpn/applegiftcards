import asyncio
import base64
import functools
import json
import re
import shutil
import socket
import subprocess
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
import uvicorn
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db.session import get_db
from app.main import create_app
from app.models.enums import GiftCardStatus, OrderStatus, SubscriptionStatus
from app.models.gift_card import GiftCard
from app.models.order import Order
from app.models.price import Price
from app.models.subscription import Subscription
from app.schemas.admin import AdminStatsResponse, GiftCardBatchCreate, GiftCardBatchResponse
from app.schemas.auth import RefreshRequest, TokenResponse, UserLogin, UserRegister
from app.schemas.catalog import CatalogItemResponse
from app.schemas.health import HealthResponse
from app.schemas.me import OrderHistoryResponse
from app.schemas.me_subscription import SubscriptionListResponse
from app.schemas.order import OneTimeOrderCreate, OneTimeOrderResponse
from app.schemas.payment_webhook import YooKassaNotification
from app.schemas.subscription import SubscriptionCancellationResponse
from app.services import order_checkout, order_fulfillment, payment_methods, payment_webhooks, payments
from app.services.crypto import decrypt_code

ROOT = Path(__file__).resolve().parents[1]
COLLECTION = ROOT / 'postman/awe_api_collection.json'
ENVIRONMENT = ROOT / 'postman/local.postman_environment.json'
BRUNO = ROOT / 'bruno'


def all_requests(items):
    for item in items:
        if 'item' in item:
            yield from all_requests(item['item'])
        else:
            yield item


def operation(request):
    url = request['url']
    if not url.startswith('{{baseUrl}}'):
        return None
    path = url.removeprefix('{{baseUrl}}').split('?', 1)[0]
    path = path.replace('{{testSubscriptionId}}', '{subscription_id}')
    return request['method'].lower(), path


def test_postman_covers_every_application_route():
    collection = json.loads(COLLECTION.read_text())
    assert collection['info']['schema'] == 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json'
    requests = list(all_requests(collection['item']))
    assert len({item['id'] for item in requests}) == len(requests)
    covered = {operation(item['request']) for item in requests}
    expected = {
        (method, path)
        for path, methods in create_app().openapi()['paths'].items()
        for method in methods
    }
    assert expected <= covered
    for item in requests:
        assert item['response'], item['name']
        assert any(event['listen'] == 'test' for event in item['event'])


def test_collection_examples_match_application_dtos():
    notification = {
        'type': 'notification', 'event': 'payment.succeeded',
        'object': {'id': 'test-payment', 'status': 'succeeded',
                   'amount': {'value': '999.00', 'currency': 'RUB'},
                   'metadata': {'order_id': '123', 'is_subscription': 'true'}},
    }
    values = {'runEmail': 'buyer@example.com', 'foreignEmail': 'other@example.com',
              'testPassword': 'DemoPass123', 'testNominalUsd': '10', 'runId': 'demo',
              'refreshToken': 'demo-refresh-token', 'notificationBody': json.dumps(notification)}
    requests = list(all_requests(json.loads(COLLECTION.read_text())['item']))
    inputs = {
        '/api/v1/auth/register': UserRegister, '/api/v1/auth/login': UserLogin,
        '/api/v1/auth/refresh': RefreshRequest, '/api/v1/orders/one-time': OneTimeOrderCreate,
        '/api/v1/admin/gift-cards/batch': GiftCardBatchCreate,
        '/api/v1/payments/webhook': YooKassaNotification,
    }
    outputs = {
        '/health': HealthResponse, '/api/v1/auth/register': TokenResponse,
        '/api/v1/auth/login': TokenResponse, '/api/v1/auth/refresh': TokenResponse,
        '/api/v1/orders/one-time': OneTimeOrderResponse,
        '/api/v1/admin/gift-cards/batch': GiftCardBatchResponse,
        '/api/v1/admin/stats': AdminStatsResponse, '/api/v1/me/orders': OrderHistoryResponse,
        '/api/v1/me/subscriptions': SubscriptionListResponse,
        '/api/v1/subscriptions/{subscription_id}/cancel': SubscriptionCancellationResponse,
    }
    for item in requests:
        route = operation(item['request'])
        if route is None:
            continue
        _, path = route
        response = item['response'][0]
        if response['code'] >= 400:
            assert isinstance(json.loads(response['body'])['detail'], str)
            continue
        if path in inputs:
            raw = item['request']['body']['raw']
            for key, value in values.items():
                raw = raw.replace('{{' + key + '}}', value)
            inputs[path].model_validate_json(raw)
        if path in outputs:
            outputs[path].model_validate_json(response['body'])
        elif path == '/api/v1/catalog':
            for row in json.loads(response['body']):
                CatalogItemResponse.model_validate(row)
        elif path == '/api/v1/payments/webhook':
            assert response['body'] == ''


def test_environment_templates_do_not_contain_credentials():
    variables = {item['key']: item for item in json.loads(ENVIRONMENT.read_text())['values']}
    assert {'baseUrl', 'adminToken', 'authToken', 'testOrderId'} <= variables.keys()
    for key in ('adminToken', 'authToken', 'refreshToken', 'yookassaSecretKey'):
        assert variables[key]['value'] == ''
        assert variables[key]['type'] == 'secret'
    text = (BRUNO / 'environments/local.bru').read_text()
    assert 'vars:secret [' in text
    for key in ('adminToken', 'authToken', 'refreshToken', 'yookassaSecretKey'):
        assert key in text
        assert not re.search(r'^\s*' + key + r':\s*\S', text, re.MULTILINE)
    for path in BRUNO.rglob('*.bru'):
        text = path.read_text()
        assert 'bru.setEnvVar(' not in text
        assert 'bru.setCollectionVar(' not in text


def test_bruno_matches_postman_and_has_native_examples():
    binary = shutil.which('bru')
    if not binary:
        pytest.skip('Install @usebruno/cli to validate native Bruno files')
    script = '''
const fs = require('fs');
const lang = require(require.resolve('@usebruno/lang', {paths:[process.argv[1]]}));
const files = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = files.map(file => {
  const data = lang.bruToJsonV2(fs.readFileSync(file, 'utf8'));
  return {name:data.meta.name, method:data.http.method, url:data.http.url, examples:data.examples};
});
process.stdout.write(JSON.stringify(result));
'''
    files = [str(path) for path in BRUNO.rglob('*.bru') if path.name not in {'folder.bru', 'collection.bru'} and path.parent.name != 'environments']
    result = subprocess.run(['node', '-e', script, str(Path(binary).resolve().parent)],
                            input=json.dumps(files), capture_output=True, text=True, check=True)
    parsed = {item['name']: item for item in json.loads(result.stdout)}
    postman = list(all_requests(json.loads(COLLECTION.read_text())['item']))
    assert len(parsed) == len(postman)
    for item in postman:
        converted = parsed[item['name']]
        assert converted['method'] == item['request']['method'].lower()
        assert converted['url'] == item['request']['url']
        assert converted['examples']
        example = converted['examples'][0]['response']
        assert int(example['status']) == item['response'][0]['code']
        assert example['body']['content'] == item['response'][0]['body']


@pytest.fixture
async def collection_api(monkeypatch, db_engine, db_session):
    for key, value in {
        'ADMIN_API_TOKEN': 'collection-test-admin-token',
        'YOOKASSA_SHOP_ID': 'collection-test-shop',
        'YOOKASSA_SECRET_KEY': 'collection-test-key',
        'JWT_SECRET_KEY': 'collection-test-jwt-signing-secret-at-least-32-bytes',
        'FERNET_KEY': Fernet.generate_key().decode(),
        'SMTP_USER': 'collection-test-smtp', 'SMTP_PASSWORD': 'collection-test-smtp-password',
        'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_ADMIN_CHAT_ID': '',
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    db_session.add(Price(nominal_usd=10, price_rub=Decimal('999.00')))
    await db_session.commit()
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.setblocking(False)
    base_url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    provider_url = base_url + '/__provider/v3'
    state = {'payments': {}, 'emails': [], 'list_calls': 0, 'verified': 0,
             'complete_payment': True, 'cursors': []}
    provider = FastAPI()

    @provider.middleware('http')
    async def provider_auth(request: Request, call_next):
        expected = 'Basic ' + base64.b64encode(b'collection-test-shop:collection-test-key').decode()
        if request.headers.get('authorization') != expected:
            return JSONResponse({'detail': 'Invalid provider credentials'}, status_code=401)
        return await call_next(request)

    @provider.post('/payments')
    async def create_payment(request: Request):
        body = await request.json()
        payment_id = '2c8f7d00-0000-5000-8000-1a2b3c4d5e6f'
        saved = body.get('save_payment_method') is True
        payment = {
            'id': payment_id, 'status': 'pending', 'paid': False, 'test': True,
            'amount': body['amount'], 'metadata': body['metadata'],
            'confirmation': {'type': 'redirect', 'confirmation_url': base_url + '/checkout'},
            'payment_method': {'id': 'test-method-id', 'type': 'bank_card', 'saved': saved},
        }
        state['payments'][payment_id] = payment
        return payment

    @provider.get('/payments')
    async def list_payments(request: Request):
        state['list_calls'] += 1
        state['cursors'].append(request.query_params.get('cursor'))
        if state['list_calls'] == 1:
            return {'items': [], 'next_cursor': 'next-page'}
        if state['list_calls'] >= 3 and state['complete_payment']:
            for payment in state['payments'].values():
                payment.update(status='succeeded', paid=True)
        return {'items': list(state['payments'].values())}

    @provider.get('/payments/{payment_id}')
    async def get_payment(payment_id: str):
        state['verified'] += 1
        return state['payments'][payment_id]

    @provider.get('/payment_methods/{method_id}')
    async def get_method(method_id: str):
        return {'id': method_id, 'type': 'bank_card', 'card': {'last4': '4242'}}

    async def capture_email(message, **kwargs):
        state['emails'].append(message)
        return {}, 'accepted'

    session_maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def get_test_db():
        async with session_maker() as session:
            yield session

    monkeypatch.setattr(order_checkout, 'create_payment', functools.partial(payments.create_payment, api_url=provider_url))
    monkeypatch.setattr(payment_webhooks, 'YOOKASSA_API_URL', provider_url)
    monkeypatch.setattr(payment_methods, 'YOOKASSA_API_URL', provider_url)
    monkeypatch.setattr(order_fulfillment, 'get_session_maker', lambda: session_maker)
    monkeypatch.setattr('app.services.email.aiosmtplib.send', capture_email)
    app = create_app()
    app.dependency_overrides[get_db] = get_test_db
    app.mount('/__provider/v3', provider)
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False, lifespan='off'))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.01)
        yield base_url, provider_url, state
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)
        sock.close()
        get_settings.cache_clear()


@pytest.mark.parametrize('runner', ['newman', 'bru'])
@pytest.mark.parametrize('paid', [True, False])
async def test_collection_runner_purchase_webhook_and_database(runner, paid, collection_api, db_session, tmp_path):
    binary = shutil.which(runner)
    if not binary:
        pytest.skip(f'Install {runner} to run its collection')
    base_url, provider_url, state = collection_api
    state['complete_payment'] = paid
    variables = {'baseUrl': base_url, 'yookassaBaseUrl': provider_url,
                 'adminToken': 'collection-test-admin-token', 'yookassaShopId': 'collection-test-shop',
                 'yookassaSecretKey': 'collection-test-key', 'paymentMaxPolls': '10' if paid else '3'}
    if runner == 'newman':
        command = [binary, 'run', str(COLLECTION), '-e', str(ENVIRONMENT), '--bail', '--timeout-request', '10000']
        cwd = ROOT
    else:
        copy_path = tmp_path / 'bruno'
        shutil.copytree(BRUNO, copy_path)
        command = [binary, 'run', '--env', 'local', '--bail']
        cwd = copy_path
    for key, value in variables.items():
        command.extend(['--env-var', f'{key}={value}'])
    process = await asyncio.create_subprocess_exec(*command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=90)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    assert state['list_calls'] == 3
    assert state['cursors'] == [None, 'next-page', None]
    if runner == 'bru':
        assert (copy_path / 'environments/local.bru').read_bytes() == (BRUNO / 'environments/local.bru').read_bytes()
        assert (copy_path / 'collection.bru').read_bytes() == (BRUNO / 'collection.bru').read_bytes()
    if not paid:
        assert process.returncode != 0
        assert '12 Wait for test payment' in stdout.decode(errors='replace')
        assert state['verified'] == 0
        order = await db_session.scalar(select(Order))
        assert order.status is OrderStatus.pending_payment
        cards = list(await db_session.scalars(select(GiftCard)))
        assert Counter(card.status for card in cards) == {GiftCardStatus.reserved: 1, GiftCardStatus.available: 1}
        assert not list(await db_session.scalars(select(Subscription)))
        assert state['emails'] == []
        return
    assert process.returncode == 0, stdout.decode(errors='replace')
    assert state['verified'] == 2
    assert len(state['payments']) == 1
    orders = list(await db_session.scalars(select(Order)))
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.delivered
    assert orders[0].email_sent_at is not None
    cards = list(await db_session.scalars(select(GiftCard)))
    assert Counter(card.status for card in cards) == {GiftCardStatus.sold: 1, GiftCardStatus.available: 1}
    assert all(card.code_encrypted != decrypt_code(card.code_encrypted).encode() for card in cards)
    subscriptions = list(await db_session.scalars(select(Subscription)))
    assert len(subscriptions) == 1
    assert subscriptions[0].status is SubscriptionStatus.cancelled
    assert len(state['emails']) == 2
    assert 'DEMO-' in state['emails'][0].get_body(preferencelist=('plain',)).get_content()
