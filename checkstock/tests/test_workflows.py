import os
os.environ["DATABASE_URL"] = "sqlite:///./test_checkstock.db"
from fastapi.testclient import TestClient
from app.main import app
from app.database import Base, engine

Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
client = TestClient(app)

def setup_module():
    # startup event seeds the demonstration products and warehouses
    with TestClient(app) as c:
        c.get('/api/health')

def get_ids():
    data=client.get('/api/overview').json()
    product=next(x for x in data['products'] if x['sku']=='SKU-101')
    north=next(x for x in data['warehouses'] if x['name']=='Север')
    south=next(x for x in data['warehouses'] if x['name']=='Юг')
    return product['id'],north['id'],south['id']

def test_inbound_and_idempotency():
    p,n,_=get_ids()
    payload={'product_id':p,'warehouse_id':n,'quantity':3,'idempotency_key':'test-inbound-1'}
    assert client.post('/api/inbound',json=payload).status_code==200
    duplicate=client.post('/api/inbound',json=payload).json()
    assert duplicate.get('duplicate') is True
    row=next(x for x in client.get('/api/overview').json()['stocks'] if x['product_id']==p and x['warehouse_id']==n)
    assert row['physical']==13

def test_transfer_and_receive_once():
    p,n,s=get_ids()
    sent=client.post('/api/transfers',json={'product_id':p,'source_warehouse_id':n,'destination_warehouse_id':s,'quantity':2,'idempotency_key':'test-transfer-1'})
    assert sent.status_code==200
    shipment=next(x for x in client.get('/api/overview').json()['shipments'] if x['kind']=='TRANSFER')
    body={'shipment_id':shipment['id'],'idempotency_key':'test-receive-1'}
    assert client.post('/api/shipments/receive',json=body).status_code==200
    assert client.post('/api/shipments/receive',json={'shipment_id':shipment['id'],'idempotency_key':'test-receive-2'}).status_code==409

def test_fbs_reserve_cancel():
    p,n,_=get_ids()
    r=client.post('/api/fbs/reserve',json={'product_id':p,'warehouse_id':n,'quantity':2,'idempotency_key':'test-reserve-1'})
    assert r.status_code==200
    reservation=client.get('/api/reservations').json()[0]
    assert reservation['quantity']==2
    assert client.post('/api/fbs/cancel',json={'shipment_id':reservation['id'],'idempotency_key':'test-cancel-1'}).status_code==200

def test_fbo_auto_allocate_from_other_warehouse():
    data=client.get('/api/overview').json()
    p=next(x['id'] for x in data['products'] if x['sku']=='SKU-103')
    n=next(x['id'] for x in data['warehouses'] if x['name']=='Север')
    s=next(x['id'] for x in data['warehouses'] if x['name']=='Юг')
    # На выбранном Севере ноль, но на Юге есть 4 шт.: запрос должен распределиться на Юг.
    r=client.post('/api/fbo/dispatch',json={'product_id':p,'warehouse_id':n,'quantity':3,'auto_allocate':True,'idempotency_key':'test-fbo-1'})
    assert r.status_code==200
    shipment=next(x for x in client.get('/api/overview').json()['shipments'] if x['kind']=='FBO' and x['sku']=='SKU-103')
    assert shipment['source']=='Юг' and shipment['quantity']==3

def test_archive_keeps_stock_and_history():
    data=client.get('/api/overview').json(); south=next(x for x in data['warehouses'] if x['name']=='Юг')
    before=sum(x['physical'] for x in data['stocks'] if x['warehouse_id']==south['id'])
    assert client.delete(f"/api/warehouses/{south['id']}").status_code==200
    after=client.get('/api/overview').json()
    archived=next(x for x in after['warehouses'] if x['id']==south['id'])
    assert archived['active'] is False
    assert sum(x['physical'] for x in after['stocks'] if x['warehouse_id']==south['id'])==before


def test_fbs_ship_releases_reserved_and_physical():
    data=client.get('/api/overview').json()
    p=next(x['id'] for x in data['products'] if x['sku']=='SKU-102')
    n=next(x['id'] for x in data['warehouses'] if x['name']=='Север')
    reserved=client.post('/api/fbs/reserve',json={'product_id':p,'warehouse_id':n,'quantity':1,'idempotency_key':'test-reserve-send'}).json()
    reservation=client.get('/api/reservations').json()[0]
    before=next(x for x in client.get('/api/overview').json()['stocks'] if x['product_id']==p and x['warehouse_id']==n)
    assert before['physical']==5 and before['reserved']==1 and before['available']==4
    assert client.post('/api/fbs/ship',json={'shipment_id':reservation['id'],'idempotency_key':'test-fbs-ship'}).status_code==200
    after=next(x for x in client.get('/api/overview').json()['stocks'] if x['product_id']==p and x['warehouse_id']==n)
    assert after['physical']==4 and after['reserved']==0 and after['available']==4
