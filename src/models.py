"""Alert definitions and fire history.

FIRING SEMANTICS (the part that makes an alerter usable rather than a spam bot)
------------------------------------------------------------------------------
A naive alerter re-sends every poll for as long as the condition holds - "AAPL
above 300" becomes 60 emails an hour. Alerts here fire on the TRANSITION into
the condition, tracked by `armed`:

    condition false -> armed = True      (re-armed, ready)
    condition true and armed -> FIRE, armed = False

What happens after a fire depends on `repeat`:

    once      deactivate permanently. One notification, ever.
    rearm     re-arm only when the condition goes false again, i.e. fire once
              per CROSSING. This is what you usually want.
    cooldown  re-fire while still true, but at most once per cooldown_minutes.
"""
import datetime as dt
import enum

from sqlalchemy import (Boolean, DateTime, Enum, Float, ForeignKey, Integer,
                        String, Text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Kind(str, enum.Enum):
    price_above = "price_above"
    price_below = "price_below"
    pct_up = "pct_up"          # % change vs previous close, upward
    pct_down = "pct_down"      # % change vs previous close, downward


class Repeat(str, enum.Enum):
    once = "once"
    rearm = "rearm"
    cooldown = "cooldown"


KIND_LABEL = {
    Kind.price_above: "price rises above",
    Kind.price_below: "price falls below",
    Kind.pct_up: "gains at least",
    Kind.pct_down: "falls at least",
}


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    kind: Mapped[Kind] = mapped_column(Enum(Kind))
    threshold: Mapped[float] = mapped_column(Float)

    repeat: Mapped[Repeat] = mapped_column(Enum(Repeat), default=Repeat.rearm)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)
    extended_hours: Mapped[bool] = mapped_column(Boolean, default=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    armed: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_fired_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    fires: Mapped[list["Fire"]] = relationship(back_populates="alert",
                                               cascade="all, delete-orphan",
                                               order_by="Fire.fired_at.desc()")

    # ---- condition evaluation
    def satisfied(self, price: float, prev_close: float | None) -> bool:
        if price is None:
            return False
        if self.kind == Kind.price_above:
            return price > self.threshold
        if self.kind == Kind.price_below:
            return price < self.threshold
        if prev_close in (None, 0):
            return False
        pct = (price / prev_close - 1.0) * 100.0
        if self.kind == Kind.pct_up:
            return pct >= self.threshold
        return pct <= -abs(self.threshold)

    def describe(self) -> str:
        unit = "%" if self.kind in (Kind.pct_up, Kind.pct_down) else ""
        val = f"{self.threshold:g}{unit}" if unit else f"${self.threshold:,.2f}"
        return f"{self.ticker} {KIND_LABEL[self.kind]} {val}"


class Fire(Base):
    __tablename__ = "fires"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"), index=True)
    fired_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    price: Mapped[float] = mapped_column(Float)
    session: Mapped[str] = mapped_column(String(8), default="REG")
    delivered: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")

    alert: Mapped[Alert] = relationship(back_populates="fires")
