import re
import string
from sqlalchemy import or_
from flask import Blueprint, render_template, request, url_for, abort, redirect
from flask import session
from flask_login import current_user
from todosrht.access import get_tracker
from todosrht.email import notify
from todosrht.types import Tracker, User, Ticket, TicketStatus, TicketAccess
from todosrht.types import TicketComment, TicketResolution, TicketSubscription
from todosrht.types import TicketSeen, Event, EventType, EventNotification
from srht.config import cfg
from srht.database import db
from srht.flask import paginate_query, loginrequired
from srht.validation import Validation
from datetime import datetime

tracker = Blueprint("tracker", __name__)

name_re = re.compile(r"^([a-z][a-z0-9_.-]*?)+$")

smtp_user = cfg("mail", "smtp-user", default=None)
notify_from = cfg("todo.sr.ht", "notify-from", default=None)

@tracker.route("/tracker/create")
@loginrequired
def create_GET():
    return render_template("tracker-create.html")

@tracker.route("/tracker/create", methods=["POST"])
@loginrequired
def create_POST():
    valid = Validation(request)
    name = valid.require("tracker_name", friendly_name="Name")
    desc = valid.optional("tracker_desc")
    if not valid.ok:
        return render_template("tracker-create.html", **valid.kwargs), 400

    valid.expect(2 < len(name) < 256,
            "Must be between 2 and 256 characters",
            field="tracker_name")
    valid.expect(not valid.ok or name[0] in string.ascii_lowercase,
            "Must begin with a lowercase letter", field="tracker_name")
    valid.expect(not valid.ok or name_re.match(name),
            "Only lowercase alphanumeric characters or -.",
            field="tracker_name")
    valid.expect(not desc or len(desc) < 4096,
            "Must be less than 4096 characters",
            field="tracker_desc")
    if not valid.ok:
        return render_template("tracker-create.html", **valid.kwargs), 400

    tracker = (Tracker.query
            .filter(Tracker.owner_id == current_user.id)
            .filter(Tracker.name == name)
        ).first()
    valid.expect(not tracker,
            "A tracker by this name already exists",
            field="tracker_name")
    if not valid.ok:
        return render_template("tracker-create.html", **valid.kwargs), 400

    tracker = Tracker()
    tracker.owner_id = current_user.id
    tracker.name = name
    tracker.description = desc
    db.session.add(tracker)
    db.session.flush()

    sub = TicketSubscription()
    sub.tracker_id = tracker.id
    sub.user_id = current_user.id
    db.session.add(sub)
    db.session.commit()

    if "create-configure" in valid:
        return redirect(url_for(".configure_GET",
                owner=current_user.username,
                name=name))

    return redirect(url_for(".tracker_GET",
            owner=current_user.canonical_name(),
            name=name))

def apply_search(query, search):
    terms = search.split(" ")
    for term in terms:
        term = term.lower()
        if ":" in term:
            prop, value = term.split(":")
        else:
            prop, value = None, term

        if prop == "status" :
            status_aliases = {
                "closed": "resolved"
            }
            if value in status_aliases:
                value = status_aliases[value]
            if hasattr(TicketStatus, value):
                status = getattr(TicketStatus, value)
                query = query.filter(Ticket.status == status)
                continue

        if prop == "submitter":
            user = User.query.filter(User.username == value).first()
            if user:
                query = query.filter(Ticket.submitter_id == user.id)
                continue

        query = query.filter(or_(
            Ticket.description.ilike("%" + value + "%"),
            Ticket.title.ilike("%" + value + "%")))

    return query

def return_tracker(tracker, access, **kwargs):
    another = session.get("another") or False
    if another:
        del session["another"]
    is_subscribed = False
    if current_user:
        sub = (TicketSubscription.query
            .filter(TicketSubscription.tracker_id == tracker.id)
            .filter(TicketSubscription.ticket_id == None)
            .filter(TicketSubscription.user_id == current_user.id)
        ).one_or_none()
        is_subscribed = bool(sub)

    tickets = Ticket.query.filter(Ticket.tracker_id == tracker.id)
    search = request.args.get("search")
    tickets = tickets.order_by(Ticket.updated.desc())
    if search:
        tickets = apply_search(tickets, search)
    else:
        tickets = tickets.filter(Ticket.status == TicketStatus.reported)
    tickets, pagination = paginate_query(tickets, results_per_page=25)

    if "another" in kwargs:
        another = kwargs["another"]
        del kwargs["another"]

    return render_template("tracker.html",
            tracker=tracker, another=another, tickets=tickets,
            access=access, is_subscribed=is_subscribed, search=search,
            **pagination, **kwargs)

@tracker.route("/<owner>/<name>")
def tracker_GET(owner, name):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)
    return return_tracker(tracker, access)

@tracker.route("/<owner>/<name>/enable_notifications", methods=["POST"])
@loginrequired
def enable_notifications(owner, name):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)

    sub = (TicketSubscription.query
        .filter(TicketSubscription.tracker_id == tracker.id)
        .filter(TicketSubscription.ticket_id == None)
        .filter(TicketSubscription.user_id == current_user.id)
    ).one_or_none()

    if sub:
        return redirect(url_for(".tracker_GET", owner=owner, name=name))

    sub = TicketSubscription()
    sub.tracker_id = tracker.id
    sub.user_id = current_user.id
    db.session.add(sub)
    db.session.commit()
    return redirect(url_for(".tracker_GET", owner=owner, name=name))

