from dataclasses import dataclass


@dataclass(frozen=True)
class PanelOperator:
    """The credentials a test signs in with; the `panel_operator` fixture configures them."""

    username: str
    password: str
