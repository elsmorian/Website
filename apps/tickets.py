from datetime import datetime, timedelta
from StringIO import StringIO
from xhtml2pdf import pisa
import qrcode
from qrcode.image.svg import SvgPathImage
from lxml import etree
from urlparse import urljoin
from flask import (
    render_template, redirect, request, flash, Blueprint,
    url_for, session, send_file, Markup, abort, current_app as app
)
from flask.ext.login import login_required, current_user
from wtforms.validators import Required, Optional, ValidationError
from wtforms import (
    SubmitField, BooleanField, StringField,
    DecimalField, FieldList, FormField, HiddenField,
)
from wtforms.fields.html5 import EmailField
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import NoResultFound

from main import db
from .common import (
    get_user_currency, set_user_currency, get_basket_and_total, process_basket,
    CURRENCY_SYMBOLS, feature_flag, create_current_user, send_template_email)
from .common.forms import IntegerSelectField, HiddenIntegerField, Form
from models.user import User
from models.ticket import (
    TicketType, Ticket, TicketAttrib,
    validate_safechars,
)
from models.site_state import get_sales_state
from models.payment import Payment


tickets = Blueprint('tickets', __name__)


class TicketForm(Form):
    ticket_id = HiddenIntegerField('Ticket Type', [Required()])


class FullTicketForm(TicketForm):
    template = 'tickets/full.html'
    accessible = BooleanField('Accessibility')


class KidsTicketForm(TicketForm):
    template = 'tickets/kids.html'
    accessible = BooleanField('Accessibility')


class CarparkTicketForm(TicketForm):
    template = 'tickets/carpark.html'
    carshare = BooleanField('Car share')


class CampervanTicketForm(TicketForm):
    pass
    # XXX do we need a template for these?
    # maybe info on where to go?
#    template= 'tickets/campervan.html'


class DonationTicketForm(TicketForm):
    template = 'tickets/donation.html'
    amount = DecimalField('Donation amount')

ticket_forms = ['full', 'kids']


def get_form_name(ticket_type):
    code = ticket_type.admits
    if code not in ticket_forms:
        return None
    return code


def add_payment_and_tickets(paymenttype):
    """
    Insert payment and tickets from session data into DB
    """
    # Implicit user signup
    if current_user.is_anonymous():
        email = session['anonymous_account_email']
        name = session['anonymous_account_name']
        try:
            create_current_user(email, name)
        except IntegrityError as e:
            app.logger.warn('Adding user raised %r, possible double-click', e)
            return None

    infodata = session.get('ticketinfo')
    basket, total = process_basket()
    currency = get_user_currency()

    if not (basket and total):
        return None

    payment = paymenttype(currency, total)
    payment.amount += paymenttype.premium(currency, total)
    current_user.payments.append(payment)

    app.logger.info('Creating tickets for basket %s', basket)
    app.logger.info('Payment: %s for %s %s (ticket total %s)', paymenttype.name,
                    payment.amount, currency, total)
    app.logger.info('Ticket info: %s', infodata)

    if infodata:
        infolists = sum([infodata[i] for i in ticket_forms], [])
        for info in infolists:
            ticket_id = int(info.pop('ticket_id'))
            ticket = basket[ticket_id]
            for k, v in info.items():
                attrib = TicketAttrib(k, v)
                ticket.attribs.append(attrib)

    for ticket in basket:
        name = get_form_name(ticket.type)

        current_user.tickets.append(ticket)
        ticket.payment = payment
        if currency == 'GBP':
            ticket.expires = datetime.utcnow() + timedelta(days=app.config.get('EXPIRY_DAYS_TRANSFER'))
        elif currency == 'EUR':
            ticket.expires = datetime.utcnow() + timedelta(days=app.config.get('EXPIRY_DAYS_TRANSFER_EURO'))

    db.session.commit()

    session.pop('basket', None)
    session.pop('ticketinfo', None)

    return payment


class ReceiptForm(Form):
    forward = SubmitField('Show e-Tickets')


