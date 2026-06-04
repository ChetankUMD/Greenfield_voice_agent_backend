"""
Greenfield Cardiology Voice Agent — FastAPI application entry point.

Startup sequence:
  1. Create all tables (idempotent via CREATE TABLE IF NOT EXISTS)
  2. Seed appointment slots and the test referral
  3. Start the 60-second referral-retry scheduler
"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import engine
from .models import Base
from .seed import seed
from .scheduler import start_scheduler
from .db import SessionLocal
from .routes import health, admin, tools, referral, vapi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ready.")

    # Seed
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()

    # Start scheduler
    scheduler = start_scheduler()

    yield

    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped.")


app = FastAPI(
    title="Greenfield Cardiology Voice Agent Backend",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(admin.router)
app.include_router(tools.router)
app.include_router(referral.router)
app.include_router(vapi.router)
