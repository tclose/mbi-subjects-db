from flask_wtf import FlaskForm
import xnat
import itertools
from sqlalchemy import sql, orm
from wtforms import (
    StringField, BooleanField, SelectMultipleField, widgets,
    SelectField, HiddenField, TextAreaField, RadioField)
from wtforms.validators import (
    DataRequired, ValidationError, Required)
from ..models import Scan, ImgSession, ScanType
from ..constants import (
    CONCLUSION, PATHOLOGIES, DATA_STATUS, PRESENT, FIX_OPTIONS)
from app import app, signature_images, db


class DivWidget():
    """
    Renders a list of fields in separate <div> blocks
    """

    def __init__(self, html_tag='ul', prefix_label=True):
        assert html_tag in ('ol', 'ul')
        self.html_tag = html_tag
        self.prefix_label = prefix_label

    def __call__(self, field, checked=None, **kwargs):
        kwargs.setdefault('id', field.id)
        html = ['<div {}>'.format(widgets.html_params(**kwargs))]
        if checked is None:
            checked = itertools.repeat(False)
        for subfield, chk in zip(field, checked):
            if chk:
                sf = subfield(checked=True)
            else:
                sf = subfield()
            html.append(
                '<div class="inline-field">{} {}</div>'.format(sf,
                                                               subfield.label))
        html.append('</div>')
        return widgets.HTMLString(''.join(html))


class MultiCheckboxField(SelectMultipleField):
    """
    A multiple-select, except displays a list of checkboxes.

    Iterating the field will produce subfields, allowing custom rendering of
    the enclosed checkbox fields.
    """
    widget = DivWidget()
    option_widget = widgets.CheckboxInput()


class DivRadioField(RadioField):

    widget = DivWidget()


class ReportForm(FlaskForm):

    findings = TextAreaField('Findings')
    conclusion = SelectField(
        'Conclusion',
        choices=[(None, '')] + [(str(i), s)
                                for i, (s, _) in CONCLUSION.items()])
    scans = MultiCheckboxField(
        'Scans used', [DataRequired("At least one scan must be selected")],
        coerce=int)
    session_id = HiddenField('session_id')
    selected_only = HiddenField('selected_only', default=False)

    def validate_findings(self, field):
        try:
            conclusion = int(self.conclusion.data)
        except ValueError:
            pass  # A conclusion hasn't been entered either
        else:
            if not self.findings.data and conclusion in PATHOLOGIES:
                raise ValidationError("Findings must be entered if a "
                                      "pathology is reported")


class RepairForm(FlaskForm):

    status = DivRadioField('Status', coerce=int, choices=[
        (o, DATA_STATUS[o][1]) for o in FIX_OPTIONS], validators=[Required()])
    xnat_id = StringField('XNAT ID')
    session_id = HiddenField('session_id')
    old_status = HiddenField('old_status')
    selected_only = HiddenField('selected_only', default=False)

    def validate_xnat_id(self, field):
        if self.status.data == PRESENT:
            with xnat.connect(
                    server=app.config['SOURCE_XNAT_URL'],
                    user=app.config['SOURCE_XNAT_USER'],
                    password=app.config['SOURCE_XNAT_PASSWORD']) as mbi_xnat:
                try:
                    exp = mbi_xnat.experiments[self.xnat_id.data]  # noqa pylint: disable=no-member
                except KeyError:
                    raise ValidationError(
                        "Did not find '{}' XNAT session, please correct or "
                        "select a different status (i.e. other than '{}')"
                        .format(
                            self.xnat_id.data, DATA_STATUS[PRESENT][1]))
                else:
                    # Update the scans listed against the XNAT session.
                    self.new_scan_types = [(s.id, s.type)
                                           for s in exp.scans.values()]


class CheckScanTypeForm(FlaskForm):

    clinical_scans = MultiCheckboxField("Select clinically relevant scans",
                                        coerce=int)
    viewed_scan_types = HiddenField("viewed_scan_types")
