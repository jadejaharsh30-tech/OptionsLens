# optionslens/backend/auth.py
"""
Fyers token management.
Token is passed via Authorization header on each request.
No token stored to disk — stateless by design.
"""
from fastapi import Header, HTTPException
from fyers_client import get_fyers


def get_token(authorization: str = Header(...)) -> str:
    """
    FastAPI dependency. Extracts Bearer token from Authorization header.
    Usage in router: token: str = Depends(get_token)
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header must be 'Bearer <token>'"
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")
    return token


def get_fyers_client(authorization: str = Header(...)):
    """
    FastAPI dependency. Returns a ready FyersModel instance.
    Usage in router: fyers = Depends(get_fyers_client)
    """
    token = get_token(authorization)
    return get_fyers(token)
