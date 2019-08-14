import os.path as op
import os
import re
import json
import shutil
import glob
from datetime import datetime
import csv
from tqdm import tqdm
from flask import (
    Blueprint, request, render_template, flash, g, redirect, url_for, Markup)
from sqlalchemy import sql, orm
import xnat
from app import db, app
from .forms import (
    ReportForm, RepairForm, CheckScanTypeForm)
from ..views import get_user
from ..models import (
    Project, Subject, ImgSession, User, Report, Scan, ScanType)
from ..decorators import requires_login
from ..constants import (
    LOW, NOT_RECORDED, MRI, PET, PATHOLOGIES, REPORTER_ROLE, ADMIN_ROLE,
    DATA_STATUS, FIX_XNAT, PRESENT, NOT_FOUND, UNIMELB_DARIS,
    INVALID_LABEL, NOT_CHECKED, CRITICAL, NONURGENT, FIX_OPTIONS,
    FOUND_NO_CLINICAL, NOT_REQUIRED)
from flask_breadcrumbs import (
    register_breadcrumb, default_breadcrumb_root)
from xnat.exceptions import XNATResponseError


mod = Blueprint('reporting', __name__, url_prefix='/reporting')
default_breadcrumb_root(mod, '.reporting')


daris_id_re = re.compile(r'1008\.2\.(\d+)\.(\d+)(?:\.1\.(\d+))?.*')


@mod.before_request
def before_request():
    get_user()


@mod.route('/', methods=['GET'])
@register_breadcrumb(mod, '.', 'Incidental Reporting')
@requires_login()
def index():
    # This should be edited to be a single jumping off page instead of
    # redirects
    return render_template("reporting/index.html",
                           is_reporter=g.user.has_role(REPORTER_ROLE),
                           is_admin=g.user.has_role(ADMIN_ROLE))


@mod.route('/sessions', methods=['GET'])
@register_breadcrumb(mod, '.sessions', 'Sessions to report')
@requires_login(REPORTER_ROLE)
def sessions():
    """
    Display all sessions that still need to be reported.
    """

    # Create query for sessions that still need to be reported
    to_report = (
        ImgSession.require_report()
        .filter_by(data_status=PRESENT)
        .filter(
            Scan.query
            .filter(Scan.session_id == ImgSession.id,
                    Scan.exported)
            .exists())
        .filter(~(
            Scan.query
            .join(ScanType)
            .filter(
                Scan.session_id == ImgSession.id,
                sql.or_(~ScanType.confirmed, sql.and_(ScanType.clinical,
                                                      ~Scan.exported)))
            .exists()))
        .order_by(ImgSession.priority.desc(), ImgSession.scan_date)).all()

    if not to_report:
        flash("There are no more sessions to report!", "success")
        return redirect(url_for('reporting.index'))

    return render_template("reporting/sessions.html",
                           page_title="Sessions to Report",
                           sessions=to_report,
                           form_target=url_for('reporting.report'),
                           number_of_rows=len(to_report),
                           include_priority=True)


@mod.route('/report', methods=['GET', 'POST'])
@register_breadcrumb(mod, '.sessions.report', 'Report submission')
@requires_login(REPORTER_ROLE)
def report():
    """
    Enter report
    """

    form = ReportForm(request.form)

    session_id = form.session_id.data

    # Retrieve session from database
    img_session = ImgSession.query.filter_by(
        id=session_id).first()

    if img_session is None:
        raise Exception(
            "Session corresponding to ID {} was not found".format(
                session_id))

    # Dynamically set form fields
    form.scans.choices = [
        (s.id, str(s)) for s in img_session.scans if s.exported]

    if form.selected_only.data != 'true':  # From sessions page
        if form.validate_on_submit():

            # create an report instance not yet stored in the database
            report = Report(
                session_id=session_id,
                reporter_id=g.user.id,
                findings=form.findings.data,
                conclusion=int(form.conclusion.data),
                used_scans=Scan.query.filter(
                    Scan.id.in_(form.scans.data)).all(),
                modality=MRI)

            # Insert the record in our database and commit it
            db.session.add(report)  # pylint: disable=no-member
            db.session.commit()  # pylint: disable=no-member

            # flash will display a message to the user
            flash('Report submitted for {}'.format(session_id), 'success')
            # redirect user to the 'home' method of the user module.
            return redirect(url_for('reporting.sessions'))
        else:
            flash("Some of the submitted values were invalid", "error")
    return render_template("reporting/report.html", session=img_session,
                           form=form, PATHOLOGIES=map(str, PATHOLOGIES),
                           CRITICAL=CRITICAL, NONURGENT=NONURGENT)


