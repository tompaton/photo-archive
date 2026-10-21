"""Small forms and highlighted EXIF viewer."""

import sqlite3
from collections.abc import Callable

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    LoadingIndicator,
    RadioButton,
    RadioSet,
    Select,
    Static,
    TextArea,
)

from .progress import background


class BooleanField(RadioSet):
    """Boolean options using the form's existing true/false/blank values."""

    DEFAULT_CSS = """
    BooleanField { layout: horizontal; height: 3; margin-bottom: 1; }
    """

    def __init__(self, key: str, value: str, nullable: bool) -> None:
        self.values = ("true", "false", "") if nullable else ("true", "false")
        labels = {"true": "True", "false": "False", "": "Null (inherit)"}
        super().__init__(
            *(
                RadioButton(labels[option], value=value == option)
                for option in self.values
            ),
            id=key,
        )

    @property
    def value(self) -> str:
        return self.values[self.pressed_index]


class Form(ModalScreen[dict[str, str] | None]):
    DEFAULT_CSS = """
    Form { align: center middle; }
    Form > VerticalScroll { width: 90%; max-width: 100; height: 90%; background: $surface; padding: 1 2; border: solid $accent; }
    Form Input, Form Select { margin-bottom: 1; }
    Form Horizontal { height: 3; }
    """
    BINDINGS = [("escape", "dismiss_form", "Cancel")]

    def __init__(
        self,
        title: str,
        fields: dict[str, str],
        save: Callable[[dict[str, str]], None],
        choices: dict[str, list[tuple[str, str]]] | None = None,
    ) -> None:
        super().__init__()
        self.title_text, self.fields = title, fields
        self.save = save
        self.choices = choices or {}
        self.saving = False

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label(self.title_text)
            for key, value in self.fields.items():
                yield Label(key.replace("_", " "))
                if key in self.choices:
                    yield Select(
                        self.choices[key], value=value, allow_blank=False, id=key
                    )
                elif key in ("skip_original", "skip_final", "enabled"):
                    yield BooleanField(key, value, nullable=key != "enabled")
                else:
                    yield Input(value, id=key)
            yield Static("", id="validation", markup=False)
            yield LoadingIndicator(id="saving")
            with Horizontal():
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#saving").display = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self.save_draft()

    @work(exclusive=True)
    async def save_draft(self) -> None:
        self.saving = True
        self.query_one("#saving").display = True
        for button in self.query(Button):
            button.disabled = True
        values = {key: self.field_value(key) for key in self.fields}
        try:
            await background(self.save, values)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.query_one("#validation", Static).update(f"Cannot save: {exc}")
        else:
            self.dismiss(values)
        finally:
            self.saving = False
            self.query_one("#saving").display = False
            for button in self.query(Button):
                button.disabled = False

    def action_dismiss_form(self) -> None:
        if not self.saving:
            self.dismiss(None)

    def field_value(self, key: str) -> str:
        widget = self.query_one(f"#{key}")
        if isinstance(widget, Select):
            return str(widget.value)
        if isinstance(widget, BooleanField):
            return widget.value
        return self.query_one(f"#{key}", Input).value


class Confirm(ModalScreen[bool]):
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > VerticalScroll { width: 80%; height: auto; max-height: 16; background: $surface; padding: 2; border: solid $warning; }
    Confirm Horizontal { height: 3; }
    """
    BINDINGS = [("escape", "no", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label(self.message)
            with Horizontal():
                yield Button("Proceed using saved results", id="yes", variant="warning")
                yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_no(self) -> None:
        self.dismiss(False)


class Exif(ModalScreen[None]):
    DEFAULT_CSS = """
    Exif { align: center middle; }
    Exif > VerticalScroll { width: 95%; height: 95%; background: $surface; }
    Exif TextArea { height: 1fr; }
    """
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield TextArea(
                self.text, language="json", read_only=True, show_line_numbers=True
            )
            yield Button("Close", id="close")

    def on_button_pressed(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