@tickets.route("/tickets/", methods=['GET', 'POST'])
def main():
    if current_user.is_anonymous():
        return redirect(url_for('tickets.choose'))

    form = ReceiptForm()
    if form.validate_on_submit():
        ticket_ids = map(str, request.form.getlist('ticket_id', type=int))
        if ticket_ids:
            return redirect(url_for('tickets.receipt', ticket_ids=','.join(ticket_ids)) + '?pdf=1')
        return redirect(url_for('tickets.receipt') + '?pdf=1')

    tickets = current_user.tickets.all()
    if not tickets:
        return redirect(url_for('tickets.choose'))

    payments = current_user.payments.filter(Payment.state != "cancelled", Payment.state != "expired").all()

    return render_template("tickets.html",
                           tickets=tickets,
                           payments=payments,
                           form=form)


@tickets.route("/tickets/token/")
@tickets.route("/tickets/token/<token>")
def tickets_token(token=None):
    if TicketType.get_types_for_token(token):
        session['ticket_token'] = token
    else:
        if 'ticket_token' in session:
            del session['ticket_token']
        flash('Ticket token was invalid')

    return redirect(url_for('tickets.choose'))


class TicketAmountForm(Form):
    amount = IntegerSelectField('Number of tickets', [Optional()])
    code = HiddenIntegerField('Ticket Type', [Required()])


class TicketAmountsForm(Form):
    types = FieldList(FormField(TicketAmountForm))
    buy = SubmitField('Buy Tickets')
    currency_code = HiddenField('Currency')
    set_currency = StringField('Set Currency', [Optional()])

    def validate_set_currency(form, field):
        if field.data not in CURRENCY_SYMBOLS:
            raise ValidationError('Invalid currency %s' % field.data)


@tickets.route("/tickets/choose", methods=['GET', 'POST'])
@feature_flag('TICKET_SALES')
def choose():
    if get_sales_state(datetime.utcnow()) != 'available':
        return render_template("tickets-cutoff.html")

    form = TicketAmountsForm()

    tts = TicketType.query.order_by(TicketType.order).all()

    token = session.get('ticket_token')
    limits = dict([(tt.id, tt.user_limit(current_user, token)) for tt in tts])

    if request.method != 'POST':
        # Empty form - populate ticket types
        first_full = False
        for tt in tts:
            form.types.append_entry()
            # Set as unicode because that's what the browser will return
            form.types[-1].code.data = tt.id

            if (not first_full) and (tt.admits == 'full') and (limits[tt.id] > 0):
                first_full = True
                form.types[-1].amount.data = 1

    tts = dict((tt.id, tt) for tt in tts)
    for f in form.types:
        t_id = int(f.code.data)  # On form return this may be a string
        f._type = tts[t_id]

        values = range(limits[t_id] + 1)
        f.amount.values = values
        f._any = any(values)

    if form.validate_on_submit():
        if form.buy.data:
            set_user_currency(form.currency_code.data)

            basket = []
            for f in form.types:
                if f.amount.data:
                    tt = f._type
                    app.logger.info('Adding %s %s tickets to basket', f.amount.data, tt.name)
                    basket += [tt.id] * f.amount.data

            if basket:
                session['basket'] = basket

                return redirect(url_for('tickets.info'))

    if request.method == 'POST' and form.set_currency.data:
        if form.set_currency.validate(form):
            app.logger.info("Updating currency to %s only", form.set_currency.data)
            set_user_currency(form.set_currency.data)

            for field in form:
                field.errors = []

    form.currency_code.data = get_user_currency()

    return render_template("tickets-choose.html", form=form)


class TicketInfoForm(Form):
    email = EmailField('Email', [Required()])
    name = StringField('Name', [Required()])

    full = FieldList(FormField(FullTicketForm))
    kids = FieldList(FormField(KidsTicketForm))
    carpark = FieldList(FormField(CarparkTicketForm))
    campervan = FieldList(FormField(CampervanTicketForm))
    donation = FieldList(FormField(DonationTicketForm))

    forward = SubmitField('Continue to Check-out')

    def validate_email(form, field):
        if current_user.is_anonymous() and User.does_user_exist(field.data):
            field.was_duplicate = True
            raise ValidationError('Account already exists')


