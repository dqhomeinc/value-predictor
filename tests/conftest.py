import pytest
from flask import Flask

from models import db


@pytest.fixture
def app():
    """A minimal Flask app + in-memory SQLite DB, just enough for tests
    that touch PropertyLookupCache — not the full create_app() factory
    (no blueprints/login/csrf needed for these tests)."""
    flask_app = Flask(__name__)
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    flask_app.config['TESTING'] = True
    db.init_app(flask_app)

    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()
        # Dispose the engine, not just the session — Flask-SQLAlchemy
        # otherwise leaves a pooled connection alive after this Flask app
        # instance goes out of scope, which caused intermittent failures
        # in other test files' fixtures that build their own Flask app
        # against the same shared `db` object (see tests/test_routes.py).
        db.engine.dispose()
