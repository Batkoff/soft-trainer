from django.db.models import Avg, Count, Q, Sum
from .models import Attempt, Contest
from .people import display_name

def standings(contest: Contest, live=False) -> list[dict]:
    if contest.status == Contest.Status.FINISHED and not live:
        return contest.final_standings
    graded = Q(attempt__contest=contest, attempt__mode="rated", attempt__status="graded")
    users = contest.participants.annotate(points=Sum("attempt__score", filter=graded, default=0),
        completed=Count("attempt", filter=graded), average=Avg("attempt__score", filter=graded)).select_related("profile").order_by("-points", "pk")
    rows = []
    previous_score, place = None, 0
    for index, user in enumerate(users, 1):
        if user.points != previous_score:
            place = index
        rows.append({"place": place, "user_id": user.pk, "name": display_name(user),
            "points": user.points, "completed": user.completed, "average": round(user.average or 0, 1)})
        previous_score = user.points
    return rows
