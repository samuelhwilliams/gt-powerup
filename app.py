#!/usr/bin/env python

import base64
from cryptography.fernet import Fernet
from datetime import datetime, timedelta
import enum
import json
import logging
import os
import requests
import uuid

from flask import (
    Flask,
    abort,
    request,
    jsonify,
    render_template,
    redirect,
    flash,
    url_for,
    session,
)
from flask_login import (
    LoginManager,
    login_user as _login_user,
    logout_user as _logout_user,
    login_required,
    current_user,
)
from flask_mail import Mail, Message
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import joinedload, backref
from sqlalchemy.orm.exc import NoResultFound

from trello import TrelloClient
from forms import (
    AuthorizeTrelloForm,
    ChooseTrelloBoardForm,
    ChooseTrelloListForm,
    LoginForm,
)


logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates")

app.config["PREFERRED_URL_SCHEME"] = "https"
app.config["SECRET_KEY"] = os.environ["SECRET_KEY"].encode("utf8")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:////tmp/flask_app.db"
)
app.config["SQLALCHEMY_ECHO"] = False
app.config["CSRF_ENABLED"] = False
db = SQLAlchemy(app)
APP_NAME = "product-signoff"

app.config["MAIL_SERVER"] = os.environ["MAILGUN_SMTP_SERVER"]
app.config["MAIL_PORT"] = os.environ["MAILGUN_SMTP_PORT"]
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USERNAME"] = os.environ["MAILGUN_SMTP_LOGIN"]
app.config["MAIL_PASSWORD"] = os.environ["MAILGUN_SMTP_PASSWORD"]
app.config["MAIL_DEFAULT_SENDER"] = "test@" + os.environ["MAILGUN_DOMAIN"]

mail = Mail(app)
login_manager = LoginManager(app)

AWAITING_PRODUCT_REVIEW = "Awaiting product review"
TICKET_APPROVED_BY = "Product accepted by {user}"

TRELLO_AUTHORIZE_URL = "https://trello.com/1/authorize"
TRELLO_API_KEY = os.environ["TRELLO_API_KEY"]
TRELLO_TOKEN_SETTINGS = dict(
    expiration="1hour", scope="read,write", name="github-signoff", key=TRELLO_API_KEY
)

GITHUB_OAUTH_URL = (
    "https://github.com/login/oauth/authorize"
    "?client_id={client_id}"
    "&redirect_uri={redirect_uri}"
    "&scope={scope}"
    "&state={state}"
)
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_CLIENT_ID = os.environ["GITHUB_CLIENT_ID"]
GITHUB_CLIENT_SECRET = os.environ["GITHUB_CLIENT_SECRET"]

SERVER_NAME = os.environ["SERVER_NAME"]


def my_login_user(user):
    _login_user(user)
    session["token_guid"] = user.current_login_token_guid
    flash("Login successful", "info")


def my_logout_user():
    _logout_user()
    if "token_guid" in session:
        del session["token_guid"]


class StatusEnum(enum.Enum):
    """
    Matches status options from GitHub status feature: https://developer.github.com/v3/repos/statuses/#create-a-status
    
    PENDING -> Trello ticket for this PR still needs product review.
    SUCCESS -> Trello ticket for this PR has been moved into 'Product accepted' column.
    """

    PENDING = "pending"
    SUCCESS = "success"


