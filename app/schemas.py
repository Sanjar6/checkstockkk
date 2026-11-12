from pydantic import BaseModel, Field

class WarehouseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)

class InboundRequest(BaseModel):
    product_id: int
    warehouse_id: int
    quantity: int = Field(gt=0, le=1_000_000)
    idempotency_key: str = Field(min_length=1, max_length=100)

class TransferRequest(BaseModel):
    product_id: int
    source_warehouse_id: int
    destination_warehouse_id: int
    quantity: int = Field(gt=0, le=1_000_000)
    idempotency_key: str = Field(min_length=1, max_length=100)

class FbsReserveRequest(BaseModel):
    product_id: int
    warehouse_id: int
    quantity: int = Field(gt=0, le=1_000_000)
    idempotency_key: str = Field(min_length=1, max_length=100)

class FbsActionRequest(BaseModel):
    shipment_id: int
    idempotency_key: str = Field(min_length=1, max_length=100)

class FboRequest(BaseModel):
    product_id: int
    warehouse_id: int
    quantity: int = Field(gt=0, le=1_000_000)
    auto_allocate: bool = False
    idempotency_key: str = Field(min_length=1, max_length=100)

class ShipmentActionRequest(BaseModel):
    shipment_id: int
    idempotency_key: str = Field(min_length=1, max_length=100)
