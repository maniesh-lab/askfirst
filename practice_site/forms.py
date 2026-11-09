"""What each practice form asks for, and the server-side checks on what comes back."""

import re
from typing import Any

LOCATIONS = ["Kathmandu", "Lalitpur", "Pokhara", "Biratnagar", "Remote"]
WORK_AUTH = [("yes", "Yes, I can work in Nepal"), ("visa", "I will need a visa"), ("no", "No")]
CATEGORIES = {
    "Electronics": [("phones", "Phones"), ("laptops", "Laptops"), ("audio", "Audio")],
    "Home": [("kitchen", "Kitchen"), ("furniture", "Furniture")],
    "Hobbies": [("books", "Books"), ("music", "Musical instruments"), ("sports", "Sports")],
}
CATEGORY_VALUES = {value for options in CATEGORIES.values() for value, _ in options}
CONDITIONS = [("new", "New"), ("like_new", "Like new"), ("used", "Used")]
CURRENCIES = ["NPR", "USD", "INR"]
SHIPPING = [
    ("pickup", "Local pickup"),
    ("courier", "Courier in the valley"),
    ("national", "Nationwide post"),
]
CONTACT_TOPICS = ["General question", "Order problem", "Partnership"]
SURVEY_TOOLS = ["Spreadsheets", "Email", "Chat apps", "Project boards"]
SURVEY_ROLES = ["Developer", "Designer", "Manager", "Student", "Other"]

PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{6,19}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL_RE = re.compile(r"^https?://\S+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _required(data: dict[str, Any], errors: dict[str, str], *names: str) -> None:
    for name in names:
        if not str(data.get(name) or "").strip():
            errors[name] = "This field is required."


def _max(data: dict[str, Any], errors: dict[str, str], name: str, limit: int) -> None:
    if len(str(data.get(name) or "")) > limit:
        errors[name] = f"Please keep this under {limit} characters."


def check_job(data: dict[str, Any], phone_name: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    _required(
        data, errors, "full_name", "email", phone_name, "location", "work_authorization", "why_role"
    )
    if data.get("email") and not EMAIL_RE.match(data["email"]):
        errors["email"] = "Please enter a valid email address."
    if data.get(phone_name) and not PHONE_RE.match(data[phone_name]):
        errors[phone_name] = "Please enter a valid phone number."
    if data.get("location") and data["location"] not in LOCATIONS:
        errors["location"] = "Please pick a location from the list."
    if data.get("work_authorization") and data["work_authorization"] not in dict(WORK_AUTH):
        errors["work_authorization"] = "Please pick one option."
    if data.get("linkedin") and not URL_RE.match(data["linkedin"]):
        errors["linkedin"] = "Please enter a full URL starting with https://"
    years = str(data.get("years_experience") or "")
    if years and (not years.isdigit() or int(years) > 50):
        errors["years_experience"] = "Please enter a whole number from 0 to 50."
    if data.get("start_date") and not DATE_RE.match(data["start_date"]):
        errors["start_date"] = "Please use the date picker."
    _max(data, errors, "why_role", 500)
    _max(data, errors, "cover_letter", 1500)
    if data.get("consent") != "yes":
        errors["consent"] = "Please agree before applying."
    return errors


def check_listing(data: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    _required(
        data,
        errors,
        "title",
        "category",
        "condition",
        "price",
        "currency",
        "quantity",
        "description",
    )
    if data.get("category") and data["category"] not in CATEGORY_VALUES:
        errors["category"] = "Please pick a category."
    if data.get("condition") and data["condition"] not in dict(CONDITIONS):
        errors["condition"] = "Please pick a condition."
    try:
        if data.get("price") and float(data["price"]) <= 0:
            errors["price"] = "Price must be more than zero."
    except ValueError:
        errors["price"] = "Price must be a number."
    if data.get("quantity") and (not str(data["quantity"]).isdigit() or int(data["quantity"]) < 1):
        errors["quantity"] = "Quantity must be at least 1."
    _max(data, errors, "title", 80)
    _max(data, errors, "description", 2000)
    return errors


def check_contact(data: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    _required(data, errors, "name", "email", "message")
    if data.get("email") and not EMAIL_RE.match(data["email"]):
        errors["email"] = "Please enter a valid email address."
    _max(data, errors, "message", 1000)
    return errors


def check_survey(data: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    _required(data, errors, "satisfaction", "role")
    if data.get("role") and data["role"] not in SURVEY_ROLES:
        errors["role"] = "Please pick a role from the list."
    _max(data, errors, "feedback", 800)
    return errors
