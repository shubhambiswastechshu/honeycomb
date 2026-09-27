"""Who may do what to a report, in one place the serializer and the view share."""

from accounts.models import User


def can_delete(user, report):
    """The person who made a report, or an owner or admin of the organization.

    Everyone in the organization can open, run and edit a report -- a client's
    dashboard is worked on by whoever is on that account this week -- but
    deleting is the one step that cannot be undone, so it is kept to the author
    and the people responsible for the workspace. A report whose author has been
    deleted (created_by is NULL) can only be removed by an owner or admin.
    """
    if report.created_by_id is not None and report.created_by_id == user.id:
        return True
    return user.role in (User.Role.OWNER, User.Role.ADMIN)
