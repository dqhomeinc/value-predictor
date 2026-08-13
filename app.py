import os

from dotenv import load_dotenv
from flask import Flask
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix

from models import db

load_dotenv()

migrate = Migrate()


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

    db.init_app(app)
    migrate.init_app(app, db)

    from blueprints.main import main_bp
    app.register_blueprint(main_bp)

    return app
