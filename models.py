from db import db
from datetime import datetime


class Appointment(db.Model):
    __tablename__ = "appointments"

    id = db.Column(db.Integer, primary_key=True)

    # snapshot של פרטי המשתמש בזמן קביעת התור
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=False)

    start_time = db.Column(db.DateTime, nullable=False, index=True)
    calendar_event_id = db.Column(db.String(200), nullable=False, unique=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PhoneVerification(db.Model):
    __tablename__ = "phone_verifications"

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    code_hash = db.Column(db.String(128), nullable=False)
    attempts = db.Column(db.Integer, default=5)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    email = db.Column(db.String(120), unique=True, nullable=True, index=True)
    phone = db.Column(db.String(20), unique=True, nullable=True, index=True)
    name = db.Column(db.String(100), nullable=True)

    plan = db.Column(db.String(50), default="free")
    last_login = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    is_active = db.Column(db.Boolean, default=True)


class TrustedDevice(db.Model):
    __tablename__ = "trusted_devices"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    device_token_hash = db.Column(db.String(128), nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("trusted_devices", lazy=True))
