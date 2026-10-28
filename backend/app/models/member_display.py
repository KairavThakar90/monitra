from sqlalchemy import (
    BigInteger, Boolean, ForeignKeyConstraint, Identity, Integer, SmallInteger, TIMESTAMP,
    UniqueConstraint, text, func,
)
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from typing import Optional

from app.core.database import Base


class MemberDisplay(Base):
    """One screen of a member's computer, as the desktop last described it.

    The desktop reports the displays it can see (`PUT /screenshots/displays`);
    this table holds the latest report, one row per screen `number` (the
    1-based position in the operating system's list -- the same number the
    capture code uses). It exists for two reasons and holds nothing else:

    * an administrator can see how many screens each member is using, and
    * an administrator can switch capture off for a particular screen.

    `capture_enabled` is the administrator's choice and is **never touched by a
    report**. A screen that is unplugged keeps its row (`connected = false`), so
    a choice made for "screen 2" is still there when it is plugged back in and a
    report cannot quietly undo it. A screen no one has ever decided about is
    captured, exactly as every screen was before this existed.
    """

    __tablename__ = "member_displays"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    number: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    #: Geometry as the OS reported it, in the virtual desktop's pixels. A screen
    #: left of or above the primary has a negative `left` / `top`.
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    left: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    top: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    #: Whether the member's desktop listed this screen in its latest report.
    connected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: The administrator's choice. True (the default) means this screen is captured.
    capture_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name="fk_member_displays_organization", ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_member_displays_user", ondelete="CASCADE"),
        UniqueConstraint("user_id", "number", name="uq_member_displays_user_number"),
    )
