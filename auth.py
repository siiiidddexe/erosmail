import bcrypt
# Passlib bcrypt monkey-patch to prevent "AttributeError: module 'bcrypt' has no attribute '__about__'"
if not hasattr(bcrypt, "__about__"):
    class About:
        __version__ = bcrypt.__version__
    bcrypt.__about__ = About()

from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from database import get_db
from models import User
import os
from functools import wraps

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key-change-in-production")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def get_user(db: Session, username: str):
    return db.query(User).filter(User.username == username).first()

def authenticate_user(db: Session, username: str, password: str):
    user = get_user(db, username)
    if not user:
        return False
    if not verify_password(password, user.password_hash):
        return False
    return user

def get_current_user(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")
    return user

def require_superadmin(user: User = Depends(get_current_user)):
    """Require superadmin role"""
    if user.role != "superadmin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Superadmin access required")
    return user

def check_permission(user: User, permission: str):
    """Check if user has specific permission (superadmin always has all permissions)"""
    if user.role == "superadmin":
        return True
    return getattr(user, permission, False)
