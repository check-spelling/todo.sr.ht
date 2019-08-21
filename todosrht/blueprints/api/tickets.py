from flask import Blueprint, abort, request
from srht.api import paginated_response
from srht.database import db
from srht.oauth import oauth, current_token
from srht.validation import Validation
from todosrht.access import get_tracker, get_ticket
from todosrht.tickets import add_comment, get_participant_for_user, submit_ticket
from todosrht.blueprints.api import get_user
from todosrht.types import Ticket, TicketAccess, TicketStatus, TicketResolution
from todosrht.types import Event, EventType, Label, TicketLabel
from todosrht.webhooks import TrackerWebhook, TicketWebhook

tickets = Blueprint("api.tickets", __name__)

@tickets.route("/api/user/<username>/trackers/<tracker_name>/tickets")
@tickets.route("/api/trackers/<tracker_name>/tickets",
        defaults={"username": None})
@oauth("tickets:read")
def tracker_tickets_GET(username, tracker_name):
    user = get_user(username)
    tracker, access = get_tracker(user, tracker_name, user=current_token.user)
    if not tracker:
        abort(404)
    if not TicketAccess.browse in access:
        abort(401)
    tickets = (Ticket.query
        .filter(Ticket.tracker_id == tracker.id)
        .order_by(Ticket.scoped_id.desc()))
    return paginated_response(Ticket.scoped_id, tickets)

@tickets.route("/api/user/<username>/trackers/<tracker_name>/tickets",
        methods=["POST"])
@tickets.route("/api/trackers/<tracker_name>/tickets",
        defaults={"username": None}, methods=["POST"])
@oauth("tickets:write")
def tracker_tickets_POST(username, tracker_name):
    user = get_user(username)
    tracker, access = get_tracker(user, tracker_name, user=current_token.user)
    if not tracker:
        abort(404)
    if not TicketAccess.submit in access:
        abort(401)

    valid = Validation(request)
    title = valid.require("title")
    desc = valid.require("description")
    valid.expect(not title or 3 <= len(title) <= 2048,
            "Title must be between 3 and 2048 characters.",
            field="title")
    valid.expect(not desc or len(desc) < 16384,
            "Description must be no more than 16384 characters.",
            field="description")
    if not valid.ok:
        return valid.response

    participant = get_participant_for_user(current_token.user)
    ticket = submit_ticket(tracker, participant.id, title, desc)
    TrackerWebhook.deliver(TrackerWebhook.Events.ticket_create,
            ticket.to_dict(),
            TrackerWebhook.Subscription.tracker_id == tracker.id)
    return ticket.to_dict(), 201

@tickets.route("/api/user/<username>/trackers/<tracker_name>/tickets/<ticket_id>")
@tickets.route("/api/trackers/<tracker_name>/tickets/<ticket_id>",
        defaults={"username": None})
@oauth("tickets:read")
def tracker_ticket_by_id_GET(username, tracker_name, ticket_id):
    user = get_user(username)
    tracker, _ = get_tracker(user, tracker_name, user=current_token.user)
    if not tracker:
        abort(404)
    ticket, access = get_ticket(tracker, ticket_id, user=current_token.user)
    if not TicketAccess.browse in access:
        abort(401)
    return ticket.to_dict()

def _webhook_filters(query, username, tracker_name, ticket_id):
    user = get_user(username)
    tracker, _ = get_tracker(user, tracker_name, user=current_token.user)
    if not tracker:
        abort(404)
    ticket, access = get_ticket(tracker, ticket_id, user=current_token.user)
    if not TicketAccess.browse in access:
        abort(401)
    return query.filter(TicketWebhook.Subscription.ticket_id == ticket.id)

def _webhook_create(sub, valid, username, tracker_name, ticket_id):
    user = get_user(username)
    tracker, _ = get_tracker(user, tracker_name, user=current_token.user)
    if not tracker:
        abort(404)
    ticket, access = get_ticket(tracker, ticket_id, user=current_token.user)
    if not TicketAccess.browse in access:
        abort(401)
    sub.ticket_id = ticket.id
    return sub

TicketWebhook.api_routes(tickets,
        "/api/user/<username>/trackers/<tracker_name>/tickets/<ticket_id>",
        filters=_webhook_filters, create=_webhook_create)

@tickets.route("/api/user/<username>/trackers/<tracker_name>/tickets/<ticket_id>",
        methods=["PUT"])
@tickets.route("/api/trackers/<tracker_name>/tickets/<ticket_id>",
        defaults={"username": None}, methods=["PUT"])
