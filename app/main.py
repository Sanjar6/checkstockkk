from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from .database import Base, engine, get_db, SessionLocal
from .models import Product, Warehouse, Stock, Shipment, Movement, Operation
from .schemas import (WarehouseCreate, InboundRequest, TransferRequest, FbsReserveRequest,
                      FbsActionRequest, FboRequest, ShipmentActionRequest)

app = FastAPI(title="CheckStock", version="1.0.0", description="Учёт остатков, резервов и перемещений товаров")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if db.scalar(select(Product).limit(1)) is None:
            products = [Product(sku="SKU-101", name="Органайзер"), Product(sku="SKU-102", name="Контейнер"), Product(sku="SKU-103", name="Корзина")]
            north, south = Warehouse(name="Север"), Warehouse(name="Юг")
            db.add_all(products + [north, south]); db.flush()
            db.add_all([Stock(product_id=products[0].id, warehouse_id=north.id, physical=10),
                        Stock(product_id=products[0].id, warehouse_id=south.id, physical=6),
                        Stock(product_id=products[1].id, warehouse_id=north.id, physical=5),
                        Stock(product_id=products[2].id, warehouse_id=south.id, physical=4)])
            db.commit()

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@asynccontextmanager
async def lifespan(app: FastAPI):
    startup()
    yield

app.router.lifespan_context = lifespan

def get_stock(db: Session, product_id: int, warehouse_id: int, lock: bool = False) -> Stock:
    stmt = select(Stock).where(Stock.product_id == product_id, Stock.warehouse_id == warehouse_id)
    if lock and engine.dialect.name != "sqlite": stmt = stmt.with_for_update()
    stock = db.scalar(stmt)
    if stock is None:
        stock = Stock(product_id=product_id, warehouse_id=warehouse_id, physical=0, reserved_fbs=0)
        db.add(stock); db.flush()
    return stock

def check_refs(db: Session, product_id: int, warehouse_id: int | None = None):
    if db.get(Product, product_id) is None: raise HTTPException(404, "Артикул не найден")
    if warehouse_id is not None:
        w = db.get(Warehouse, warehouse_id)
        if w is None or not w.is_active: raise HTTPException(404, "Активный склад не найден")

def start_operation(db: Session, key: str, action: str):
    old = db.scalar(select(Operation).where(Operation.idempotency_key == key))
    if old: return old, True
    op = Operation(idempotency_key=key, action=action)
    db.add(op); db.flush()
    return op, False

def log_move(db: Session, op: Operation, product_id: int, qty: int, action: str,
             source: int | None = None, destination: int | None = None, note: str = ""):
    db.add(Movement(product_id=product_id, quantity=qty, action=action,
                    from_warehouse_id=source, to_warehouse_id=destination,
                    note=note, operation_id=op.id))

def finish(db: Session, op: Operation, details: str):
    op.details = details
    db.commit()
    return {"ok": True, "operation_id": op.id, "message": details}

def duplicate(op: Operation):
    return {"ok": True, "duplicate": True, "operation_id": op.id, "message": "Операция уже была обработана; повторного списания нет."}

@app.get("/api/overview")
def overview(db: Session = Depends(get_db)):
    products = db.scalars(select(Product).order_by(Product.sku)).all()
    warehouses = db.scalars(select(Warehouse).order_by(Warehouse.id)).all()
    rows = []
    for p in products:
        for w in warehouses:
            s = db.scalar(select(Stock).where(Stock.product_id == p.id, Stock.warehouse_id == w.id))
            rows.append({"product_id": p.id, "sku": p.sku, "product": p.name, "warehouse_id": w.id,
                         "warehouse": w.name, "warehouse_active": w.is_active,
                         "physical": s.physical if s else 0, "reserved": s.reserved_fbs if s else 0,
                         "available": s.available if s else 0})
    shipments = db.scalars(select(Shipment).where(Shipment.status == "IN_TRANSIT").order_by(Shipment.created_at.desc())).all()
    transit = [{"id": x.id, "kind": x.kind, "sku": x.product.sku, "product": x.product.name, "quantity": x.quantity,
                "source": x.source.name if x.source else "—", "destination": x.destination.name if x.destination else ("Клиент" if x.kind == "FBS" else "Маркетплейс"),
                "status": x.status, "note": x.note} for x in shipments]
    return {"products": [{"id": p.id, "sku": p.sku, "name": p.name} for p in products],
            "warehouses": [{"id": w.id, "name": w.name, "active": w.is_active} for w in warehouses],
            "stocks": rows, "shipments": transit}