class LoginToken(db.Model):
    __tablename__ = "login_token"
    guid = db.Column(db.Text, primary_key=True)   # TODO:  should change this to a binary/native uuid type
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    payload = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.utcnow() + timedelta(minutes=5),
    )
    consumed_at = db.Column(
        db.DateTime, nullable=True
    )  # either by logging in or creating a second token

    @classmethod
    def create_token(cls, user):
        assert isinstance(user, User)
        tokens = cls.query.filter(cls.user_id == user.id)
        for token in tokens:
            if not token.consumed_at:
                token.consumed_at = datetime.utcnow()
                db.session.add(token)
            db.session.commit()

        token = cls()
        token.guid = str(uuid.uuid4())
        token.user = user

        fernet = Fernet(app.config["SECRET_KEY"])
        payload_data = {"user_id": user.id, "token_guid": token.guid}
        payload = base64.urlsafe_b64encode(fernet.encrypt(json.dumps(payload_data).encode('utf8'))).decode('utf8')
        token.payload = payload

        db.session.add(token)
        db.session.commit()

        return token

    @classmethod
    def login_user(cls, payload):
        my_logout_user()
        fernet = Fernet(app.config["SECRET_KEY"])
        payload_data = json.loads(fernet.decrypt(base64.urlsafe_b64decode(payload.encode("utf8"))))
        token = cls.query.get(payload_data["token_guid"])

        if not token:
            flash("No token found", "error")
        
        elif token.user.id != payload_data["user_id"]:
            flash("Invalid token data", "error")
            logger.warn("Invalid token data: ", token, token.guid, token.user.id, payload_data["user_id"])

        elif token.consumed_at:
            flash("Token already used", "error")
            logger.warn("Token already used: ", token, token.guid, token.payload, token.consumed_at)

        elif datetime.utcnow() >= token.expires_at:
            flash("Token expired", "error")
            logger.warn("Token expired: ", token, token.guid, token.payload, token.expires_at, datetime.utcnow(), token.created_at)
        
        else:
            token.consumed_at = datetime.utcnow()
            token.user.active = True
            # token.user.current_login_token = token

            db.session.add(token)
            db.session.add(token.user)
            db.session.commit()

            my_login_user(token.user)

            return token.user
        
        return None


class User(db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.Text, index=True)
    active = db.Column(db.Boolean, default=False)
    current_login_token_guid = db.Column(
        db.Text, db.ForeignKey("login_token.guid"), nullable=True
    )

    login_tokens = db.relationship(
        LoginToken, primaryjoin=id == LoginToken.user_id, lazy="joined", backref="user"
    )
    current_login_token = db.relationship(
        LoginToken, primaryjoin=current_login_token_guid == LoginToken.guid, lazy="joined"
    )

    @classmethod
    def find_or_create(cls, email):
        user = cls.query.filter(cls.email == email).first()
        if not user:
            user = cls(email=email)
            db.session.add(user)
            db.session.commit()
        return user

    def is_authenticated(self):
        return self.id and self.active is True

    def is_active(self):
        return self.is_authenticated()

    def is_anonymous(self):
        return not self.is_authenticated()

    def get_id(self):
        return str(self.id)


@login_manager.user_loader
def load_user(user_id):
    user = User.query.get(user_id)

    if not user:
        return None

    elif "token_guid" not in session:
        return None

    elif user.current_login_token_guid != session["token_guid"]:
        flash("You have been logged of the session.")
        del session["token_guid"]
        return None

    return user


class GithubIntegration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    state = db.Column(db.Text, index=True, nullable=False)
    token = db.Column(db.Text, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey(User.id), nullable=False)

    user = db.relationship(
        User, lazy="joined", backref=backref("github_integration", uselist=False)
    )


class GithubRepo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    repo_id = db.Column(db.Integer, index=True, nullable=False, unique=True)
    github_integration_id = db.Column(
        db.Integer, db.ForeignKey(GithubIntegration.id), nullable=False
    )

    github_integration = db.relationship(
        GithubIntegration, lazy="joined", backref="github_repos"
    )


class TrelloIntegration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.Text, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey(User.id), nullable=False)

    user = db.relationship(
        User, lazy="joined", backref=backref("trello_integration", uselist=False)
    )


