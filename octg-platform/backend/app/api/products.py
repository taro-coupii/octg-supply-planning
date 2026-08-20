from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Product
from app.schemas import ProductOut

router = APIRouter()


def _out(p: Product) -> ProductOut:
    return ProductOut(
        id=p.id, name=p.name, unit_of_measure=p.unit_of_measure.value, weight_kg=p.weight_kg
    )


@router.get("/products", response_model=list[ProductOut])
def list_products(db: Session = Depends(get_db)):
    return [_out(p) for p in db.query(Product).order_by(Product.name).all()]


@router.get("/products/{product_id}", response_model=ProductOut)
def get_product(product_id: str, db: Session = Depends(get_db)):
    p = db.get(Product, product_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return _out(p)
