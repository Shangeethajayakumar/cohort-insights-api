from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.scripts.core.services.documents import DocumentService

bearer = HTTPBearer(auto_error=False)


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> str:
    user_id = None
    if credentials is not None:
        user_id = request.app.state.settings.api_keys.get(credentials.credentials)
    if user_id is None:
        raise HTTPException(
            401, "Valid bearer API key required", headers={"WWW-Authenticate": "Bearer"}
        )
    return user_id


def document_service(request: Request) -> DocumentService:
    return request.app.state.service