class IntegratedTrelloList(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    list_id = db.Column(db.Text, nullable=False, unique=True)
    integration_id = db.Column(
        db.Integer, db.ForeignKey(TrelloIntegration.id), nullable=False
    )

    trello_integration = db.relationship(
        TrelloIntegration, lazy="joined", backref="integrated_trello_lists"
    )


class PullRequestStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    head_sha = db.Column(db.Text, index=True, nullable=False)
    repo_id = db.Column(
        db.Integer, db.ForeignKey(GithubRepo.repo_id), index=True, nullable=False
    )
    branch = db.Column(db.Text, index=True, nullable=False)
    status = db.Column(db.Text, nullable=False)  # should be an enum
    url = db.Column(db.Text, nullable=False)
    trello_list_id = db.Column(db.Text, db.ForeignKey(IntegratedTrelloList.list_id), index=True, nullable=True)
    
    github_repo = db.relationship(GithubRepo, lazy="joined", backref="pull_requests")
    trello_list = db.relationship(IntegratedTrelloList, lazy="joined", backref="pull_requests")

    @classmethod
    def create_from_github(cls, data):
        if "pull_request" in data:
            data = data["pull_request"]

            pr = PullRequestStatus.query.get(data["id"])

            if not pr:
                pr = PullRequestStatus(
                    id=data["id"],
                    head_sha=data["head"]["sha"],
                    branch=data["head"]["ref"],
                    status=StatusEnum.PENDING.value,
                    url=data["head"]["repo"]["statuses_url"].format(
                        sha=data["head"]["sha"]
                    ),
                )

            response = pr.set_github_status(
                state=StatusEnum.PENDING.value,
                description=AWAITING_PRODUCT_REVIEW,
                context=APP_NAME,
            )
            if response.status_code == 201:
                db.session.add(pr)
                db.session.commit()

    def set_github_status(self, state, description, context):
        return requests.post(
            self.url,
            json={"state": state, "description": description, "context": context},
            params={"access_token": self.trello_list.trello_integration.user.github_integration.token},  # TODO: Finish implementing github repos
        )


def get_github_client(user):
    github_integration = GithubIntegration.query.filter(
        GithubIntegration.user_id == current_user.id
    ).one()
    return GithubClient(TRELLO_API_KEY, github_integration)


def get_trello_client(user):
    trello_integration = TrelloIntegration.query.filter(
        TrelloIntegration.user_id == current_user.id
    ).one()
    return TrelloClient(TRELLO_API_KEY, trello_integration)


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET" and current_user.is_authenticated:
        return redirect(url_for(".dashboard"))
        
    form = LoginForm()

    if form.validate_on_submit():
        user = User.find_or_create(form.email.data)
        if user.active:
            user.active = False
            db.session.add(user)
            db.session.commit()

        token = LoginToken.create_token(user)
        body = render_template("email_login_link.html", payload=token.payload)
        msg = Message(
            "Login to Github-Trello-Signoff",
            sender=app.config["MAIL_DEFAULT_SENDER"],
            recipients=[form.email.data],
            html=body,
        )
        print(url_for(".login", payload=token.payload, _external=True))
        # mail.send(msg)

        flash("A login link has been emailed to you. Please click it within 5 minutes.")
        
    return render_template("index.html", form=form)


@app.route("/login/<payload>")
def login(payload):
    user = LoginToken.login_user(payload)
    if user:
        return redirect(url_for(".dashboard"))
    
    return redirect(url_for(".index"))


@app.route("/dashboard")
@login_required
def dashboard():
    github_integration = GithubIntegration.query.filter(
        GithubIntegration.user_id == current_user.id
    ).first()
    trello_integration = TrelloIntegration.query.filter(
        TrelloIntegration.user_id == current_user.id
    ).first()
    return render_template(
        "dashboard.html",
        github_integration=github_integration,
        trello_integration=trello_integration,
    )


@app.route("/github/integration", methods=["GET", "POST"])
@login_required
def integrate_github():
    # trello_card_urls = PullRequestStatus.find_trello_card_urls(request.json)
    
    if request.method == "POST":
        github_integration = GithubIntegration(state=str(uuid.uuid4()), user_id=current_user.id)
        db.session.add(github_integration)
        db.session.commit()

        session["github_integration_id"] = github_integration.id

        return redirect(
            GITHUB_OAUTH_URL.format(
                client_id=GITHUB_CLIENT_ID,
                redirect_uri="https://"
                + os.environ["SERVER_NAME"]
                + url_for(".github_authorization_complete"),
                scope="repo:status",
                state=github_integration.state,
            )
        )

    return render_template("integrate_github.html")


@app.route("/github/integration/callback", methods=["POST"])
def github_callback():
    print(request)
    if request.method == "POST" and request.json:
        PullRequestStatus.create_from_github(request.json)


@app.route("/github/integration/complete")
@login_required
def github_authorization_complete():
    github_integration = GithubIntegration.query.get(session["github_integration_id"])

    if request.args["state"] != github_integration.state:
        flash(
            "Invalid state from GitHub authentication. Possible man-in-the-middle attempt. Process aborted."
        )
        return redirect(url_for(".dashboard"))

    response = requests.get(
        GITHUB_TOKEN_URL,
        params={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": request.args["code"],
            "state": github_integration.state,
        },
        headers={"Accept": "application/json"},
    )

    if response.status_code == 200:
        github_integration.token = response.json()["access_token"]
        db.session.add(github_integration)
        db.session.commit()
        flash("GitHub integration successful.")
        return redirect(url_for(".dashboard"))

    flash("Something went wrong with integration?" + str(response))
    return redirect(url_for(".dashboard"))


@app.route("/trello/integration", methods=["HEAD"])
def integrate_trello_head():
    return jsonify(status="OK"), 200


@app.route("/trello/integration", methods=["POST"])
def trello_callback():
    data = json.loads(request.get_data(as_text=True))
    if data.get("action", {}).get("type") == "updateCard":
        try:
            integrated_trello_list = IntegratedTrelloList.query.filter(
                IntegratedTrelloList.list_id
                == data["action"]["data"]["listAfter"]["id"]
            ).one()
        
        except NoResultFound as e:
            logger.error(str(e))
            logger.error(data)
        
        else:
            trello_client = TrelloClient(TRELLO_API_KEY, integrated_trello_list.trello_integration)

            pull_requests = integrated_trello_list.pull_requests
            print(pull_requests)

            if pull_requests:
                for pull_request in pull_requests:
                    pull_requests.set_github_status(
                        state=StatusEnum.SUCCESS.value,
                        description=TICKET_APPROVED_BY,
                        context=APP_NAME,
                    )

    return jsonify(status="OK"), 200


@app.route("/trello/integration", methods=["GET"])
@login_required
def integrate_trello():
    authorize_form = AuthorizeTrelloForm()
    return render_template("integrate_trello.html", authorize_form=authorize_form)


@app.route("/trello/integration/authorize", methods=["POST"])
@login_required
def authorize_trello():
    trello_integration = TrelloIntegration.query.filter(
        TrelloIntegration.user_id == current_user.id
    ).first()
    if trello_integration:
        abort(400, "Already have an integration token for Trello")

    personalized_authorize_url = "{authorize_url}?expiration={expiration}&scope={scope}&name={name}&response_type=token&key={key}".format(
        authorize_url=TRELLO_AUTHORIZE_URL, **TRELLO_TOKEN_SETTINGS
    )
    return redirect(personalized_authorize_url)


@app.route("/trello/integration/complete", methods=["POST"])
@login_required
def authorize_trello_complete():
    form = AuthorizeTrelloForm()

    if form.validate_on_submit():
        trello_integration = TrelloIntegration.query.filter(
            TrelloIntegration.user_id == current_user.id
        ).first()
        if trello_integration:
            abort(400, "Already have an integration token for Trello")

        trello_integration = TrelloIntegration()
        trello_integration.token = form.trello_integration.data
        trello_integration.user_id = current_user.id
        db.session.add(trello_integration)
        db.session.commit()

        flash("Authorization complete.", "info")
        return redirect(url_for(".choose_trello_board")), 200

    flash("Form submit failed", "error")
    return redirect(url_for(".index")), 400


@app.route("/trello/choose-board")
@login_required
def choose_trello_board():
    trello_client = get_trello_client(current_user)
    board_form = ChooseTrelloBoardForm(trello_client.get_boards())

    return render_template("trello-select-board.html", board_form=board_form)


@app.route("/trello/choose-list", methods=["POST"])
@login_required
def choose_trello_list():
    trello_client = get_trello_client(current_user)
    
    list_form = ChooseTrelloListForm(
        trello_client.get_lists(board_id=ChooseTrelloBoardForm().board_choice.data)
    )

    return render_template("trello-select-list.html", list_form=list_form)


@app.route("/trello/create-webhook", methods=["POST"])
@login_required
def create_trello_webhook():
    trello_client = get_trello_client(current_user)

    list_form = ChooseTrelloListForm()

    trello_client.create_webhook(
        object_id=list_form.list_choice.data,
        callback_url=f"{SERVER_NAME}{url_for('.integrate_trello', integration_id=trello_client.integration.id)}",
    )

    integrated_trello_list = IntegratedTrelloList(
        integration_id=trello_client.integration.id, list_id=list_form.list_choice.data
    )

    db.session.add(integrated_trello_list)
    db.session.commit()

    flash("You have created a new webhook monitoring the “{product_list}” Trello list.", "info")
    return redirect(url_for(".index")), 201


@app.route("/trello/revoke", methods=["POST"])
@login_required
def revoke_trello():
    trello_client = get_trello_client(current_user)
    trello_client.revoke_integration()
    
    TrelloIntegration.query.filter(TrelloIntegration.user_id == current_user.id).delete()
    db.session.commit()
    
    return redirect(url_for(".dashboard"))
    


if __name__ == "__main__":
    db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)