@mod.route('/fix-sessions', methods=['GET'])
@register_breadcrumb(mod, '.fix_sessions', 'Sessions to repair')
@requires_login(ADMIN_ROLE)
def fix_sessions():
    # Create query for sessions that need to be fixed
    to_fix = (
        ImgSession.require_report()
        .filter(ImgSession.data_status.in_((
            INVALID_LABEL, NOT_FOUND, UNIMELB_DARIS, FIX_XNAT,
            FOUND_NO_CLINICAL)))
        .order_by(ImgSession.data_status.desc(), ImgSession.scan_date)).all()

    new_missing_scans = (
        ImgSession.require_report()
        .filter_by(data_status=PRESENT)
        .filter(~(
            Scan.query
            .join(ScanType)
            .filter(
                Scan.session_id == ImgSession.id,
                sql.or_(~ScanType.confirmed, ScanType.clinical))
            .exists()))
        .order_by(ImgSession.data_status.desc(), ImgSession.scan_date)).all()

    # Update status of imports that don't have any clinical scans (and all
    # types have been confirmed)
    for img_session in new_missing_scans:
        img_session.data_status = FOUND_NO_CLINICAL

    db.session.commit()  # pylint: disable=no-member

    to_fix = new_missing_scans + to_fix

    if not to_fix:
        flash("There are no more sessions to repair!", "success")
        return redirect(url_for('reporting.index'))

    return render_template("reporting/sessions.html",
                           page_title="Sessions to Repair",
                           sessions=to_fix,
                           form_target=url_for('reporting.repair'),
                           include_status=True,
                           include_subject=True,
                           number_of_rows=len(to_fix),
                           DATA_STATUS=DATA_STATUS)