@app.get("/api/history")
def history(limit: int = 100, db: Session = Depends(get_db)):
    records = db.scalars(select(Movement).order_by(Movement.created_at.desc()).limit(min(max(limit, 1), 300))).all()
    return [{"id": m.id, "date": m.created_at.isoformat(), "sku": m.product.sku, "product": m.product.name,
             "quantity": m.quantity, "action": m.action,
             "source": m.source.name if m.source else "—",
             "destination": m.destination.name if m.destination else ("Клиент" if "FBS клиенту" in m.action else ("Маркетплейс" if "FBO" in m.action else "—")), "note": m.note}
            for m in records]

@app.get("/api/warehouses")
def warehouses(db: Session = Depends(get_db)):
    return [{"id": w.id, "name": w.name, "active": w.is_active} for w in db.scalars(select(Warehouse).order_by(Warehouse.id)).all()]

@app.post("/api/warehouses")
def create_warehouse(data: WarehouseCreate, db: Session = Depends(get_db)):
    name = data.name.strip()
    if not name: raise HTTPException(400, "Название склада обязательно")
    w = Warehouse(name=name); db.add(w)
    try: db.commit()
    except IntegrityError: db.rollback(); raise HTTPException(409, "Склад с таким названием уже существует")
    return {"ok": True, "id": w.id, "name": w.name}

@app.delete("/api/warehouses/{warehouse_id}")
def archive_warehouse(warehouse_id: int, db: Session = Depends(get_db)):
    w = db.get(Warehouse, warehouse_id)
    if w is None: raise HTTPException(404, "Склад не найден")
    if not w.is_active: return {"ok": True, "message": "Склад уже архивирован; остатки и история сохранены."}
    # Soft delete: stock and movement references remain available for audit.
    w.is_active = False; db.commit()
    return {"ok": True, "message": "Склад архивирован. Остатки и история сохранены."}

@app.post("/api/inbound")
def inbound(data: InboundRequest, db: Session = Depends(get_db)):
    try:
        check_refs(db, data.product_id, data.warehouse_id); op, dup = start_operation(db, data.idempotency_key, "INBOUND")
        if dup: db.rollback(); return duplicate(op)
        s = get_stock(db, data.product_id, data.warehouse_id, True); s.physical += data.quantity
        log_move(db, op, data.product_id, data.quantity, "Приход", destination=data.warehouse_id)
        return finish(db, op, f"Приход: +{data.quantity} ед. на склад.")
    except Exception: db.rollback(); raise

@app.post("/api/transfers")
def transfer(data: TransferRequest, db: Session = Depends(get_db)):
    try:
        if data.source_warehouse_id == data.destination_warehouse_id: raise HTTPException(400, "Выберите разные склады")
        check_refs(db, data.product_id, data.source_warehouse_id); check_refs(db, data.product_id, data.destination_warehouse_id)
        op, dup = start_operation(db, data.idempotency_key, "TRANSFER")
        if dup: db.rollback(); return duplicate(op)
        source = get_stock(db, data.product_id, data.source_warehouse_id, True)
        if source.available < data.quantity: raise HTTPException(409, f"Недостаточно доступного товара на складе. Доступно: {source.available}")
        source.physical -= data.quantity
        shipment = Shipment(kind="TRANSFER", product_id=data.product_id, quantity=data.quantity,
                           source_warehouse_id=data.source_warehouse_id, destination_warehouse_id=data.destination_warehouse_id,
                           note="Перемещение между ФФ")
        db.add(shipment); log_move(db, op, data.product_id, data.quantity, "Отправка перемещения", data.source_warehouse_id, data.destination_warehouse_id)
        db.flush(); return finish(db, op, f"Перемещение №{shipment.id} отправлено. Товар числится в пути.")
    except Exception: db.rollback(); raise