def build_info_form(formdata):
    basket, total = get_basket_and_total()

    if not basket:
        return None, basket, total

    parent_form = TicketInfoForm(formdata)

    # First, filter to the currently exposed forms
    forms = [getattr(parent_form, f) for f in ticket_forms]

    if not any(forms):
        # Nothing submitted, so create forms for the basket
        for i, ticket in enumerate(basket):
            name = get_form_name(ticket)
            if not name:
                continue

            f = getattr(parent_form, name)
            f.append_entry()
            ticket.form = f[-1]
            ticket.form.ticket_id.data = i

        if not any(forms):
            # No forms to fill
            return None, basket, total

    else:
        # If we have some details, match them to the basket
        # FIXME: doesn't play well with multiple browser tabs
        form_tickets = [t for t in basket if get_form_name(t)]
        entries = sum([f.entries for f in forms], [])
        for ticket, subform in zip(form_tickets, entries):
            ticket.form = subform

    # FIXME: check that there aren't any surplus submitted forms
    return parent_form, basket, total


@tickets.route("/tickets/info", methods=['GET', 'POST'])
def info():
    form, basket, total = build_info_form(request.form)
    if not form:
        return redirect(url_for('payments.choose'))

    if not current_user.is_anonymous():
        form.email.data = current_user.email
        form.name.data = current_user.name

    if form.validate_on_submit():
        if current_user.is_anonymous():
            session['anonymous_account_email'] = form.email.data
            session['anonymous_account_name'] = form.name.data

        session['ticketinfo'] = form.data

        return redirect(url_for('payments.choose'))

    return render_template('tickets-info.html',
                           form=form, basket=basket,
                           total=total, is_anonymous=current_user.is_anonymous())




class TicketTransferForm(Form):
    email = EmailField('Email', [Required()])
    name = StringField('Name', [Required()])

    transfer = SubmitField('Transfer Ticket')


@tickets.route('/tickets/<ticket_id>/transfer', methods=['GET', 'POST'])
@login_required
def transfer(ticket_id):
    try:
        ticket = current_user.tickets.filter_by(id=ticket_id).one()
    except NoResultFound:
        return redirect(url_for('tickets.main'))

    if not ticket or not ticket.paid or not ticket.type.is_transferable:
        return redirect(url_for('tickets.main'))

    form = TicketTransferForm()

    if form.validate_on_submit():
        assert ticket.user_id == current_user.id
        email = form.email.data

        if not User.does_user_exist(email):
            # Create a new user to transfer the ticket to
            to_user = User(email, form.name.data)
            to_user.generate_random_password()
            db.session.add(to_user)
            db.session.commit()
            email_template = 'ticket-transfer-new-owner-and-user.txt'
        else:
            to_user = User.query.filter_by(email=email).one()
            email_template = 'ticket-transfer-new-owner.txt'

        ticket.transfer_to(to_user)
        # Log the transfer in an SQL parse-able manner (e.g. ids only)
        transfer_str = '%d -> %d' % (current_user.id, to_user.id)
        ticket.attribs.append(TicketAttrib('transfer', transfer_str))

        app.logger.info('Ticket %s transferred from %s to %s', ticket,
                        current_user, to_user)
        # Save everything
        db.session.commit()
        # Alert the users via email
        send_template_email("You've been sent a ticket to EMF 2016!",
                            to_user.email, current_user.email,
                            'emails/' + email_template,
                            to_user=to_user, from_user=current_user)

        send_template_email("You sent someone an EMF 2016 ticket",
                            to_user.email, current_user.email,
                            'emails/ticket-transfer-original-owner.txt',
                            to_user=to_user, from_user=current_user)

        return redirect(url_for('tickets.transferred'))

    return render_template('ticket-transfer.html', ticket=ticket, form=form)


