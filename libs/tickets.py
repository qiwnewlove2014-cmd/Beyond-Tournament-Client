from functools import partial

from . import consts
from .menu import Menu
from .menus import set_default_sounds
from .speech import speak


MAX_FEEDBACK_CHARACTERS = 1000


class Tickets:
    """Accessible native feedback UI.

    The class name and legacy packet handlers are retained for compatibility
    with older servers, while all user-facing text now describes Feedback.
    """

    def __init__(self, game):
        self.game = game
        self.reviewer = False

    @property
    def gameplay(self):
        return self.game.gameplay

    def _show(self, screen, replace=False):
        if replace and self.gameplay.substates:
            return self.gameplay.replace_last_substate(screen)
        return self.gameplay.add_substate(screen)

    def _close(self):
        self.gameplay.pop_last_substate()

    def _menu(self, title):
        feedback_menu = Menu(self.game, title, parrent=self.gameplay)
        set_default_sounds(feedback_menu)
        return feedback_menu

    def show_home(self, reviewer=False, replace=False):
        self.reviewer = bool(reviewer)
        feedback_menu = self._menu("Feedback and bug reports")
        items = [
            ("Send new feedback", self.choose_category),
            ("View your feedback", lambda: self.request_list("own")),
        ]
        if self.reviewer:
            items.append(("Review open player feedback", lambda: self.request_list("staff")))
        items.append(("Close", self._close))
        feedback_menu.add_items(items)
        self._show(feedback_menu, replace=replace)

    def choose_category(self):
        category_menu = self._menu("Choose a feedback category")
        category_menu.add_items([
            ("Bug report", lambda: self.prompt_feedback("bug")),
            ("Suggestion", lambda: self.prompt_feedback("suggestion")),
            ("General feedback", lambda: self.prompt_feedback("general")),
            ("Back", lambda: self.show_home(self.reviewer, replace=True)),
        ])
        self._show(category_menu, replace=True)

    def prompt_feedback(self, category):
        prompt = (
            f"Write your {category} feedback in English. "
            f"Maximum {MAX_FEEDBACK_CHARACTERS} characters."
        )
        input_screen = self.game.input.run(
            prompt,
            handeler=lambda message: self.submit_feedback(category, message),
            msg_length=MAX_FEEDBACK_CHARACTERS,
        )
        self._show(input_screen, replace=True)

    def submit_feedback(self, category, message):
        message = message.strip()
        if not message:
            speak("Feedback canceled.")
            self.show_home(self.reviewer, replace=True)
            return
        if len(message) > MAX_FEEDBACK_CHARACTERS:
            speak(f"Feedback must not exceed {MAX_FEEDBACK_CHARACTERS} characters.")
            self.prompt_feedback(category)
            return

        self.game.network.send(
            consts.CHANNEL_MENUS,
            "submit_ticket",
            {"category": category, "message": message},
        )
        self.show_home(self.reviewer, replace=True)

    def request_list(self, scope):
        self.game.network.send(
            consts.CHANNEL_MENUS,
            "request_feedback_list",
            {"scope": scope},
        )

    def view_tickets(self, tickets, reviewer=False):
        """Compatibility entry point for legacy ticket list packets."""
        self.show_list(tickets, reviewer=reviewer)

    def show_list(self, tickets, reviewer=False):
        self.reviewer = bool(reviewer)
        title = "Open player feedback" if self.reviewer else "Your feedback"
        feedback_menu = self._menu(title)
        items = []
        for ticket in tickets:
            messages = ticket.get("message_list") or ["No message"]
            summary = str(messages[0]).replace("\r", " ").replace("\n", " ")
            if len(summary) > 120:
                summary = summary[:117] + "..."
            items.append((
                f"{ticket.get('category', 'general')} feedback {ticket.get('id', 0)} "
                f"from {ticket.get('author', 'unknown')}, {ticket.get('status', 'open')}: {summary}",
                partial(self.view_feedback, ticket, self.reviewer),
            ))

        if not items:
            items.append(("No feedback found", lambda: None))
        items.append(("Back", lambda: self.show_home(self.reviewer, replace=True)))
        feedback_menu.add_items(items)
        self._show(feedback_menu, replace=True)

    def view_feedback(self, ticket, reviewer=False):
        feedback_menu = self._menu(f"Feedback number {ticket.get('id', 0)}")
        messages = ticket.get("message_list") or []
        items = [
            (f"Author: {ticket.get('author', 'unknown')}", lambda: None),
            (f"Category: {ticket.get('category', 'general')}", lambda: None),
            (f"Status: {ticket.get('status', 'open')}", lambda: None),
        ]
        if messages:
            items.append((f"Original message: {messages[0]}", lambda: None))
            for index, message in enumerate(messages[1:], start=1):
                items.append((f"Reply {index}: {message}", lambda: None))

        if ticket.get("status") != "closed":
            items.append(("Send a reply", lambda: self.prompt_reply(ticket, reviewer)))
            if reviewer:
                items.append(("Close this feedback", lambda: self.confirm_close(ticket)))
        items.append(("Back", lambda: self.request_list("staff" if reviewer else "own")))
        feedback_menu.add_items(items)
        self._show(feedback_menu, replace=True)

    def prompt_reply(self, ticket, reviewer=False):
        input_screen = self.game.input.run(
            f"Enter your reply. Maximum {MAX_FEEDBACK_CHARACTERS} characters.",
            handeler=lambda message: self.send_reply(ticket, message, reviewer),
            msg_length=MAX_FEEDBACK_CHARACTERS,
        )
        self._show(input_screen, replace=True)

    def send_reply(self, ticket, message, reviewer=False):
        message = message.strip()
        if not message:
            speak("Reply canceled.")
            self.view_feedback(ticket, reviewer)
            return
        self.game.network.send(
            consts.CHANNEL_MENUS,
            "send_ticket_message",
            {"id": ticket.get("id"), "message": message},
        )
        self.show_home(reviewer, replace=True)

    def confirm_close(self, ticket):
        confirm_menu = self._menu(f"Close feedback number {ticket.get('id', 0)}?")
        confirm_menu.add_items([
            ("Yes, close this feedback", lambda: self.close_feedback(ticket)),
            ("No, go back", lambda: self.view_feedback(ticket, True)),
        ])
        self._show(confirm_menu, replace=True)

    def close_feedback(self, ticket):
        self.game.network.send(
            consts.CHANNEL_MENUS,
            "close_feedback",
            {"id": ticket.get("id")},
        )
        self.show_home(True, replace=True)

    # Secure legacy edit/reply entry points retained for old menu callbacks.
    def edit_ticket(self, ticket):
        input_screen = self.game.input.run(
            f"Edit your original feedback. Maximum {MAX_FEEDBACK_CHARACTERS} characters.",
            default=(ticket.get("message_list") or [""])[0],
            handeler=lambda message: self.edit_ticket2(ticket, message),
            msg_length=MAX_FEEDBACK_CHARACTERS,
        )
        self._show(input_screen, replace=True)

    def edit_ticket2(self, ticket, message):
        message = message.strip()
        if not message:
            speak("Edit canceled.")
            self.view_feedback(ticket, False)
            return
        self.game.network.send(
            consts.CHANNEL_MENUS,
            "edit_ticket",
            {"id": ticket.get("id"), "message": message},
        )
        self.show_home(False, replace=True)

    def reply_ticket(self, ticket):
        self.prompt_reply(ticket, False)

    def reply_ticket2(self, ticket, message):
        self.send_reply(ticket, message, False)
