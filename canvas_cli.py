#!/usr/bin/env python3
"""
Canvas CLI - A command-line tool for Canvas LMS.
Lets you view courses, upcoming assignments, and grades from your terminal.
"""

import os
import sys
import argparse
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests
from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text

load_dotenv()

CANVAS_BASE_URL = "https://boisestatecanvas.instructure.com"
TOKEN = os.getenv("CANVAS_API_TOKEN")
console = Console()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def get_headers():
    """Return auth headers, or exit with a helpful message if token is missing."""
    if not TOKEN:
        console.print(
            "[bold red]Error:[/bold red] CANVAS_API_TOKEN is not set.\n"
            "Create a .env file with: CANVAS_API_TOKEN=your_token_here"
        )
        sys.exit(1)
    return {"Authorization": f"Bearer {TOKEN}"}


def paginate(url, params=None):
    """
    Fetch all pages from a Canvas list endpoint.
    Canvas uses Link headers to indicate the next page URL.
    """
    results = []
    headers = get_headers()
    if params is None:
        params = {}
    params["per_page"] = 100

    while url:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            console.print("[bold red]Error:[/bold red] Could not connect. Check your internet connection.")
            sys.exit(1)
        except requests.exceptions.Timeout:
            console.print("[bold red]Error:[/bold red] Request timed out. Try again.")
            sys.exit(1)
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 401:
                console.print(
                    "[bold red]Error:[/bold red] Token is invalid or expired.\n"
                    "Regenerate it in Canvas: Account > Settings > Approved Integrations."
                )
            elif resp.status_code == 403:
                console.print("[bold red]Error:[/bold red] Access denied for that resource.")
            else:
                console.print(f"[bold red]HTTP {resp.status_code}:[/bold red] {e}")
            sys.exit(1)

        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            # Some endpoints return a single object
            return data

        # Follow the next page link if it exists
        url = None
        params = {}  # next URL already has params baked in
        link_header = resp.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                break

    return results


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_courses(_args):
    """
    GET /api/v1/courses
    List all active courses for the authenticated user.
    """
    courses = paginate(
        f"{CANVAS_BASE_URL}/api/v1/courses",
        {"enrollment_state": "active", "include[]": "term"},
    )

    # Canvas sometimes returns courses without a 'name' key (e.g. concluded ones
    # that slip through). Filter those out.
    courses = [c for c in courses if "name" in c]

    if not courses:
        console.print("[yellow]No active courses found.[/yellow]")
        return

    table = Table(title="Your Active Courses", box=box.ROUNDED, show_lines=True)
    table.add_column("ID", style="dim", width=10)
    table.add_column("Course Name", style="bold", min_width=30)
    table.add_column("Code", style="cyan", width=18)
    table.add_column("Term", style="green", width=20)

    for course in courses:
        term_obj = course.get("term")
        term = term_obj.get("name", "N/A") if isinstance(term_obj, dict) else "N/A"
        table.add_row(
            str(course.get("id", "")),
            course.get("name", "Unknown"),
            course.get("course_code", "N/A"),
            term,
        )

    console.print(table)
    console.print(f"\n[dim]Found {len(courses)} active course(s)[/dim]")