@mod.route('/repair', methods=['GET', 'POST'])
@register_breadcrumb(mod, '.fix_sessions.repair', 'Repair Session')
@requires_login(ADMIN_ROLE)
def repair():

    form = RepairForm(request.form)

    session_id = form.session_id.data

    # Retrieve session from database
    img_session = ImgSession.query.filter_by(
        id=session_id).first()

    if img_session is None:
        raise Exception(
            "Session corresponding to ID '{}' was not found".format(
                session_id))

    form.old_status.data = img_session.data_status

    if form.selected_only.data != 'true':  # From sessions page
        if form.validate_on_submit():

            old_xnat_id = img_session.xnat_id

            img_session.data_status = form.status.data
            if form.status.data in (PRESENT, FIX_XNAT):
                img_session.xnat_id = form.xnat_id.data

            # Check to see whether the session is missing clinically relevant
            # scans
            edit_link = ('<a href="javascript:select_session({});">Edit</a>'
                         .format(session_id))

            # flash will display a message to the user
            if img_session.data_status == PRESENT:
                # Add new scan types if required
                if hasattr(form, 'new_scan_types'):
                    # Delete existing scans linked to the session if present
                    (Scan.query
                     .filter_by(session_id=img_session.id)
                     .delete())

                    for scan_xnat_id, scan_type in form.new_scan_types:
                        try:
                            scan_type = ScanType.query.filter_by(
                                name=scan_type).one()
                        except orm.exc.NoResultFound:
                            scan_type = ScanType(scan_type)
                            db.session.add(scan_type)  # noqa pylint: disable=no-member
                        try:
                            Scan.query.filter_by(session_id=img_session.id,
                                                 xnat_id=scan_xnat_id).one()
                        except orm.exc.NoResultFound:
                            db.session.add(Scan(scan_xnat_id, img_session,  # noqa pylint: disable=no-member
                                                scan_type))

                    missing_scans = bool(
                        Scan.query
                        .join(ScanType)
                        .filter(
                            Scan.session_id == img_session.id,
                            sql.or_(ScanType.clinical,
                                    ~ScanType.confirmed)).count())
                else:
                    missing_scans = False

                if missing_scans:
                    img_session.data_status = FOUND_NO_CLINICAL
                    flash(Markup(
                        "{} does not contain and clinically relevant scans."
                        " Status set to '{}', change to '{}' if this is "
                        "expected. {}").format(
                            img_session.xnat_id,
                            DATA_STATUS[FOUND_NO_CLINICAL][0],
                            DATA_STATUS[NOT_REQUIRED][0],
                            edit_link), "warning")
                else:
                    flash(Markup('Repaired {}. {}'
                                 .format(session_id, edit_link)), 'success')
            elif form.status.data != form.old_status.data:
                flash(Markup('Marked {} as "{}". {}'.format(
                    session_id, DATA_STATUS[form.status.data][0], edit_link)),
                      'info')
            elif form.xnat_id.data != old_xnat_id:
                flash(Markup(
                    'Updated XNAT ID of {} but didn\'t update status from '
                    '"{}. {}"').format(
                        session_id, DATA_STATUS[form.status.data][0],
                        edit_link), 'warning')

            db.session.commit()  # pylint: disable=no-member

            # redirect user to the 'home' method of the user module.
            return redirect(url_for('reporting.fix_sessions'))
        else:
            flash("Invalid inputs", "error")
    else:
        form.xnat_id.data = img_session.xnat_id
        form.status.data = img_session.data_status

    return render_template("reporting/repair.html", session=img_session,
                           form=form, PRESENT=PRESENT, FIX_XNAT=FIX_XNAT,
                           FIX_OPTIONS=FIX_OPTIONS,
                           xnat_url=app.config['SOURCE_XNAT_URL'],
                           xnat_project=form.xnat_id.data.split('_')[0],
                           DATA_STATUS=DATA_STATUS,
                           xnat_subject='_'.join(
                               form.xnat_id.data.split('_')[:2]))


@mod.route('/confirm-scan-types', methods=['GET', 'POST'])
@register_breadcrumb(mod, '.confirm_scan_types', 'Confirm Scan Types')
@requires_login(ADMIN_ROLE)
def confirm_scan_types():

    form = CheckScanTypeForm(request.form)
    # make sure data are valid, but doesn't validate password is right

    if form.is_submitted():
        viewed_scans = json.loads(form.viewed_scan_types.data)

        clinical_scans = form.clinical_scans.data

        # Update the scans are clinically relevant
        (ScanType.query  # pylint: disable=no-member
         .filter(ScanType.id.in_(clinical_scans))
         .update({ScanType.clinical: True}, synchronize_session=False))
        # Update the scans aren't clinically relevant
        (ScanType.query  # pylint: disable=no-member
         .filter(ScanType.id.in_(viewed_scans))
         .filter(~ScanType.id.in_(clinical_scans))
         .update({ScanType.clinical: False}, synchronize_session=False))
        # Mark all viewed scans as confirmed
        (ScanType.query
         .filter(ScanType.id.in_(viewed_scans))
         .update({ScanType.confirmed: True}, synchronize_session=False))

        db.session.commit()  # pylint: disable=no-member
        flash("Confirmed clinical relevance of {} scan types"
              .format(len(viewed_scans)), "success")

    num_unconfirmed = (
        ScanType.query
        .filter_by(confirmed=False)).count()

    scan_types_to_view = (
        ScanType.query
        .filter_by(confirmed=False)
        .order_by(ScanType.name)
        .limit(app.config['NUM_ROWS_PER_PAGE'])).all()

    if not scan_types_to_view:
        flash("All scan types have been reviewed!", "success")
        return redirect(url_for('reporting.index'))

    form.clinical_scans.choices = [
        (t.id, t.name) for t in scan_types_to_view]

    form.clinical_scans.render_kw = {
        'checked': [t.clinical for t in scan_types_to_view]}

    form.viewed_scan_types.data = json.dumps(
        [t.id for t in scan_types_to_view])

    return render_template("reporting/confirm_scan_types.html", form=form,
                           num_showing=(len(scan_types_to_view),
                                        num_unconfirmed))