@app.post("/api/fbs/reserve")
def fbs_reserve(data: FbsReserveRequest, db: Session = Depends(get_db)):
    try:
        check_refs(db, data.product_id, data.warehouse_id); op, dup = start_operation(db, data.idempotency_key, "FBS_RESERVE")
        if dup: db.rollback(); return duplicate(op)
        s = get_stock(db, data.product_id, data.warehouse_id, True)
        if s.available < data.quantity: raise HTTPException(409, f"Недостаточно доступного товара. Доступно: {s.available}")
        s.reserved_fbs += data.quantity
        sh = Shipment(kind="FBS", product_id=data.product_id, quantity=data.quantity, source_warehouse_id=data.warehouse_id,
                      status="RESERVED", note="Резерв FBS")
        db.add(sh); log_move(db, op, data.product_id, data.quantity, "Резерв FBS", source=data.warehouse_id)
        db.flush(); return finish(db, op, f"FBS резерв №{sh.id} создан. Товар заблокирован на ФФ.")
    except Exception: db.rollback(); raise

@app.post("/api/fbs/ship")
def fbs_ship(data: FbsActionRequest, db: Session = Depends(get_db)):
    try:
        op, dup = start_operation(db, data.idempotency_key, "FBS_SHIP")
        if dup: db.rollback(); return duplicate(op)
        sh = db.get(Shipment, data.shipment_id)
        if sh is None or sh.kind != "FBS" or sh.status != "RESERVED": raise HTTPException(409, "Активный резерв FBS не найден")
        s = get_stock(db, sh.product_id, sh.source_warehouse_id, True)
        if s.reserved_fbs < sh.quantity or s.physical < sh.quantity: raise HTTPException(409, "Нарушен баланс резерва; отправка отменена")
        s.reserved_fbs -= sh.quantity; s.physical -= sh.quantity
        sh.status = "IN_TRANSIT"; sh.note = "Отправлено клиенту"
        log_move(db, op, sh.product_id, sh.quantity, "Отправка FBS клиенту", sh.source_warehouse_id)
        return finish(db, op, f"FBS №{sh.id} отправлен клиенту. Товар числится в пути.")
    except Exception: db.rollback(); raise

@app.post("/api/fbs/deliver")
def fbs_deliver(data: FbsActionRequest, db: Session = Depends(get_db)):
    try:
        op, dup = start_operation(db, data.idempotency_key, "FBS_DELIVER")
        if dup:
            db.rollback()
            return duplicate(op)

        sh = db.get(Shipment, data.shipment_id)
        if sh is None or sh.kind != "FBS" or sh.status != "IN_TRANSIT":
            raise HTTPException(409, "Отправление FBS не найдено или уже завершено")

        sh.status = "DELIVERED"
        sh.completed_at = datetime.now(timezone.utc)
        log_move(
            db, op, sh.product_id, sh.quantity,
            "Доставка FBS клиенту",
            source=sh.source_warehouse_id,
            note="Доставка подтверждена сотрудником"
        )
        return finish(db, op, f"Заказ FBS №{sh.id} отмечен как доставленный.")
    except Exception:
        db.rollback()
        raise


@app.post("/api/fbs/cancel")
def fbs_cancel(data: FbsActionRequest, db: Session = Depends(get_db)):
    try:
        op, dup = start_operation(db, data.idempotency_key, "FBS_CANCEL")
        if dup: db.rollback(); return duplicate(op)
        sh = db.get(Shipment, data.shipment_id)
        if sh is None or sh.kind != "FBS" or sh.status != "RESERVED": raise HTTPException(409, "Активный резерв FBS не найден")
        s = get_stock(db, sh.product_id, sh.source_warehouse_id, True)
        if s.reserved_fbs < sh.quantity: raise HTTPException(409, "Резерв не сходится с остатками")
        s.reserved_fbs -= sh.quantity; sh.status = "CANCELLED"; sh.completed_at = datetime.now(timezone.utc)
        log_move(db, op, sh.product_id, sh.quantity, "Отмена резерва FBS", source=sh.source_warehouse_id)
        return finish(db, op, f"Резерв FBS №{sh.id} отменён. Товар снова доступен.")
    except Exception: db.rollback(); raise