@tickets.route("/tickets/transferred")
@login_required
def transferred():
    # Build a 'like' query string to find transfers from this user.
    id_str = '{0:d}%'.format(current_user.id)
    transfer_logs = TicketAttrib.query.filter_by(name='transfer').\
                                 filter(TicketAttrib.value.like(id_str)).all()

    transferred = []
    for ta in transfer_logs:
        ticket = ta.ticket
        # Because we log both ends of the transfer we can make sure we only show
        # people the transfers they have made (and not any subsequent transfers)
        to_user_id = int(ta.value.split('->')[1])
        to_user = User.query.get(to_user_id)
        transferred.append((ticket, to_user))

    return render_template('tickets-transferred.html', transferred=transferred)



@tickets.route("/tickets/receipt")
@tickets.route("/tickets/<ticket_ids>/receipt")
@login_required
def receipt(ticket_ids=None):
    if current_user.admin and ticket_ids is not None:
        tickets = Ticket.query
    else:
        tickets = current_user.tickets

    tickets = tickets.filter_by(paid=True) \
        .join(Payment).filter(~Payment.state.in_(['cancelled'])) \
        .join(TicketType).order_by(TicketType.order)

    if ticket_ids is not None:
        ticket_ids = map(int, ticket_ids.split(','))
        tickets = tickets.filter(Ticket.id.in_(ticket_ids))

    if not tickets.all():
        abort(404)

    png = bool(request.args.get('png'))
    pdf = bool(request.args.get('pdf'))
    table = bool(request.args.get('table'))

    page = render_receipt(tickets, png, table, pdf)
    if pdf:
        return send_file(render_pdf(page), mimetype='application/pdf')

    return page


def render_receipt(tickets, png=False, table=False, pdf=False):
    user = tickets[0].user

    for ticket in tickets:
        if ticket.receipt is None:
            ticket.create_receipt()
        if ticket.qrcode is None:
            ticket.create_qrcode()

    entrance_tickets = tickets.filter(TicketType.admits.in_(['full', 'kids'])).all()
    vehicle_tickets = tickets.filter(TicketType.admits.in_(['car', 'campervan'])).all()

    return render_template('receipt.html', user=user, format_inline_qr=format_inline_qr,
                           entrance_tickets=entrance_tickets, vehicle_tickets=vehicle_tickets,
                           pdf=pdf, png=png, table=table)


def render_pdf(html, url_root=None):
    # This needs to fetch URLs found within the page, so if
    # you're running a dev server, use app.run(processes=2)
    if url_root is None:
        url_root = request.url_root

    def fix_link(uri, rel):
        if uri.startswith('//'):
            uri = 'https:' + uri
        if uri.startswith('https://'):
            return uri

        return urljoin(url_root, uri)

    pdffile = StringIO()
    pisa.CreatePDF(html, pdffile, link_callback=fix_link)
    pdffile.seek(0)

    return pdffile


def format_inline_qr(code):
    url = app.config.get('CHECKIN_BASE') + code

    qrfile = StringIO()
    qr = qrcode.make(url, image_factory=SvgPathImage)
    qr.save(qrfile, 'SVG')
    qrfile.seek(0)

    root = etree.XML(qrfile.read())
    # Wrap inside an element with the right default namespace
    svgns = 'http://www.w3.org/2000/svg'
    newroot = root.makeelement('{%s}svg' % svgns, nsmap={None: svgns})
    newroot.append(root)

    return Markup(etree.tostring(root))


@tickets.route("/receipt/<code>/qr")
def tickets_qrcode(code):
    if len(code) > 8:
        abort(404)

    if not validate_safechars(code):
        abort(404)

    url = app.config.get('CHECKIN_BASE') + code

    qrfile = make_qr_png(url, box_size=3)
    return send_file(qrfile, mimetype='image/png')


def make_qr_png(*args, **kwargs):
    qrfile = StringIO()

    qr = qrcode.make(*args, **kwargs)
    qr.save(qrfile, 'PNG')
    qrfile.seek(0)

    return qrfile