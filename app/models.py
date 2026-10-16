from datetime import date, datetime
from pydantic import BaseModel, Field
from uuid import UUID

class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    phone: str = Field(min_length=3, max_length=30)

class WalletCreate(BaseModel):
    currency: str = "INR"
    name: str = Field(default="Main", min_length=1, max_length=100)

class TransferCreate(BaseModel):
    from_wallet_id: int
    to_wallet_id: int
    amount_paise: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=1000)

class TopupCreate(BaseModel):
    wallet_id: int
    amount_paise: int = Field(gt=0)

class WithdrawalCreate(BaseModel):
    wallet_id: int
    amount_paise: int = Field(gt=0)

class DecisionCreate(BaseModel):
    decision: str

class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

class UserOut(BaseModel):
    id: int
    name: str
    email: str
    phone: str
    kyc_status: str
    created_at: datetime
