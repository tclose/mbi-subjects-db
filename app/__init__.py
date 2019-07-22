import os.path as op
from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_breadcrumbs import Breadcrumbs

templates_dir = op.join(op.dirname(__file__), 'templates')
static_dir = op.join(op.dirname(__file__), 'static')

app = Flask(__name__, template_folder=templates_dir, static_folder=static_dir)
app.config.from_object('config')

db = SQLAlchemy(app)

Breadcrumbs(app)


@app.errorhandler(404)
def not_found(error):
    return render_template('404.html'), 404

from .reporting.views import mod as reportingModule  # noqa
app.register_blueprint(reportingModule)