@mod.route('/import', methods=['GET'])
def import_():
    export_file = app.config['FILEMAKER_IMPORT_FILE']
    if not op.exists(export_file):
        raise Exception("Could not find an FileMaker export file at {}"
                        .format(export_file))
    imported = []
    previous = []
    skipped = []
    # Get previous reporters
    nick_ferris = User.query.filter_by(
        email='nicholas.ferris@monash.edu').one()
    paul_beech = User.query.filter_by(email='paul.beech@monash.edu').one()
    axis = User.query.filter_by(email='s.ahern@axisdi.com.au').one()
    with xnat.connect(server=app.config['SOURCE_XNAT_URL'],
                      user=app.config['SOURCE_XNAT_USER'],
                      password=app.config['SOURCE_XNAT_PASSWORD']) as mbi_xnat:
        with open(export_file) as f:
            rows = list(csv.DictReader(f))
            for row in tqdm(rows):
                data_status = PRESENT
                # Check to see if the project ID is one of the valid types
                mbi_project_id = row['ProjectID']
                if mbi_project_id is None or not mbi_project_id:
                    mbi_project_id = ''
                    data_status = INVALID_LABEL
                else:
                    mbi_project_id = mbi_project_id.strip()
                    if mbi_project_id[:3] not in ('MRH', 'MMH', 'CLF'):
                        print("skipping {} from {}".format(row['StudyID'],
                                                           mbi_project_id))
                        skipped.append(row)
                        continue
                try:
                    project = Project.query.filter_by(
                        mbi_id=mbi_project_id).one()
                except orm.exc.NoResultFound:
                    project = Project(mbi_project_id)
                    db.session.add(project)  # pylint: disable=no-member
                db.session.commit()  # pylint: disable=no-member
                # Extract subject information from CSV row
                mbi_subject_id = (row['SubjectID'].strip()
                                  if row['SubjectID'] is not None else '')
                study_id = (row['StudyID'].strip()
                            if row['StudyID'] is not None else '')
                first_name = (row['FirstName'].strip()
                              if row['FirstName'] is not None else '')
                last_name = (row['LastName'].strip()
                             if row['LastName'] is not None else '')
                try:
                    dob = (datetime.strptime(row['DOB'].replace('.', '/'),
                                             '%d/%m/%Y')
                           if row['DOB'] is not None else datetime(1, 1, 1))
                except ValueError:
                    raise Exception(
                        "Could not parse date of birth of {} ({})"
                        .format(study_id, row['DOB']))
                # Check to see if subject is present in database already
                # otherwise add them
                try:
                    subject = Subject.query.filter_by(
                        mbi_id=mbi_subject_id).one()
                except orm.exc.NoResultFound:
                    subject = Subject(mbi_subject_id,
                                      first_name, last_name, dob)
                    db.session.add(subject)  # pylint: disable=no-member
                # Check to see whether imaging session has previously been
                # reported or not
                if ImgSession.query.get(study_id) is None:
                    # Parse scan date
                    try:
                        scan_date = datetime.strptime(
                            row['ScanDate'].replace('.', '/'), '%d/%m/%Y')
                    except ValueError:
                        raise Exception("Could not parse scan date for {} ({})"
                                        .format(study_id, row['ScanDate']))
                    # Extract subject and visit ID from DARIS ID or explicit
                    # fields
                    if row['DarisID']:
                        match = daris_id_re.match(row['DarisID'])
                        if match is not None:
                            _, subject_id, visit_id = match.groups()
                            if visit_id is None:
                                visit_id = '1'
                        else:
                            subject_id = visit_id = ''
                            if row['DarisID'].startswith('1.5.'):
                                data_status = UNIMELB_DARIS
                            else:
                                data_status = INVALID_LABEL
                    else:
                        try:
                            subject_id = row['XnatSubjectID'].strip()
                        except (KeyError, AttributeError):
                            subject_id = ''
                        try:
                            visit_id = visit_id = row['XnatVisitID'].strip()
                        except (KeyError, AttributeError):
                            visit_id = ''
                        if not subject_id or not visit_id:
                            data_status = INVALID_LABEL
                    try:
                        subject_id = int(subject_id)
                    except ValueError:
                        pass
                    else:
                        subject_id = '{:03}'.format(subject_id)
                    # Determine whether there are outstanding report(s) for
                    # this session or not and what the XNAT session prefix is
                    all_reports_submitted = bool(row['MrReport'])
                    if mbi_project_id.startswith('MMH'):
                        visit_prefix = 'MRPT'
                        all_reports_submitted &= bool(row['PetReport'])
                    else:
                        visit_prefix = 'MR'
                    # Get the visit part of the XNAT ID
                    try:
                        numeral, suffix = re.match(r'(\d+)(.*)',
                                                   visit_id).groups()
                        visit_id = '{}{:02}{}'.format(
                            visit_prefix, int(numeral),
                            (suffix if suffix is not None else ''))
                    except (ValueError, TypeError, AttributeError):
                        data_status = INVALID_LABEL
                    xnat_id = '_'.join(
                        (mbi_project_id, subject_id, visit_id)).upper()
                    # If the session hasn't been reported on check XNAT for
                    # matching session so we can export appropriate scans to
                    # the alfred
                    scan_ids = []
                    if all_reports_submitted:
                        data_status = NOT_CHECKED
                    elif data_status not in (INVALID_LABEL, UNIMELB_DARIS):
                        try:
                            exp = mbi_xnat.experiments[xnat_id]  # noqa pylint: disable=no-member
                        except KeyError:
                            data_status = NOT_FOUND
                        else:
                            try:
                                for scan in exp.scans.values():
                                    scan_ids.append((scan.id, scan.type))
                            except XNATResponseError as e:
                                raise Exception(
                                    "Problem reading scans of {} ({}):\n{}"
                                    .format(study_id, xnat_id, str(e)))
                    session = ImgSession(study_id,
                                         project,
                                         subject,
                                         xnat_id,
                                         scan_date,
                                         data_status,
                                         priority=LOW)
                    db.session.add(session)  # pylint: disable=no-member
                    # Add scans to session
                    for scan_id, scan_type in scan_ids:
                        try:
                            scan_type = ScanType.query.filter_by(
                                name=scan_type).one()
                        except orm.exc.NoResultFound:
                            scan_type = ScanType(scan_type)
                            db.session.add(scan_type)  # noqa pylint: disable=no-member
                        db.session.add(Scan(scan_id, session, scan_type))  # noqa pylint: disable=no-member
                    # Add dummy reports if existing report was stored in FM
                    if row['MrReport'] is not None and row['MrReport'].strip():
                        if 'MSH' in row['MrReport']:
                            reporter = axis
                        else:
                            reporter = nick_ferris
                        db.session.add(Report(  # noqa pylint: disable=no-member
                            session.id, reporter.id, '', NOT_RECORDED,
                            [], MRI, date=scan_date, dummy=True))
                    if (row['PetReport'] is not None and
                            row['PetReport'].strip()):
                        db.session.add(Report(  # noqa pylint: disable=no-member
                            session.id, paul_beech.id, '', NOT_RECORDED,
                            [], PET, date=scan_date, dummy=True))  # noqa pylint: disable=no-member
                    db.session.commit()  # pylint: disable=no-member
                    imported.append(study_id)
                else:
                    previous.append(study_id)
    return 200, {'imported': imported,
                 'previous': previous,
                 'skipped': skipped}