def cmd_assignments(args):
    """
    GET /api/v1/courses  (to resolve course names)
    GET /api/v1/courses/:id/assignments  (for each course)

    Lists upcoming assignments sorted by due date.
    Color coding:
      red    = due within 24 hours
      yellow = due within 7 days
      green  = due later
    """
    # Fetch active courses first so we can show course names
    courses = paginate(
        f"{CANVAS_BASE_URL}/api/v1/courses",
        {"enrollment_state": "active"},
    )
    courses = [c for c in courses if "name" in c]

    if not courses:
        console.print("[yellow]No active courses found.[/yellow]")
        return

    # If user passed --course-id, narrow down to just that one
    if args.course_id:
        courses = [c for c in courses if str(c.get("id")) == str(args.course_id)]
        if not courses:
            console.print(
                f"[red]Course ID {args.course_id} not found in your active courses.\n"
                f"Run 'canvas courses' to see valid IDs.[/red]"
            )
            return

    now = datetime.now(timezone.utc)
    all_assignments = []

    with console.status("[bold green]Fetching assignments from all courses..."):
        for course in courses:
            cid = course["id"]
            params = {"order_by": "due_at", "include[]": "submission"}
            # 'upcoming' bucket filters server-side to not-yet-due
            if not args.all:
                params["bucket"] = "upcoming"

            assignments = paginate(
                f"{CANVAS_BASE_URL}/api/v1/courses/{cid}/assignments",
                params,
            )
            for a in assignments:
                a["_course_name"] = course.get("name", "Unknown")
            all_assignments.extend(assignments)

    # Sort by due date; no-due-date items go to the end
    def due_sort_key(a):
        due = a.get("due_at")
        if not due:
            return datetime.max.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(due.replace("Z", "+00:00"))

    all_assignments.sort(key=due_sort_key)

    # Drop past-due items unless --all was passed
    if not args.all:
        filtered = []
        for a in all_assignments:
            due = a.get("due_at")
            if not due:
                filtered.append(a)
                continue
            due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
            if due_dt >= now:
                filtered.append(a)
        all_assignments = filtered

    if not all_assignments:
        console.print("[yellow]No upcoming assignments found.[/yellow]")
        return

    table = Table(
        title="Upcoming Assignments",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Due", width=17)
    table.add_column("Course", style="cyan", min_width=24, overflow="fold")
    table.add_column("Assignment", min_width=34, overflow="fold")
    table.add_column("Pts", justify="right", width=5)
    table.add_column("Status", width=12)

    for a in all_assignments:
        due_str = a.get("due_at")

        if due_str:
            due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
            delta = due_dt - now
            hours = delta.total_seconds() / 3600
            due_display = due_dt.strftime("%m/%d  %I:%M %p")

            if hours < 24:
                due_text = Text(due_display, style="bold red")
                status_text = Text("DUE SOON", style="bold red")
            elif hours < 168:  # 7 days
                due_text = Text(due_display, style="bold yellow")
                status_text = Text("This Week", style="yellow")
            else:
                due_text = Text(due_display, style="green")
                status_text = Text("Upcoming", style="green")
        else:
            due_text = Text("No due date", style="dim")
            status_text = Text("-", style="dim")

        # Override status if already submitted
        submission = a.get("submission") or {}
        if submission.get("submitted_at"):
            status_text = Text("Submitted ✓", style="bold green")

        points = a.get("points_possible")
        pts_str = str(int(points)) if points is not None else "-"

        table.add_row(
            due_text,
            a.get("_course_name", "Unknown"),
            a.get("name", "Unknown"),
            pts_str,
            status_text,
        )

    console.print(table)
    console.print(f"\n[dim]Showing {len(all_assignments)} assignment(s)[/dim]")


def cmd_grades(_args):
    """
    GET /api/v1/users/self/enrollments  (for grades)
    GET /api/v1/courses                 (for course names)

    Shows current grade and score for each active course.
    """
    enrollments = paginate(
        f"{CANVAS_BASE_URL}/api/v1/users/self/enrollments",
        {"type[]": "StudentEnrollment", "state[]": "active"},
    )

    if not enrollments:
        console.print("[yellow]No active enrollments found.[/yellow]")
        return

    # Build a course ID -> name map
    courses = paginate(
        f"{CANVAS_BASE_URL}/api/v1/courses",
        {"enrollment_state": "active"},
    )
    course_map = {c["id"]: c.get("name", "Unknown") for c in courses if "id" in c}

    table = Table(title="Current Grades", box=box.ROUNDED, show_lines=True)
    table.add_column("Course", style="cyan", min_width=36, overflow="fold")
    table.add_column("Grade", justify="center", width=8)
    table.add_column("Score", justify="center", width=10)

    for e in enrollments:
        grades = e.get("grades") or {}
        cid = e.get("course_id")
        course_name = course_map.get(cid, f"Course {cid}")

        letter = grades.get("current_grade") or "N/A"
        score = grades.get("current_score")
        score_str = f"{score:.1f}%" if score is not None else "N/A"

        # Color by letter grade
        if letter.startswith("A"):
            grade_text = Text(letter, style="bold green")
        elif letter.startswith("B"):
            grade_text = Text(letter, style="green")
        elif letter.startswith("C"):
            grade_text = Text(letter, style="yellow")
        elif letter.startswith("D"):
            grade_text = Text(letter, style="bold yellow")
        elif letter == "F":
            grade_text = Text(letter, style="bold red")
        else:
            grade_text = Text(letter, style="dim")

        table.add_row(course_name, grade_text, score_str)

    console.print(table)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="canvas",
        description="A CLI tool for Canvas LMS — view courses, assignments, and grades.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- courses ---
    subparsers.add_parser("courses", help="List your active courses and their IDs.")

    # --- assignments ---
    assign_parser = subparsers.add_parser(
        "assignments",
        help="List upcoming assignments sorted by due date.",
    )
    assign_parser.add_argument(
        "-c", "--course-id",
        metavar="ID",
        help="Only show assignments for this course ID (see 'canvas courses').",
    )
    assign_parser.add_argument(
        "-a", "--all",
        action="store_true",
        help="Include past/overdue assignments as well.",
    )

    # --- grades ---
    subparsers.add_parser("grades", help="Show current grade and score for each course.")

    args = parser.parse_args()

    if args.command == "courses":
        cmd_courses(args)
    elif args.command == "assignments":
        cmd_assignments(args)
    elif args.command == "grades":
        cmd_grades(args)


if __name__ == "__main__":
    main()
