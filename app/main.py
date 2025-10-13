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

ORDER_ID_REGEX = os.getenv("ORDER_ID_REGEX", r"^\d{4,6}$")
ORDER_ID_PATTERN = re.compile(ORDER_ID_REGEX)

ENFORCE_IP_ALLOWLIST = os.getenv("ENFORCE_IP_ALLOWLIST", "true").lower() == "true"
ALLOWED_PROXY_IPS = set(
    (os.getenv("ALLOWED_PROXY_IPS") or "34.228.46.223,34.230.166.144")
    .replace(" ", "")
    .split(",")
)

C7_BASE_URL = os.getenv("C7_BASE_URL", "https://api.commerce7.com/v1")
C7_APP_ID = os.getenv("C7_APP_ID", "")
C7_APP_SECRET = os.getenv("C7_APP_SECRET", "")
C7_TENANT = os.getenv("C7_TENANT", "")
C7_TIMEOUT_S = float(os.getenv("C7_TIMEOUT_S", "3.0"))

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
        print("DEBUG: Incoming candidate IP:", candidate_ip)
        if candidate_ip not in ALLOWED_PROXY_IPS:
            print("DEBUG: IP not allowed:", candidate_ip)
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    return await call_next(request)

# -----------------------------
# Auth dependency
# -----------------------------
def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    print("DEBUG: Received x-api-key:", x_api_key)
    if not x_api_key or x_api_key != API_KEY:
        print("DEBUG: API key invalid or missing")
        raise HTTPException(status_code=401, detail=None)

# -----------------------------
# HTTP headers builders
# -----------------------------
def _c7_headers() -> Dict[str, str]:
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
    print("DEBUG: C7 request URL:", url, "params:", params)
    r = await client.get(url, headers=_c7_headers(), params=params, timeout=C7_TIMEOUT_S)
    print("DEBUG: C7 response status:", r.status_code)
    text = await r.text()
    print("DEBUG: C7 response body:", text)
    r.raise_for_status()
    return r.json()

async def fetch_c7_order_by_number_and_email(order_id: str, email: str) -> Optional[Dict[str, Any]]:
    order_no = _parse_order_number(order_id)
    print("DEBUG: parsed order_no:", order_no)
    if order_no is None:
        print("DEBUG: order_no is None")
        return None

    async with httpx.AsyncClient() as client:
        data = None
        try:
            print("DEBUG: calling /orders search")
            data = await _fetch_c7_json(client, "/orders", params={"q": str(order_no)})
        except Exception as e:
            print("DEBUG: exception during search:", e)

        orders = data.get("orders") if data else []
        print("DEBUG: orders list:", orders)

        match = next((o for o in orders if str(o.get("orderNumber")).isdigit() and int(o["orderNumber"]) == order_no), None)
        print("DEBUG: match from search:", match)
        if not match:
            print("DEBUG: no match, returning None")
            return None

        internal_id = match.get("id")
        print("DEBUG: internal_id:", internal_id)
        if internal_id is None:
            print("DEBUG: internal_id missing")
            return None

        full_order = None
        try:
            path = f"/orders/{internal_id}"
            print("DEBUG: fetching full order using path:", path)
            full_order = await _fetch_c7_json(client, path, params=None)
        except Exception as e:
            print("DEBUG: exception fetching full order:", e)
            full_order = None

        if not full_order:
            print("DEBUG: full_order None, returning None")
            return None

        cust_id = full_order.get("customerId")
        print("DEBUG: full_order customerId:", cust_id)
        if not cust_id:
            print("DEBUG: missing customerId")
            return None

        customer = None
        try:
            cust_path = f"/customers/{cust_id}"
            print("DEBUG: fetching customer using path:", cust_path)
            customer = await _fetch_c7_json(client, cust_path, params=None)
        except Exception as e:
            print("DEBUG: exception fetching customer:", e)
            return None

        emails = [e.get("email", "").lower() for e in (customer.get("emails") or [])]
        print("DEBUG: customer emails:", emails)
        if email.lower() not in emails:
            print("DEBUG: email mismatch")
            return None

        print("DEBUG: returning full_order")
        return full_order

async def fetch_ws_tracking(tracking_numbers: List[str]) -> Optional[Dict[str, Any]]:
    if not (WS_ENABLE and WS_API_KEY and tracking_numbers):
        return None
    url = WS_BASE_URL.rstrip("/") + "/openapi/tracking/getdetails"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=_ws_headers(), json={"trackingNumbers": tracking_numbers}, timeout=WS_TIMEOUT_S)
        print("DEBUG: WS request URL:", url, "payload trackingNumbers:", tracking_numbers)
        print("DEBUG: WS response status:", r.status_code)
        text = await r.text()
        print("DEBUG: WS response body:", text)
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
    except Exception as e:
        print("DEBUG: exception in _status_line_from_ws:", e)
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
    print("DEBUG: entering order_lookup with", order_id, customer_email)
    if not ORDER_ID_PATTERN.match(order_id.strip()):
        print("DEBUG: order_id pattern mismatch", order_id)
        return JSONResponse(status_code=404, content=NotFoundResponse(context=_context_not_found(order_id)).model_dump())

    try:
        c7_order = await fetch_c7_order_by_number_and_email(order_id, customer_email)
    except Exception as e:
        print("DEBUG: exception in fetch_c7_order_by_number_and_email:", e)
        c7_order = None

    if not c7_order:
        print("DEBUG: c7_order is None, returning Not Found")
        return JSONResponse(status_code=404, content=NotFoundResponse(context=_context_not_found(order_id)).model_dump())

    print("DEBUG: c7_order found:", c7_order)

    items = _extract_items_from_c7(c7_order)
    tracking_numbers, _carrier = _extract_tracking_from_c7(c7_order)

    status_line = _status_line_from_c7(c7_order)
    best_tracking_url: Optional[str] = None

    if WS_ENABLE and tracking_numbers:
        try:
            ws = await fetch_ws_tracking(tracking_numbers[:3])
            if ws:
                status_line, best_tracking_url = _status_line_from_ws(ws)
        except Exception as e:
            print("DEBUG: exception in WS tracking:", e)

    context = f"Order {order_id} found. Items: {_format_items(items)}. {status_line}"
    print("DEBUG: responding context:", context, "tracking_url:", best_tracking_url)
    return JSONResponse(status_code=200, content=FoundResponse(context=context, tracking_url=best_tracking_url).model_dump())
