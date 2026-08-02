from functools import partial

from . import consts
from .menu import Menu
from .menus import set_default_sounds
from .speech import speak


MAX_FEEDBACK_CHARACTERS = 1000
MAX_FEEDBACK_DELETE_REASON_CHARACTERS = 200


class Tickets:
    """Accessible native feedback UI.

    The class name and legacy packet handlers are retained for compatibility
    with older servers, while all user-facing text now describes Feedback.
    """

    def __init__(self, game):
        self.game = game
        self.reviewer = False
        self.scope = "own"
        self.can_permanently_delete = False

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
            items.append(("Read this first: Staff feedback review guidelines", self.show_staff_guidelines))
            items.append(("Review open player feedback", lambda: self.request_list("staff")))
            items.append(("View resolved feedback", lambda: self.request_list("resolved")))
        items.append(("Close", self._close))
        feedback_menu.add_items(items)
        self._show(feedback_menu, replace=replace)

    def show_staff_guidelines(self):
        guidelines_menu = self._menu(
            "Thank you for helping care for player feedback. "
            "Please follow these brief guidelines for a fair, respectful, and private review."
        )
        guidelines_menu.add_items([
            (
                "Privacy: Keep feedback within the authorized staff team. "
                "Do not quote or identify the player publicly without their permission.",
                lambda: None,
            ),
            (
                "Respect: Reply calmly and professionally. Acknowledge the concern, "
                "avoid blame or sarcasm, and explain the next step when known.",
                lambda: None,
            ),
            (
                "Fairness: Never resolve, hide, or delete feedback merely because it "
                "criticizes the game, a decision, or a staff member.",
                lambda: None,
            ),
            (
                "Resolution: Send a helpful reply before resolving and archiving. "
                "If the matter is uncertain, leave it open and ask senior staff.",
                lambda: None,
            ),
            (
                "Player privacy requests: If a player asks to remove sensitive personal "
                "information, acknowledge the request and escalate it to senior staff promptly.",
                lambda: None,
            ),
            (
                "Permanent deletion: Use it only when removal is necessary, such as for "
                "sensitive information, abuse, spam, or duplicates, and record a clear reason.",
                lambda: None,
            ),
            ("Back", lambda: self.show_home(True, replace=True)),
        ])
        self._show(guidelines_menu, replace=True)

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

    def show_list(self, tickets, reviewer=False, scope="own", can_permanently_delete=False):
        self.reviewer = bool(reviewer)
        self.scope = scope if scope in ("own", "staff", "resolved") else "own"
        self.can_permanently_delete = bool(can_permanently_delete)
        if self.scope == "resolved":
            title = "Resolved player feedback"
        else:
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
                f"from {ticket.get('author', 'unknown')}, {self._status_label(ticket)}: {summary}",
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
            (f"Status: {self._status_label(ticket)}", lambda: None),
        ]
        if messages:
            items.append((f"Original message: {messages[0]}", lambda: None))
            for index, message in enumerate(messages[1:], start=1):
                items.append((f"Reply {index}: {message}", lambda: None))

        if ticket.get("status") != "closed":
            items.append(("Send a reply", lambda: self.prompt_reply(ticket, reviewer)))
            if reviewer:
                items.append(("Resolve and archive this feedback", lambda: self.confirm_close(ticket)))
        elif reviewer and self.can_permanently_delete:
            items.append((
                "Permanently delete this resolved feedback",
                lambda: self.confirm_permanent_delete(ticket),
            ))
        items.append(("Back", lambda: self.request_list(self.scope)))
        feedback_menu.add_items(items)
        self._show(feedback_menu, replace=True)

    def _status_label(self, ticket):
        status = ticket.get("status", "open")
        return "resolved and archived" if status == "closed" else status

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
        confirm_menu = self._menu(
            f"Resolve and archive feedback number {ticket.get('id', 0)}? "
            "The player can still read it in their history."
        )
        confirm_menu.add_items([
            ("Yes, resolve and archive this feedback", lambda: self.close_feedback(ticket)),
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

    def confirm_permanent_delete(self, ticket):
        warning_menu = self._menu(
            f"Permanently delete feedback number {ticket.get('id', 0)}? "
            "This cannot be undone and should be used only when removal is necessary."
        )
        warning_menu.add_items([
            ("Continue and enter a deletion reason", lambda: self.prompt_delete_reason(ticket)),
            ("Cancel and keep this feedback", lambda: self.view_feedback(ticket, True)),
        ])
        self._show(warning_menu, replace=True)

    def prompt_delete_reason(self, ticket):
        input_screen = self.game.input.run(
            "Enter a respectful reason for permanent deletion. Between 3 and 200 characters.",
            handeler=lambda reason: self.confirm_delete_reason(ticket, reason),
            msg_length=MAX_FEEDBACK_DELETE_REASON_CHARACTERS,
        )
        self._show(input_screen, replace=True)

    def confirm_delete_reason(self, ticket, reason):
        reason = reason.strip()
        if len(reason) < 3:
            speak("A deletion reason must contain at least 3 characters.")
            self.prompt_delete_reason(ticket)
            return
        if len(reason) > MAX_FEEDBACK_DELETE_REASON_CHARACTERS:
            speak(
                f"A deletion reason must not exceed "
                f"{MAX_FEEDBACK_DELETE_REASON_CHARACTERS} characters."
            )
            self.prompt_delete_reason(ticket)
            return

        final_menu = self._menu(
            f"Final confirmation for permanently deleting feedback number "
            f"{ticket.get('id', 0)}. Reason: {reason}"
        )
        final_menu.add_items([
            (
                "Yes, permanently delete and record this reason",
                lambda: self.permanently_delete_feedback(ticket, reason),
            ),
            ("No, keep this feedback", lambda: self.view_feedback(ticket, True)),
        ])
        self._show(final_menu, replace=True)

    def permanently_delete_feedback(self, ticket, reason):
        self.game.network.send(
            consts.CHANNEL_MENUS,
            "permanently_delete_feedback",
            {"id": ticket.get("id"), "reason": reason},
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
