from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from backend.app.core.company_lifecycle import portal_access_allowed
from backend.app.core.config.settings import settings
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import (
    CUSTOMER_ROLES,
    SESSION_COOKIE_NAME,
    get_current_user,
    require_customer_user,
)
from backend.app.core.mail import send_password_reset_code
from backend.app.core.password_policy import validate_password
from backend.app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.models.password_reset import PasswordResetCode
from backend.app.models.user import User


router = APIRouter(prefix="/auth", tags=["Authentication"])


class LoginRequest(BaseModel):
    email: str
    password: str


class SignupRequest(BaseModel):
    full_name: str
    email: str
    password: str
    workspace_name: str | None = None


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def _utcnow_naive() -> datetime:
    """Return UTC without tzinfo for compatibility with existing naive DB timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max(60, settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60),
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=settings.is_production,
        httponly=True,
        samesite="lax",
    )


def _auth_payload(user: User, company: Company | None = None) -> dict:
    return {
        "id": user.id,
        "company_id": user.company_id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "onboarding_source": getattr(company, "onboarding_source", None),
    }


@router.post("/login")
def login(data: LoginRequest, response: Response):
    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.email == data.email.strip().lower())
            .first()
        )
        if user is None or not user.active or not verify_password(
            data.password, user.password_hash
        ):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        company = None
        if user.company_id is not None:
            company = db.query(Company).filter(Company.id == user.company_id).first()
            if company is None:
                raise HTTPException(status_code=403, detail="Company not found")
            if user.role in CUSTOMER_ROLES and not portal_access_allowed(company):
                raise HTTPException(status_code=403, detail="Company portal access is suspended")

        token = create_access_token(user.id, user.token_version)
        _set_session_cookie(response, token)
        return {
            "access_token": token,
            "token_type": "bearer",
            "user": _auth_payload(user, company),
        }
    finally:
        db.close()


@router.post("/refresh")
def refresh_session(
    response: Response,
    current_user: User = Depends(get_current_user),
):
    token = create_access_token(current_user.id, current_user.token_version)
    _set_session_cookie(response, token)
    return {"status": "refreshed"}


def _validate_new_password(password: str):
    try:
        validate_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _hash_reset_code(email: str, code: str) -> str:
    message = (email.lower().strip() + ":" + code).encode("utf-8")
    return hmac.new(
        settings.JWT_SECRET.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


@router.post("/customer/forgot-password")
def customer_forgot_password(data: ForgotPasswordRequest):
    db = SessionLocal()
    generic_response = {
        "status": "accepted",
        "message": (
            "If the email belongs to a customer account, a verification code will be sent."
        ),
    }
    try:
        email = data.email.strip().lower()
        user = db.query(User).filter(User.email == email).first()
        if user is None or user.company_id is None or user.role not in CUSTOMER_ROLES:
            return generic_response

        latest = (
            db.query(PasswordResetCode)
            .filter(
                PasswordResetCode.user_id == user.id,
                PasswordResetCode.used_at.is_(None),
            )
            .order_by(PasswordResetCode.id.desc())
            .first()
        )
        now = _utcnow_naive()
        if latest is not None and (now - latest.created_at).total_seconds() < 60:
            return generic_response

        old_codes = (
            db.query(PasswordResetCode)
            .filter(
                PasswordResetCode.user_id == user.id,
                PasswordResetCode.used_at.is_(None),
            )
            .all()
        )
        for item in old_codes:
            item.used_at = now

        code = f"{secrets.randbelow(1000000):06d}"
        reset = PasswordResetCode(
            user_id=user.id,
            email=email,
            code_hash=_hash_reset_code(email, code),
            attempts=0,
            expires_at=now + timedelta(minutes=10),
        )
        db.add(reset)
        db.commit()

        try:
            send_password_reset_code(email, code)
        except Exception:
            reset.used_at = _utcnow_naive()
            db.commit()
            raise HTTPException(status_code=503, detail="Email service is unavailable")
        return generic_response
    finally:
        db.close()


@router.post("/customer/reset-password")
def customer_reset_password(data: ResetPasswordRequest, response: Response):
    _validate_new_password(data.new_password)
    db = SessionLocal()
    try:
        email = data.email.strip().lower()
        user = db.query(User).filter(User.email == email).first()
        if user is None or user.company_id is None or user.role not in CUSTOMER_ROLES:
            raise HTTPException(status_code=400, detail="Invalid or expired code")

        reset = (
            db.query(PasswordResetCode)
            .filter(
                PasswordResetCode.user_id == user.id,
                PasswordResetCode.used_at.is_(None),
            )
            .order_by(PasswordResetCode.id.desc())
            .first()
        )
        now = _utcnow_naive()
        if reset is None or reset.expires_at < now or reset.attempts >= 5:
            raise HTTPException(status_code=400, detail="Invalid or expired code")

        reset.attempts += 1
        expected = _hash_reset_code(email, data.code.strip())
        if not hmac.compare_digest(expected, reset.code_hash):
            db.commit()
            raise HTTPException(status_code=400, detail="Invalid or expired code")

        user.password_hash = hash_password(data.new_password)
        user.token_version += 1
        reset.used_at = now
        db.commit()
        _clear_session_cookie(response)
        return {"status": "password_changed"}
    finally:
        db.close()


@router.post("/customer/change-password")
def customer_change_password(
    data: ChangePasswordRequest,
    response: Response,
    current_user: User = Depends(require_customer_user),
):
    _validate_new_password(data.new_password)
    if data.current_password == data.new_password:
        raise HTTPException(status_code=400, detail="New password must be different")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == current_user.id).first()
        if user is None or not verify_password(data.current_password, user.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        user.password_hash = hash_password(data.new_password)
        user.token_version += 1
        db.commit()
        _clear_session_cookie(response)
        return {"status": "password_changed"}
    finally:
        db.close()


@router.post("/logout")
def logout(response: Response):
    _clear_session_cookie(response)
    return {"status": "logged_out"}
