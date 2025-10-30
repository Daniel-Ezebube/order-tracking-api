# file: app/main.py
from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional, List, Tuple, Union

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr

# -----------------------------
# Config (env)
# -----------------------------
API_KEY = os.getenv("API_KEY", "change-me")

ORDER_ID_REGEX = os.getenv("ORDER_ID_REGEX", r"^\d{4,6}$")
ORDER_ID_PATTERN = re.compile(ORDER_ID_REGEX)

# Toggleable IP allowlist (set ENFORCE_IP_ALLOWLIST=false to disable)
ENFORCE_IP_ALLOWLIST = os.getenv("ENFORCE_IP_ALLOWLIST", "true").lower() == "true"
ALLOWED_PROXY_IPS = set(
    (os.getenv("ALLOWED_PROXY_IPS") or "34.228.46.223,34.230.166.144")
    .replace(" ", "")
    .split(",")
)

# Support contact to show customers
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "daniel@moduswines.com")

# --- Wineshipping (WS) only ---
WS_ENABLE = os.getenv("WS_ENABLE", "true").lower() == "true"
WS_BASE_URL = os.getenv("WS_BASE_URL", "https://api.wineshipping.com/v3")
WS_TIMEOUT_S = float(os.getenv("WS_TIMEOUT_S", "4.0"))

# Body-based auth fields (kept)
WS_USER_KEY = os.getenv("WS_USER_KEY", "")
WS_PASSWORD = os.getenv("WS_PASSWORD", "")
WS_CUSTOMER_NO = os.getenv("WS_CUSTOMER_NO", "")

# -----------------------------
# Schemas
# -----------------------------
class FoundResponse(BaseModel):
    context: str
    tracking_url: Optional[str] = None

class NotFoundResponse(BaseModel):
    context: str

# -----------------------------
# App
# -----------------------------
app = FastAPI(title="Order Tracking (Wineshipping only)", version="2.2.0")

# -----------------------------
# Middleware: IP allowlist (optional)
# -----------------------------
if ENFORCE_IP_ALLOWLIST:
    @app.middleware("http")
    async def ip_allowlist_middleware(request: Request, call_next):
        xff = request.headers.get("x-forwarded-for", "")
        chain = [ip.strip() for ip in xff.split(",") if ip.strip()]
        candidate_ip = chain[0] if chain else (request.client.host if request.client else "")
        if candidate_ip not in ALLOWED_PROXY_IPS:
            # Minimal logging — no sensitive data
            print(f"DEBUG: Rejected IP {candidate_ip}")
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        return await call_next(request)

# -----------------------------
# Auth dependency
# -----------------------------
def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    # Do not log secrets
    if not x_api_key or x_api_key != API_KEY:
        print("DEBUG: API key invalid or missing")  # safe message
        raise HTTPException(status_code=401, detail=None)

# -----------------------------
# Status mapping (customer-friendly)
# -----------------------------
FULFILLMENT_STATUS_MESSAGES: Dict[str, str] = {
    "RECEIVED": "Order received for fulfillment processing.",
    "ON INV HOLD": "Order is on inventory hold.",
    "ON WINERY REQUESTED HOLD": "Order is on winery hold.",
    "ON WEATHER HOLD": "Order is on hold due to weather conditions.",
    "ON CUSTOMER SERVICE HOLD": "Order is on Wineshipping customer service hold.",
    "PROCESSING ORDER": "Order is being prepared for shipment.",
    "CANCELED": "Order has been canceled.",
    "EXCEPTION": "There is an exception with this order. Check the details for more info.",
    "READY TO SHIP": "Package is ready for carrier pickup.",
    "READY FOR PICKUP": "Package is ready for Will Call pickup.",
    "SHIPPED": "Package has left the fulfillment facility.",
    "IN TRANSIT": "Package is with the carrier and in transit.",
    "DELIVERED": "Package was delivered.",
    "RETURNED": "Package is returning to the sender.",
    "DELIVERED TO SHIPPER": "Package returned to the sender.",
    "DAMAGED": "Carrier reported damage; shipment is still in transit.",
}

def _friendly_from_status_fields(d: Dict[str, Any]) -> str:
    """
    Try to derive a customer-friendly line from Wineshipping status fields.
    We look for a code-like field first, then fall back to text descriptions.
    """
    # Try typical fields that may contain a code
    code_candidates = [
        d.get("FulfillmentStatus"),
        d.get("StatusCode"),
        d.get("Status"),
        d.get("CarrierStatus"),
    ]
    code = next((c for c in code_candidates if isinstance(c, str)), None)
    friendly = None
    if code:
        key = code.strip().upper()
        friendly = FULFILLMENT_STATUS_MESSAGES.get(key)

    # Fallback to descriptions if mapping not found
    desc = d.get("StatusDescription") or d.get("Status") or d.get("CarrierStatus")
    if not friendly and isinstance(desc, str) and desc.strip():
        friendly = desc.strip()

    # Final fallback
    return friendly or "Order is on its way."