@tracker.route("/<owner>/<name>/disable_notifications", methods=["POST"])
@loginrequired
def disable_notifications(owner, name):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)

    sub = (TicketSubscription.query
        .filter(TicketSubscription.tracker_id == tracker.id)
        .filter(TicketSubscription.ticket_id == None)
        .filter(TicketSubscription.user_id == current_user.id)
    ).one_or_none()

    if not sub:
        return redirect(url_for(".tracker_GET", owner=owner, name=name))

    db.session.delete(sub)
    db.session.commit()
    return redirect(url_for(".tracker_GET", owner=owner, name=name))

def parse_html_perms(short, valid):
    result = 0
    for sub_perm in TicketAccess:
        new_perm = valid.optional("perm_{}_{}".format(short, sub_perm.name))
        if new_perm:
            result |= int(new_perm)
    return result

access_help_map={
    TicketAccess.browse:
        "Permission to view tickets",
    TicketAccess.submit:
        "Permission to submit tickets",
    TicketAccess.comment:
        "Permission to comment on tickets",
    TicketAccess.edit:
        "Permission to edit tickets",
    TicketAccess.triage:
        "Permission to resolve, re-open, or label tickets",
}

@tracker.route("/<owner>/<name>/configure", methods=["POST"])
@loginrequired
def configure_POST(owner, name):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)
    if current_user.id != tracker.owner_id:
        abort(403)

    valid = Validation(request)
    perm_anon = parse_html_perms('anon', valid)
    perm_user = parse_html_perms('user', valid)
    perm_submit = parse_html_perms('submit', valid)
    # TODO: once repos are linked
    #perm_commit = parse_html_perms('commit', valid)

    desc = valid.optional("tracker_desc", default=tracker.description)
    valid.expect(not desc or len(desc) < 4096,
            "Must be less than 4096 characters",
            field="tracker_desc")
    if not valid.ok:
        return render_template("tracker-configure.html",
            tracker=tracker, access_type_list=TicketAccess,
            access_help_map=access_help_map, **valid.kwargs), 400

    tracker.default_anonymous_perms = perm_anon
    tracker.default_user_perms = perm_user
    tracker.default_submitter_perms = perm_submit
    #tracker.default_committer_perms = perm_commit
    tracker.description = desc
    db.session.commit()

    return redirect(url_for(".tracker_GET", owner=owner, name=name))


@tracker.route("/<owner>/<name>/configure")
@loginrequired
def configure_GET(owner, name):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)
    if current_user.id != tracker.owner_id:
        abort(403)
    return render_template("tracker-configure.html",
        tracker=tracker, access_type_list=TicketAccess,
        access_help_map=access_help_map)

@tracker.route("/<owner>/<name>/submit", methods=["POST"])
@loginrequired
def tracker_submit_POST(owner, name):
    tracker, access = get_tracker(owner, name, True)
    if not tracker:
        abort(404)
    if not TicketAccess.submit in access:
        abort(403)

    valid = Validation(request)
    title = valid.require("title", friendly_name="Title")
    desc = valid.optional("description")
    another = valid.optional("another")

    valid.expect(not title or 3 <= len(title) <= 2048,
            "Title must be between 3 and 2048 characters.",
            field="title")
    valid.expect(not desc or len(desc) < 16384,
            "Description must be no more than 16384 characters.",
            field="description")

    if not valid.ok:
        db.session.commit() # Unlock tracker row
        return return_tracker(tracker, **valid.kwargs), 400

    ticket = Ticket()
    ticket.submitter_id = current_user.id
    ticket.tracker_id = tracker.id
    ticket.scoped_id = tracker.next_ticket_id
    tracker.next_ticket_id += 1
    ticket.user_agent = request.headers.get("User-Agent")
    ticket.title = title
    ticket.description = desc
    db.session.add(ticket)
    tracker.updated = datetime.utcnow()
    # TODO: Handle unique constraint failure (contention) and retry?
    db.session.commit()
    event = Event()
    event.event_type = EventType.created
    event.user_id = current_user.id
    event.ticket_id = ticket.id
    db.session.add(event)
    db.session.flush()

    ticket_url = url_for("ticket.ticket_GET",
            owner=tracker.owner.canonical_name(),
            name=name,
            ticket_id=ticket.scoped_id)

    subscribed = False
    for sub in tracker.subscriptions:
        notification = EventNotification()
        notification.user_id = sub.user_id
        notification.event_id = event.id
        db.session.add(notification)

        if sub.user_id == ticket.submitter_id:
            subscribed = True
            continue
        notify(sub, "new_ticket", "{}/{}/#{}: {}".format(
            tracker.owner.canonical_name(), tracker.name,
            ticket.scoped_id, ticket.title),
                headers={
                    "From": notify_from,
                    "Sender": smtp_user,
                }, ticket=ticket,
                ticket_url=ticket_url.replace("%7E", "~")) # hack

    if not subscribed:
        sub = TicketSubscription()
        sub.ticket_id = ticket.id
        sub.user_id = current_user.id
        db.session.add(sub)

    db.session.commit()

    if another:
        session["another"] = True
        return redirect(url_for(".tracker_GET",
                owner=tracker.owner.canonical_name(),
                name=name))
    else:
        return redirect(ticket_url)
