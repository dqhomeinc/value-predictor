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
    __tablename__ = 'analysis'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    # User inputs
    address = db.Column(db.String(255), nullable=False)
    purchase_price = db.Column(db.Float, nullable=False)
    condition_notes = db.Column(db.Text, nullable=True)

    # Denormalized property characteristics
    property_sqft = db.Column(db.Integer, nullable=True)
    property_bedrooms = db.Column(db.Integer, nullable=True)
    property_bathrooms = db.Column(db.Float, nullable=True)
    property_year_built = db.Column(db.Integer, nullable=True)

    # ARV
    arv_estimate = db.Column(db.Float, nullable=True)
    arv_method = db.Column(db.String(30), nullable=True)  # 'rentcast_avm' | 'comps_heuristic'
    arv_confidence_low = db.Column(db.Float, nullable=True)
    arv_confidence_high = db.Column(db.Float, nullable=True)
    arv_comps_count = db.Column(db.Integer, nullable=True)
    arv_comps_snapshot = db.Column(db.JSON, nullable=True)

    # Renovation cost
    reno_cost_estimate = db.Column(db.Float, nullable=True)
    reno_cost_breakdown = db.Column(db.JSON, nullable=True)

    # Profit projection
    holding_period_months = db.Column(db.Integer, nullable=True)
    agent_commission_pct = db.Column(db.Float, nullable=True)
    closing_costs_pct = db.Column(db.Float, nullable=True)
    projected_net_profit = db.Column(db.Float, nullable=True)
    projected_margin_pct = db.Column(db.Float, nullable=True)

    # Flip Score
    flip_score = db.Column(db.Integer, nullable=True)
    flip_grade = db.Column(db.String(30), nullable=True)
    flip_score_breakdown = db.Column(db.JSON, nullable=True)

    # LLM output
    llm_model = db.Column(db.String(60), nullable=True)
    llm_recommendation = db.Column(db.String(30), nullable=True)
    llm_summary = db.Column(db.Text, nullable=True)
    llm_risk_flags = db.Column(db.JSON, nullable=True)
    llm_raw_response = db.Column(db.JSON, nullable=True)

    user = db.relationship('User', backref=db.backref('analyses', lazy=True, cascade='all, delete-orphan'))