# -----------------------------
# WS helpers
# -----------------------------
def _ws_build_getdetails_body(order_no: str) -> Dict[str, Any]:
    # AuthenticationDetails in BODY should remain
    return {
        "AuthenticationDetails": {
            "UserKey": WS_USER_KEY,
            "Password": WS_PASSWORD,
            "CustomerNo": WS_CUSTOMER_NO,
        },
        "OrderNo": order_no
    }

def _status_line_and_url_from_ws_payload(payload: Union[List[Dict[str, Any]], Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    def pick(d: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        base = _friendly_from_status_fields(d)
        eta = d.get("EstimatedDeliveryDate") or d.get("EstimatedDelivery")
        url = d.get("TrackingURL") or d.get("TrackingUrl") or d.get("EmbeddedCarrierTrackingUrl")
        line = base
        if eta:
            line += f" Estimated delivery {eta}."
        return line, url

    if isinstance(payload, list):
        if not payload:
            return "Order is on its way; follow via the provided tracking link.", None
        return pick(payload[0])
    if isinstance(payload, dict):
        return pick(payload)
    return "Order is on its way; follow via the provided tracking link.", None

async def fetch_ws_getdetails(order_no: str) -> Optional[Union[List[Dict[str, Any]], Dict[str, Any]]]:
    if not (WS_ENABLE and order_no and WS_USER_KEY and WS_PASSWORD and WS_CUSTOMER_NO):
        print("DEBUG: WS disabled or missing credentials")
        return None

    url = WS_BASE_URL.rstrip("/") + "/api/Tracking/GetDetails"
    body = _ws_build_getdetails_body(order_no)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as client:
        # Avoid logging credentials or full payloads
        print(f"DEBUG: Calling WS GetDetails for order {order_no}")
        try:
            r = await client.post(url, headers=headers, json=body, timeout=WS_TIMEOUT_S)
        except Exception as e:
            print("DEBUG: WS request error:", e)
            raise
        print("DEBUG: WS response status:", r.status_code)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            print("DEBUG: WS response not JSON-decoded")
            return {"StatusDescription": "Received non-JSON response"}

# -----------------------------
# Health
# -----------------------------
@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}

# -----------------------------
# Main Endpoint (WS only)
# -----------------------------
@app.get(
    "/order-lookup",
    response_model=FoundResponse | NotFoundResponse,
    responses={
        200: {"model": FoundResponse, "description": "Found Order"},
        401: {"description": "Unauthorized"},
        404: {"model": NotFoundResponse, "description": "Order Not Found"},
    },
    tags=["order"],
)
async def order_lookup(
    order_id: str = Query(..., description="Order number (exactly 5 digits, e.g., 40500)"),
    customer_email: EmailStr = Query(..., description="Customer email (kept for compatibility; not used)"),
    _: Any = Depends(require_api_key),
):
    # Limit logging to non-PII
    print("DEBUG: entering order_lookup with order_id:", order_id)

    if not ORDER_ID_PATTERN.match(order_id.strip()):
        print("DEBUG: order_id pattern mismatch", order_id)
        return JSONResponse(
            status_code=404,
            content=NotFoundResponse(
                context=(
                    f"Order {order_id} not found with the provided details. "
                    f"Please double-check the order number or contact support for assistance. "
                    f"For more information, contact {SUPPORT_CONTACT}."
                )
            ).model_dump()
        )

    normalized = order_id.strip().lstrip("#")
    try:
        ws_payload = await fetch_ws_getdetails(normalized)
    except Exception as e:
        print("DEBUG: exception in fetch_ws_getdetails:", e)
        ws_payload = None

    if not ws_payload:
        print("DEBUG: ws_payload is None; Not Found")
        return JSONResponse(
            status_code=404,
            content=NotFoundResponse(
                context=(
                    f"Order {order_id} not found with the provided details. "
                    f"Please double-check the order number or contact support for assistance. "
                    f"For more information, contact {SUPPORT_CONTACT}."
                )
            ).model_dump()
        )

    status_line, tracking_url = _status_line_and_url_from_ws_payload(ws_payload)
    # Keep copy tight; itemization not available without Commerce7.
    context = (
        f"Order {order_id} found. {status_line} "
        f"For more information, contact {SUPPORT_CONTACT}."
    )
    print("DEBUG: responding 200 for order_id:", order_id)
    return JSONResponse(
        status_code=200,
        content=FoundResponse(context=context, tracking_url=tracking_url).model_dump()
    )
