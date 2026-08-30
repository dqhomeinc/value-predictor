from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'user'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class PropertyLookupCache(db.Model):
    """
    Caches RentCast's raw responses per normalized address, indefinitely
    (no expiry — RentCast's terms place no limit on retention). Protects
    the ~50 calls/month free-tier quota: a cache hit costs 0 calls, a miss
    costs 2 (Value Estimate + Property Records).
    """
    __tablename__ = 'property_lookup_cache'

    id = db.Column(db.Integer, primary_key=True)
    normalized_address = db.Column(db.String(255), unique=True, nullable=False)
    # Value Estimate (AVM) response: subject property characteristics,
    # market value estimate, and comps — all in one payload.
    raw_avm_json = db.Column(db.JSON, nullable=True)
    # Property Records response: adds zoning and subdivision, which aren't
    # present on the Value Estimate response.
    raw_property_json = db.Column(db.JSON, nullable=True)
    fetched_at = db.Column(db.DateTime, server_default=db.func.now())


class Analysis(db.Model):
    """
    A teardown-and-rebuild deal analysis for one address. The stored deal
    fields (build_cost_estimate, total_cost_estimate, required_sale_price,
    achievable_margin_pct, is_worth_it) are the baseline computed from the
    values as originally submitted — the results page's live cost/sqft and
    margin sliders are exploratory and not re-persisted here.
    """
    __tablename__ = 'analysis'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    # User inputs
    address = db.Column(db.String(255), nullable=False)
    purchase_price = db.Column(db.Float, nullable=False)
    initial_cost_per_sqft = db.Column(db.Float, nullable=False)
    initial_profit_margin_pct = db.Column(db.Float, nullable=False)

    # Denormalized property characteristics. zoning/subdivision are
    # informational-only — never used in the deal math (see plan: RentCast's
    # zoning is a classification string, not a buildable constraint).
    property_sqft = db.Column(db.Integer, nullable=True)
    property_lot_size = db.Column(db.Integer, nullable=True)
    property_bedrooms = db.Column(db.Integer, nullable=True)
    property_bathrooms = db.Column(db.Float, nullable=True)
    property_year_built = db.Column(db.Integer, nullable=True)
    property_zoning = db.Column(db.String(30), nullable=True)
    property_subdivision = db.Column(db.String(255), nullable=True)
    # RentCast Property Records' raw `history` field: a dict keyed by ISO
    # date string, each value {event, date, price}. Informational only,
    # same as zoning/subdivision — never used in the deal math.
    property_sale_history = db.Column(db.JSON, nullable=True)
    # For the results-page map (OpenStreetMap/Leaflet) — display only.
    property_latitude = db.Column(db.Float, nullable=True)
    property_longitude = db.Column(db.Float, nullable=True)

    # Market value benchmark (services/market_value.py)
    market_value_estimate = db.Column(db.Float, nullable=True)
    market_value_method = db.Column(db.String(30), nullable=True)  # 'rentcast_avm' | 'comps_median_sqft'
    market_value_confidence = db.Column(db.String(10), nullable=True)  # 'high' | 'low' — derived, not from the API
    market_value_comps_count = db.Column(db.Integer, nullable=True)
    market_value_comps_snapshot = db.Column(db.JSON, nullable=True)

    # Rebuild deal baseline (services/rebuild_calc.py)
    build_cost_estimate = db.Column(db.Float, nullable=True)
    total_cost_estimate = db.Column(db.Float, nullable=True)
    required_sale_price = db.Column(db.Float, nullable=True)
    achievable_margin_pct = db.Column(db.Float, nullable=True)
    is_worth_it = db.Column(db.Boolean, nullable=True)

    user = db.relationship('User', backref=db.backref('analyses', lazy=True, cascade='all, delete-orphan'))

    @property
    def sale_history_sorted(self):
        """
        property_sale_history as a list of {event, date, price} entries,
        most-recent-first. The dict's keys are ISO date strings ("YYYY-MM-DD"),
        which sort correctly as plain strings — no date parsing needed.
        """
        if not self.property_sale_history:
            return []
        return [entry for _, entry in sorted(self.property_sale_history.items(), reverse=True)]
