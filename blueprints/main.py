from flask import Blueprint, render_template, jsonify
from flask_login import login_required, current_user

main_bp = Blueprint('main', __name__)


@main_bp.route('/health')
def health():
    return jsonify(status='ok')


@main_bp.route('/')
@login_required
def index():
    return render_template('index.html', user=current_user)