@oauth("tickets:write")
def tracker_ticket_by_id_PUT(username, tracker_name, ticket_id):
    user = get_user(username)
    tracker, _ = get_tracker(user, tracker_name, user=current_token.user)
    if not tracker:
        abort(404)
    ticket, access = get_ticket(tracker, ticket_id, user=current_token.user)

    participant = get_participant_for_user(current_token.user)
    required_access = TicketAccess.none
    valid = Validation(request)
    comment = resolution = None
    resolve = reopen = False
    labels = None
    events = list()
    if "comment" in valid:
        required_access |= TicketAccess.comment
        comment = valid.optional("comment")
        valid.expect(not comment or 3 <= len(comment) <= 16384,
                "Comment must be between 3 and 16384 characters.",
                field="comment")
    if "status" in valid:
        required_access |= TicketAccess.triage
        status = valid.optional("status",
                cls=TicketStatus, default=valid.status)
        if status != ticket.status:
            if status != TicketStatus.open:
                resolve = True
                resolution = valid.require("resolution", cls=TicketResolution)
            else:
                reopen = True
    if "labels" in valid:
        required_access |= TicketAccess.triage
        labels = valid.optional("labels", cls=list)
        valid.expect(all(isinstance(x, str) for x in labels),
                "Expected array of strings", field="labels")
        if not valid.ok:
            return valid.response
        have = set(label.name for label in ticket.labels)
        want = set(labels)
        to_remove = have - want
        to_add = want - have
        for name in to_remove:
            label = (Label.query
                    .filter(Label.tracker_id == tracker.id)
                    .filter(Label.name == name)).one_or_none()
            (TicketLabel.query
                    .filter(TicketLabel.ticket_id == ticket.id)
                    .filter(TicketLabel.label_id == label.id)).delete()
            event = Event()
            event.event_type = EventType.label_removed
            event.participant_id = participant.id
            event.ticket_id = ticket.id
            event.label_id = label.id
            db.session.add(event)
            db.session.flush()
            TicketWebhook.deliver(TicketWebhook.Events.event_create,
                    event.to_dict(),
                    TicketWebhook.Subscription.ticket_id == ticket.id)
            TrackerWebhook.deliver(TrackerWebhook.Events.event_create,
                    event.to_dict(),
                    TrackerWebhook.Subscription.tracker_id == ticket.tracker_id)
            events.append(event)
        for name in to_add:
            label = (Label.query
                    .filter(Label.tracker_id == tracker.id)
                    .filter(Label.name == name)).one_or_none()
            valid.expect(label, f"Unknown label {name}", field="labels")
            tl = TicketLabel()
            tl.ticket_id = ticket.id
            tl.label_id = label.id
            tl.user_id = current_token.user_id
            db.session.add(tl)
            event = Event()
            event.event_type = EventType.label_added
            event.participant_id = participant.id
            event.ticket_id = ticket.id
            event.label_id = label.id
            db.session.add(event)
            db.session.flush()
            TicketWebhook.deliver(TicketWebhook.Events.event_create,
                    event.to_dict(),
                    TicketWebhook.Subscription.ticket_id == ticket.id)
            TrackerWebhook.deliver(TrackerWebhook.Events.event_create,
                    event.to_dict(),
                    TrackerWebhook.Subscription.tracker_id == ticket.tracker_id)
            events.append(event)
        if not valid.ok:
            return valid.response

    if not valid.ok:
        return valid.response

    if access & required_access != required_access:
        abort(401)

    if comment or resolve or resolution or reopen:
        event = add_comment(
            participant, ticket, comment, resolve, resolution, reopen)
        db.session.add(event)
        db.session.flush()
        events.append(events)
        TicketWebhook.deliver(TicketWebhook.Events.event_create,
                event.to_dict(),
                TicketWebhook.Subscription.ticket_id == ticket.id)
        TrackerWebhook.deliver(TrackerWebhook.Events.event_create,
                event.to_dict(),
                TrackerWebhook.Subscription.tracker_id == ticket.tracker_id)

    db.session.commit()

    return {
        "ticket": ticket.to_dict(),
        "events": [event.to_dict() for event in events],
    }

@tickets.route("/api/user/<username>/trackers/<tracker_name>/tickets/<ticket_id>/events")
@tickets.route("/api/trackers/<tracker_name>/tickets/<ticket_id>/events",
        defaults={"username": None})
@oauth("tickets:read")
def tracker_ticket_by_id_events_GET(username, tracker_name, ticket_id):
    user = get_user(username)
    tracker, _ = get_tracker(user, tracker_name, user=current_token.user)
    if not tracker:
        abort(404)
    ticket, access = get_ticket(tracker, ticket_id, user=current_token.user)
    if not TicketAccess.browse in access:
        abort(401)
    events = Event.query.filter(Event.ticket_id == ticket.id)
    return paginated_response(Event.id, events)
