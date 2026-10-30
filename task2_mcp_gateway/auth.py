import secrets

TOKEN_ROLES = {
    "viewer-token": "viewer",
    "admin-token": "admin",
}


def role_for(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    for known, role in TOKEN_ROLES.items():
        if secrets.compare_digest(token.encode(), known.encode()):
            return role
    return None


def can_call_tool(role: str, tool_name: str) -> bool:
    if tool_name.lower().startswith("admin_"):
        return role == "admin"
    return role in ("viewer", "admin")
