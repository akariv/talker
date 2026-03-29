"""Client authentication via X-Client-Name + X-Api-Key headers."""

from fastapi import Request, HTTPException
import db


async def authenticate(request: Request) -> str:
    """Validate client headers, return client_name. Raises 401/403 on failure."""
    client_name = request.headers.get("X-Client-Name")
    api_key = request.headers.get("X-Api-Key")

    if not client_name or not api_key:
        raise HTTPException(status_code=401, detail="Missing X-Client-Name or X-Api-Key")

    client = db.get_client(client_name)
    if not client or client.get("api_key") != api_key:
        raise HTTPException(status_code=403, detail="Invalid credentials")

    return client_name
