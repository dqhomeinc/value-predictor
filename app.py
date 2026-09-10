import os

from dotenv import load_dotenv
from flask import Flask
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from models import db, User

load_dotenv()

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
migrate = Migrate()
csrf = CSRFProtect()


def create_app():
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # DATABASE_URL set implies a deployed environment (e.g. Render), where a
    # real SECRET_KEY is required — fail fast at startup rather than silently
    # falling back to a public, committed value that would let anyone forge
    # session cookies once session-based auth is wired up. Locally, with no
    # DATABASE_URL, fall back to the dev-only value for convenience.
    database_url = os.environ.get('DATABASE_URL')
    if database_url:
        app.config['SECRET_KEY'] = os.environ['SECRET_KEY']
    else:
        app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-fallback-key')

    # Use PostgreSQL (via pg8000, pure-Python driver) when DATABASE_URL is
    # set; SQLite locally. pg8000 needs the +pg8000 dialect prefix and has no
    # system library dependencies.
    db_url = database_url or 'sqlite:///value_predictor.db'
    if db_url.startswith('postgres://'):
        db_url = 'postgresql+pg8000://' + db_url[len('postgres://'):]
    elif db_url.startswith('postgresql://'):
        db_url = 'postgresql+pg8000://' + db_url[len('postgresql://'):]
    # pg8000 negotiates SSL automatically and doesn't accept a sslmode kwarg.
    db_url = db_url.split('?', 1)[0]
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    if db_url.startswith('postgresql+pg8000://'):
        # Neon suspends its compute after a few idle minutes and closes every
        # connection, including the ones SQLAlchemy is holding in its pool.
        # Without a check, the next request is handed one of those dead
        # connections and its first query fails with pg8000's "network
        # error". In production that surfaced as a 500 on the first request
        # after 7 idle minutes, raised from Flask-Login's user loader before
        # the view even ran. pool_pre_ping tests each connection as it
        # leaves the pool and transparently replaces a dead one.
        # Postgres only: the local SQLite file has no server to drop it.
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from blueprints.auth import auth_bp
    from blueprints.main import main_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    return app
