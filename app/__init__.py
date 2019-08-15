import os.path as op
import logging
from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_breadcrumbs import Breadcrumbs, default_breadcrumb_root
from flask_mail import Mail
from flask_uploads import (
    UploadSet, configure_uploads, patch_request_class)

templates_dir = op.join(op.dirname(__file__), 'templates')
static_dir = op.join(op.dirname(__file__), 'static')

app = Flask(__name__, template_folder=templates_dir, static_folder=static_dir)
app.config.from_object('config')

db = SQLAlchemy(app)

Breadcrumbs(app)
default_breadcrumb_root(app, '.')

mail = Mail(app)


signature_images = UploadSet('signature', 'jpg jpeg png gif'.split())
configure_uploads(app, signature_images)
patch_request_class(app)


@app.errorhandler(404)
def not_found(error):
    return render_template('page-not-found.html'), 404


@app.teardown_appcontext
def shutdown_session(exception=None):
    db.session.remove()

from .reporting.views import mod as reportingModule  # noqa
app.register_blueprint(reportingModule)


if __name__ != '__main__':
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
