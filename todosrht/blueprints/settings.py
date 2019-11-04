from flask import Blueprint, render_template, request, url_for, abort, redirect
from flask_login import current_user
from todosrht.access import get_tracker
from todosrht.trackers import get_recent_users
from todosrht.types import UserAccess, User
from todosrht.types import Ticket, TicketAccess
from todosrht.urls import tracker_url
from todosrht.webhooks import UserWebhook
from srht.database import db
from srht.flask import loginrequired, session
from srht.validation import Validation

settings = Blueprint("settings", __name__)

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

@settings.route("/<owner>/<name>/settings/details")
@loginrequired
def details_GET(owner, name):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)
    if current_user.id != tracker.owner_id:
        abort(403)
    return render_template("tracker-details.html",
        view="details", tracker=tracker)

@settings.route("/<owner>/<name>/settings/details", methods=["POST"])
@loginrequired
def details_POST(owner, name):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)
    if current_user.id != tracker.owner_id:
        abort(403)

    valid = Validation(request)
    desc = valid.optional("tracker_desc", default=tracker.description)
    valid.expect(not desc or len(desc) < 4096,
            "Must be less than 4096 characters",
            field="tracker_desc")
    if not valid.ok:
        return render_template("tracker-details.html",
            tracker=tracker, **valid.kwargs), 400

    tracker.description = desc

    UserWebhook.deliver(UserWebhook.Events.tracker_update,
            tracker.to_dict(),
            UserWebhook.Subscription.user_id == tracker.owner_id)

    db.session.commit()
    return redirect(tracker_url(tracker))


def render_tracker_access(tracker, **kwargs):
    recent_users = get_recent_users(tracker)
    return render_template("tracker-access.html",
        view="access", tracker=tracker, access_type_list=TicketAccess,
        access_help_map=access_help_map, recent_users=recent_users, **kwargs)


@settings.route("/<owner>/<name>/settings/access")
@loginrequired
def access_GET(owner, name):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)
    if current_user.id != tracker.owner_id:
        abort(403)
    return render_tracker_access(tracker)

@settings.route("/<owner>/<name>/settings/access", methods=["POST"])
@loginrequired
def access_POST(owner, name):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)
    if current_user.id != tracker.owner_id:
        abort(403)

    valid = Validation(request)
    perm_anon = parse_html_perms('anon', valid)
    perm_user = parse_html_perms('user', valid)
    perm_submit = parse_html_perms('submit', valid)

    if not valid.ok:
        return render_tracker_access(tracker, **valid.kwargs), 400

    tracker.default_anonymous_perms = perm_anon
    tracker.default_user_perms = perm_user
    tracker.default_submitter_perms = perm_submit

    UserWebhook.deliver(UserWebhook.Events.tracker_update,
            tracker.to_dict(),
            UserWebhook.Subscription.user_id == tracker.owner_id)

    db.session.commit()
    return redirect(tracker_url(tracker))

@settings.route("/<owner>/<name>/settings/user-access/create", methods=["POST"])
@loginrequired
def user_access_create_POST(owner, name):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)
    if current_user.id != tracker.owner_id:
        abort(403)

    valid = Validation(request)
    username = valid.require("username")
    permissions = parse_html_perms("user_access", valid)
    if not valid.ok:
        return render_tracker_access(tracker, **valid.kwargs), 400

    username = username.lstrip("~")
    user = User.query.filter_by(username=username).one_or_none()
    valid.expect(user, "User not found.", field="username")
    if not valid.ok:
        return render_tracker_access(tracker, **valid.kwargs), 400

    existing = UserAccess.query.filter_by(user=user, tracker=tracker).count()

    valid.expect(user != tracker.owner,
        "Cannot override tracker owner's permissions.", field="username")
    valid.expect(existing == 0,
        "This user already has custom permissions assigned.", field="username")
    if not valid.ok:
        return render_tracker_access(tracker, **valid.kwargs), 400

    ua = UserAccess(tracker=tracker, user=user, permissions=permissions)
    db.session.add(ua)
    db.session.commit()

    return redirect(url_for("settings.access_GET",
            owner=tracker.owner.canonical_name,
            name=name))

@settings.route("/<owner>/<name>/settings/user-access/<user_id>/delete",
    methods=["POST"])
@loginrequired
def user_access_delete_POST(owner, name, user_id):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)
    if current_user.id != tracker.owner_id:
        abort(403)

    UserAccess.query.filter_by(user_id=user_id, tracker_id=tracker.id).delete()
    db.session.commit()

    return redirect(url_for("settings.access_GET",
            owner=tracker.owner.canonical_name,
            name=name))

@settings.route("/<owner>/<name>/settings/delete")
@loginrequired
def delete_GET(owner, name):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)
    if current_user.id != tracker.owner_id:
        abort(403)
    return render_template("tracker-delete.html",
        view="delete", tracker=tracker)

@settings.route("/<owner>/<name>/settings/delete", methods=["POST"])
@loginrequired
def delete_POST(owner, name):
    tracker, access = get_tracker(owner, name)
    if not tracker:
        abort(404)
    if current_user.id != tracker.owner_id:
        abort(403)
    session["notice"] = f"{tracker.owner}/{tracker.name} was deleted."
    # SQLAlchemy shits itself on some of our weird constraints/relationships
    # so fuck it, postgres knows what to do here
    tracker_id = tracker.id
    owner_id = tracker.owner_id
    assert isinstance(tracker_id, int)
    db.session.expunge_all()
    db.engine.execute(f"DELETE FROM tracker WHERE id = {tracker_id};")
    db.session.commit()

    UserWebhook.deliver(UserWebhook.Events.tracker_delete,
            { "id": tracker_id },
            UserWebhook.Subscription.user_id == owner_id)

    return redirect(url_for("html.index"))
