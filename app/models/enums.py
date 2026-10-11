import enum


class OrderStatus(str, enum.Enum):
    pending_payment = "pending_payment"
    paid = "paid"
    delivered = "delivered"
    completed = "completed"
    cancelled = "cancelled"


class GiftCardStatus(str, enum.Enum):
    available = "available"
    reserved = "reserved"
    sold = "sold"
    blocked = "blocked"