@mod.route('/export', methods=['GET'])
def export():

    tmp_download_dir = app.config['TEMP_DOWNLOAD_DIR']

    os.makedirs(tmp_download_dir, exist_ok=True)

    with xnat.connect(server=app.config['SOURCE_XNAT_URL'],
                      user=app.config['SOURCE_XNAT_USER'],
                      password=app.config['SOURCE_XNAT_PASSWORD']) as mbi_xnat:
        with xnat.connect(server=app.config['TARGET_XNAT_URL'],
                          user=app.config['TARGET_XNAT_USER'],
                          password=app.config[
                              'TARGET_XNAT_PASSWORD']) as alf_xnat:
            alf_project = alf_xnat.projects[app.config['TARGET_XNAT_PROJECT']]  # noqa pylint: disable=no-member
            for img_session in ImgSession.ready_for_export():
                mbi_session = mbi_xnat.experiments[img_session.xnat_id]  # noqa pylint: disable=no-member
                try:
                    alf_subject = alf_project.subjects[
                        img_session.subject.mbi_id]
                except KeyError:
                    alf_subject = alf_xnat.classes.SubjectData(  # noqa pylint: disable=no-member
                        label=img_session.subject.mbi_id, parent=alf_project)
                alf_session = alf_xnat.classes.MrSessionData(  # noqa pylint: disable=no-member
                    label=img_session.id, parent=alf_subject)
                prev_exported = list(alf_session.scans.keys())
                # Loop through clinically relevant scans that haven't been
                # exported and export
                for scan in img_session.scans:
                    if scan.is_clinical and scan.xnat_id not in prev_exported:
                        mbi_scan = mbi_session.scans[str(scan.xnat_id)]
                        tmp_dir = op.join(tmp_download_dir,
                                          str(img_session.id))
                        mbi_scan.download_dir(tmp_dir)
                        alf_scan = alf_xnat.classes.MrScanData(  # noqa pylint: disable=no-member
                            id=mbi_scan.id, type=mbi_scan.type,
                            parent=alf_session)
                        resource = alf_scan.create_resource('DICOM')
                        files_dir = glob.glob(op.join(
                            tmp_dir, '*', 'scans', '*', 'resources',
                            'DICOM', 'files'))[0]
                        for fname in os.listdir(files_dir):
                            resource.upload(op.join(files_dir, fname), fname)
                        mbi_checksums = _get_checksums(mbi_xnat, mbi_scan)
                        alf_checksums = _get_checksums(alf_xnat, alf_scan)
                        if (mbi_checksums != alf_checksums):
                            raise Exception(
                                "Checksums do not match for uploaded scan {} "
                                "from {} (to {}) XNAT session".format(
                                    mbi_scan.type, mbi_session.label,
                                    alf_session.label))
                        scan.exported = True
                        db.session.commit()  # pylint: disable=no-member
                        shutil.rmtree(tmp_dir)
                # Trigger DICOM information extraction
                alf_xnat.put('/data/experiments/' + alf_session.id +  # noqa pylint: disable=no-member
                             '?pullDataFromHeaders=true')


def _get_checksums(login, scan):
    files_json = login.get_json(scan.uri + '/files')['ResultSet']['Result']
    return {r['Name']: r['digest'] for r in files_json
            if r['Name'].endswith('.dcm')}