@app.post("/api/fbo/dispatch")
def fbo_dispatch(data: FboRequest, db: Session = Depends(get_db)):
    try:
        check_refs(db, data.product_id, data.warehouse_id); op, dup = start_operation(db, data.idempotency_key, "FBO_DISPATCH")
        if dup: db.rollback(); return duplicate(op)
        selected = get_stock(db, data.product_id, data.warehouse_id, True)
        available = selected.available
        allocations = []
        if available >= data.quantity:
            allocations = [(data.warehouse_id, data.quantity)]
        elif data.auto_allocate:
            need = data.quantity
            candidates_stmt = select(Stock).join(Warehouse).where(
                Stock.product_id == data.product_id, Stock.warehouse_id != data.warehouse_id,
                Warehouse.is_active == True).order_by(Stock.physical.desc())
            if engine.dialect.name != "sqlite": candidates_stmt = candidates_stmt.with_for_update()
            candidates = db.scalars(candidates_stmt).all()
            take_selected = min(selected.available, need)
            if take_selected > 0: allocations.append((data.warehouse_id, take_selected)); need -= take_selected
            for candidate in candidates:
                take = min(candidate.available, need)
                if take > 0: allocations.append((candidate.warehouse_id, take)); need -= take
                if need == 0: break
            if need > 0:
                total = selected.available + sum(x.available for x in candidates)
                raise HTTPException(409, f"Общего доступного остатка недостаточно. Доступно: {total}, требуется: {data.quantity}")
        else:
            total = selected.available
            raise HTTPException(409, f"На выбранном ФФ не хватает товара: доступно {total}, требуется {data.quantity}. Включите распределение по другим ФФ.")
        shipment_ids = []
        for wh_id, qty in allocations:
            s = get_stock(db, data.product_id, wh_id, True)
            if s.available < qty: raise HTTPException(409, "Остаток изменился во время операции; повторите запрос")
            s.physical -= qty
            sh = Shipment(kind="FBO", product_id=data.product_id, quantity=qty, source_warehouse_id=wh_id,
                          note="Отправка на склад маркетплейса")
            db.add(sh); db.flush(); shipment_ids.append(sh.id)
            log_move(db, op, data.product_id, qty, "Отправка FBO", source=wh_id, note=f"Поставка FBO №{sh.id}")
        return finish(db, op, f"FBO отправка создана: {data.quantity} ед. в {len(allocations)} отправке(ах). ID: {', '.join(map(str, shipment_ids))}.")
    except Exception: db.rollback(); raise

@app.post("/api/shipments/receive")
def receive_shipment(data: ShipmentActionRequest, db: Session = Depends(get_db)):
    try:
        op, dup = start_operation(db, data.idempotency_key, "SHIPMENT_RECEIVE")
        if dup: db.rollback(); return duplicate(op)
        sh = db.get(Shipment, data.shipment_id)
        if sh is None or sh.status != "IN_TRANSIT" or sh.kind not in ("TRANSFER", "FBO"):
            raise HTTPException(409, "Отправка не найдена или уже принята")
        if sh.kind == "TRANSFER":
            dest = db.get(Warehouse, sh.destination_warehouse_id)
            if dest is None: raise HTTPException(409, "Склад-получатель не найден")
            target = get_stock(db, sh.product_id, sh.destination_warehouse_id, True)
            target.physical += sh.quantity
            log_move(db, op, sh.product_id, sh.quantity, "Приёмка перемещения", sh.source_warehouse_id, sh.destination_warehouse_id)
            message = f"Перемещение №{sh.id} принято на склад {dest.name}."
        else:
            log_move(db, op, sh.product_id, sh.quantity, "Приёмка FBO маркетплейсом", source=sh.source_warehouse_id, note="Принято маркетплейсом")
            message = f"FBO отправка №{sh.id} принята маркетплейсом."
        sh.status = "RECEIVED"; sh.completed_at = datetime.now(timezone.utc)
        return finish(db, op, message)
    except Exception: db.rollback(); raise

@app.get("/api/reservations")
def reservations(db: Session = Depends(get_db)):
    items = db.scalars(select(Shipment).where(Shipment.kind == "FBS", Shipment.status == "RESERVED").order_by(Shipment.created_at.desc())).all()
    return [{"id": x.id, "sku": x.product.sku, "product": x.product.name, "quantity": x.quantity,
             "warehouse": x.source.name if x.source else "—", "warehouse_id": x.source_warehouse_id} for x in items]

@app.get("/api/health")
def health(): return {"status": "ok"}
