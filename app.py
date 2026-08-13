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
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-fallback-key')

    # Use PostgreSQL (via pg8000, pure-Python driver) when DATABASE_URL is
    # set; SQLite locally. pg8000 needs the +pg8000 dialect prefix and has no
    # system library dependencies.
    db_url = os.environ.get('DATABASE_URL') or 'sqlite:///value_predictor.db'
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
