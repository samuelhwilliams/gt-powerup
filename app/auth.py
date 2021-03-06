import base64
from datetime import datetime
import json
import uuid

from cryptography.fernet import Fernet
from flask import flash, session
from flask_login import login_user as _login_user, logout_user as _logout_user, current_user

from app import login_manager
from app.models import User, LoginToken


@login_manager.user_loader
def load_user(user_id):
    user = User.query.get(user_id)

    if not user:
        return None

    elif not user.active:
        flash("Logged out because not active")
        return None

    elif "token_guid" not in session:
        flash("Logged out because token not in session")
        return None

    # TODO: RE-ENABLE ME LATER
    # elif user.login_tokens[-1].guid != session["token_guid"]:
    #     flash("You have been logged out of the session.", "warning")
    #     del session["token_guid"]
    #     return None

    return user


def create_login_token(app, db, email):
    user = User.query.filter(User.email == email).one_or_none()
    if user:
        tokens = LoginToken.query.filter(LoginToken.user_id == user.id, LoginToken.consumed_at == None).all()  # noqa
        for token in tokens:
            token.consumed_at = datetime.utcnow()
            db.session.add(token)

        token = LoginToken(guid=str(uuid.uuid4()), user=user)
        db.session.add(token)
        db.session.commit()

        payload_data = {"user_id": user.id, "token_guid": token.guid}

    else:
        payload_data = {"new_user": email}

    fernet = Fernet(app.config["SECRET_KEY"])
    b64_string = base64.urlsafe_b64encode(fernet.encrypt(json.dumps(payload_data).encode("utf8")))
    payload = b64_string.decode("utf8").rstrip("=")

    return payload


def logout_user(db):
    if current_user.is_authenticated:
        current_user.active = False
        db.session.add(current_user)
        db.session.commit()

    _logout_user()
    session.clear()


def login_user(app, db, payload):
    logout_user(db)

    fernet = Fernet(app.config["SECRET_KEY"])
    b64_string = base64.urlsafe_b64decode((payload + "===").encode("utf8"))
    payload_data = json.loads(fernet.decrypt(b64_string))

    if "new_user" in payload_data:
        user = User(email=payload_data["new_user"], active=True)
        now = datetime.utcnow()
        token = LoginToken(guid=str(uuid.uuid4()), user=user, created_at=now, expires_at=now, consumed_at=now)

        db.session.add(user)
        db.session.add(token)
        db.session.commit()

        _login_user(token.user)
        session["token_guid"] = token.guid

        return user

    else:  # Existing user - find valid login token
        token = LoginToken.query.get(payload_data["token_guid"])

        if not token:
            flash("No token found", "error")

        elif token.user.id != payload_data["user_id"]:
            flash("Invalid token data", "error")
            app.logger.warn("Invalid token data: ", token, token.guid, token.user.id, payload_data["user_id"])

        elif token.consumed_at:
            flash("Token already used", "error")
            app.logger.warn("Token already used: ", token, token.guid, token.consumed_at)

        elif datetime.utcnow() >= token.expires_at:
            flash("Token expired", "error")
            app.logger.warn("Token expired: ", token, token.guid, token.expires_at, datetime.utcnow(), token.created_at)

        else:
            token.consumed_at = datetime.utcnow()
            token.user.active = True

            db.session.add(token)
            db.session.add(token.user)
            db.session.commit()

            _login_user(token.user)
            session["token_guid"] = token.user.login_tokens[-1].guid

            return token.user

    return None
