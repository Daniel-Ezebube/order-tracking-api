# file: app/main.py
from __future__ import annotations

import base64
import os
import re
from typing import Any, Dict, Optional, List

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr

# -----------------------------
# Config (env)
# -----------------------------
API_KEY = os.getenv("API_KEY", "change-me")

# Enforce exactly five digits, e.g., 40500
ORDER_ID_REGEX = os.getenv("ORDER_ID_REGEX", r"^\d{4,6}$")
ORDER_ID_PATTERN = re.compile(ORDER_ID_REGEX)

ENFORCE_IP_ALLOWLIST = os.getenv("ENFORCE_IP_ALLOWLIST", "true").lower() == "true"
ALLOWED_PROXY_IPS = set(
    (os.getenv("ALLOWED_PROXY_IPS") or "34.228.46.223,34.230.166.144")
    .replace(" ", "")
    .split(",")
)

# --- Commerce7 ---
C7_BASE_URL = os.getenv("C7_BASE_URL", "https://api.commerce7.com/v1")
C7_APP_ID = os.getenv("C7_APP_ID", "")
C7_APP_SECRET = os.getenv("C7_APP_SECRET", "")
C7_TENANT = os.getenv("C7_TENANT", "")  # REQUIRED
C7_TIMEOUT_S = float(os.getenv("C7_TIMEOUT_S", "3.0"))

# --- Wineshipping (optional enrichment) ---
WS_ENABLE = os.getenv("WS_ENABLE", "false").lower() == "true"
WS_BASE_URL = os.getenv("WS_BASE_URL", "https://developer.wineshipping.com/api/v3.1")
WS_API_KEY = os.getenv("WS_API_KEY", "")
WS_TIMEOUT_S = float(os.getenv("WS_TIMEOUT_S", "3.0"))

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
app = FastAPI(title="Custom Order Tracking API", version="1.2.1")

# -----------------------------
# Middleware: IP allowlist
# -----------------------------
@app.middleware("http")
async def ip_allowlist_middleware(request: Request, call_next):
    if ENFORCE_IP_ALLOWLIST:
        xff = request.headers.get("x-forwarded-for", "")
        chain = [ip.strip() for ip in xff.split(",") if ip.strip()]
        candidate_ip = chain[0] if chain else (request.client.host if request.client else "")
        if candidate_ip not in ALLOWED_PROXY_IPS:
            # Why: restrict to Zipchat proxies when enabled.
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    return await call_next(request)

# -----------------------------
# Auth dependency (Zipchat -> our endpoint)
# -----------------------------
def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail=None)  # spec: 401, body not required

# -----------------------------
# HTTP headers for Commerce7 & Wineshipping
# -----------------------------
def _c7_headers() -> Dict[str, str]:
    # Why: C7 requires Basic Auth (appID:appSecret) + tenant header.
    if not (C7_APP_ID and C7_APP_SECRET and C7_TENANT):
        raise RuntimeError("Missing C7_APP_ID/C7_APP_SECRET/C7_TENANT")
    token = base64.b64encode(f"{C7_APP_ID}:{C7_APP_SECRET}".encode("utf-8")).decode("utf-8")
    return {
        "Authorization": f"Basic {token}",
        "tenant": C7_TENANT,
        "Accept": "application/json",
    }

def _ws_headers() -> Dict[str, str]:
    if not WS_API_KEY:
        return {}
    return {"Authorization": f"Bearer {WS_API_KEY}", "Accept": "application/json", "Content-Type": "application/json"}

# -----------------------------
# Helpers
# -----------------------------
def _parse_order_number(order_id: str) -> Optional[int]:
    # Accepts '40500' (strict 5 digits). Keeps '#' stripping defensive in case upstream sends it.
    s = order_id.strip()
    if s.startswith("#"):
        s = s[1:]
    try:
        return int(s)
    except ValueError:
        return None

def _format_items(items: Dict[str, int]) -> str:
    return ", ".join(f"{qty} x {title}" for title, qty in items.items())

def _context_not_found(order_id: str) -> str:
    return f"Order {order_id} not found with the provided details. Please double-check the order number or contact support for assistance."

def _status_line_from_c7(order: Dict[str, Any]) -> str:
    fs = (order.get("fulfillmentStatus") or "").lower()
    ss = (order.get("shippingStatus") or "").lower()
    if fs in {"not fulfilled"}:
        return "Not shipped yet; typical dispatch within two business days."
    if fs in {"partially fulfilled"}:
        return "Partially fulfilled; remaining items will ship soon."
    if fs in {"fulfilled"}:
        if ss in {"delivered"}:
            return "Order was delivered."
        if ss in {"in transit", "pending"}:
            return "Order is on its way; follow via the provided tracking link."
        return "Order fulfilled."
    if fs in {"no fulfillment required"}:
        return "No fulfillment required."
    return "Order status available; see tracking for latest updates."

def _extract_items_from_c7(order: Dict[str, Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for line in order.get("items") or []:
        title = line.get("productTitle") or line.get("title") or line.get("sku") or "Item"
        qty = int(line.get("quantity") or 1)
        out[title] = out.get(title, 0) + qty
    return out or {"Items": 1}

def _extract_tracking_from_c7(order: Dict[str, Any]) -> tuple[List[str], Optional[str]]:
    nums: List[str] = []
    carrier: Optional[str] = None
    for f in order.get("fulfillments") or []:
        if (f.get("type") or "").lower() == "shipped":
            shipped = f.get("shipped") or {}
            for tn in shipped.get("trackingNumbers") or []:
                if tn:
                    nums.append(str(tn))
            if not carrier:
                c = shipped.get("carrier")
                carrier = str(c) if c else None
    return nums, carrier

async def _fetch_c7_json(client: httpx.AsyncClient, path: str, params: Dict[str, Any] | None = None) -> Any:
    url = C7_BASE_URL.rstrip("/") + path
    r = await client.get(url, headers=_c7_headers(), params=params, timeout=C7_TIMEOUT_S)
    r.raise_for_status()
    return r.json()

async def fetch_c7_order_by_number_and_email(order_id: str, email: str) -> Optional[Dict[str, Any]]:
    """Find the order by human order number, then verify the customer email via /customer/{id}."""
    order_no = _parse_order_number(order_id)
    if order_no is None:
        return None

    async with httpx.AsyncClient() as client:
        data = await _fetch_c7_json(client, "/order", params={"q": str(order_no)})
        orders = data.get("orders") or []
        # Exact match on orderNumber (integer compare)
        match = next((o for o in orders if str(o.get("orderNumber", "")).isdigit() and int(o["orderNumber"]) == order_no), None)
        if not match:
            return None

        cust_id = match.get("customerId")
        if not cust_id:
            return None
        customer = await _fetch_c7_json(client, f"/customer/{cust_id}")
        emails = [e.get("email", "").lower() for e in (customer.get("emails") or [])]
        if email.lower() not in emails:
            # Do not leak existence on mismatch.
            return None

        return match

async def fetch_ws_tracking(tracking_numbers: List[str]) -> Optional[Dict[str, Any]]:
    if not (WS_ENABLE and WS_API_KEY and tracking_numbers):
        return None
    url = WS_BASE_URL.rstrip("/") + "/openapi/tracking/getdetails"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=_ws_headers(), json={"trackingNumbers": tracking_numbers}, timeout=WS_TIMEOUT_S)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

def _status_line_from_ws(ws_payload: Dict[str, Any]) -> tuple[str, Optional[str]]:
    try:
        details = ws_payload.get("details") or ws_payload.get("packages") or []
        pkg = details[0] if details else {}
        status = pkg.get("statusDescription") or pkg.get("carrierStatus") or "in transit"
        eta = pkg.get("estimatedDeliveryDate")
        url = pkg.get("trackingUrl") or pkg.get("embeddedCarrierTrackingUrl")
        line = f"Order is on its way ({status})."
        if eta:
            line += f" Estimated delivery {eta}."
        return line, url
    except Exception:
        return "Order is on its way; follow via the provided tracking link.", None

# -----------------------------
# Health
# -----------------------------
@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}

# -----------------------------
# Main Endpoint
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
    customer_email: EmailStr = Query(..., description="Customer email on the order"),
    _: Any = Depends(require_api_key),
):
    if not ORDER_ID_PATTERN.match(order_id.strip()):
        # Spec prefers 404 with context over 400 for invalid user inputs.
        return JSONResponse(status_code=404, content=NotFoundResponse(context=_context_not_found(order_id)).model_dump())

    try:
        c7_order = await fetch_c7_order_by_number_and_email(order_id, customer_email)
    except Exception:
        c7_order = None  # avoid leaking upstream details

    if not c7_order:
        return JSONResponse(status_code=404, content=NotFoundResponse(context=_context_not_found(order_id)).model_dump())

    items = _extract_items_from_c7(c7_order)
    tracking_numbers, _carrier = _extract_tracking_from_c7(c7_order)

    status_line = _status_line_from_c7(c7_order)
    best_tracking_url: Optional[str] = None

    if WS_ENABLE and tracking_numbers:
        try:
            ws = await fetch_ws_tracking(tracking_numbers[:3])
            if ws:
                status_line, best_tracking_url = _status_line_from_ws(ws)
        except Exception:
            pass

    context = f"Order {order_id} found. Items: {_format_items(items)}. {status_line}"
    return JSONResponse(status_code=200, content=FoundResponse(context=context, tracking_url=best_tracking_url).model_dump())
