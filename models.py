from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=True)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False)
    invited_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    created_pitches = relationship("Pitch", back_populates="creator")
    created_sessions = relationship("Session", back_populates="creator")
    session_players = relationship("SessionPlayer", back_populates="user")
    scores = relationship("Score", back_populates="user")
    planned_games = relationship("PlannedGame", back_populates="creator")
    invite_codes = relationship("InviteCode", back_populates="owner", foreign_keys="InviteCode.owner_id")

    def __repr__(self):
        return self.username


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(10), unique=True, nullable=False, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    used_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    used_at = Column(DateTime, nullable=True)

    owner = relationship("User", back_populates="invite_codes", foreign_keys=[owner_id])
    used_by = relationship("User", foreign_keys=[used_by_id])


class Pitch(Base):
    __tablename__ = "pitches"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    location = Column(String(300), nullable=True)
    place_id = Column(String(300), nullable=True, unique=True, index=True)
    num_holes = Column(Integer, nullable=False)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    creator = relationship("User", back_populates="created_pitches")
    holes = relationship("PitchHole", back_populates="pitch", order_by="PitchHole.hole_number")
    sessions = relationship("Session", back_populates="pitch")


class PitchHole(Base):
    __tablename__ = "pitch_holes"

    id = Column(Integer, primary_key=True, index=True)
    pitch_id = Column(Integer, ForeignKey("pitches.id"), nullable=False)
    hole_number = Column(Integer, nullable=False)
    par = Column(Integer, nullable=False)

    pitch = relationship("Pitch", back_populates="holes")


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, index=True)
    pitch_id = Column(Integer, ForeignKey("pitches.id"), nullable=False)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    invite_code = Column(String(6), unique=True, nullable=False, index=True)
    status = Column(String(20), default="active")  # active, completed
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    pitch = relationship("Pitch", back_populates="sessions")
    creator = relationship("User", back_populates="created_sessions")
    players = relationship("SessionPlayer", back_populates="session")
    scores = relationship("Score", back_populates="session")


class SessionPlayer(Base):
    __tablename__ = "session_players"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    joined_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("Session", back_populates="players")
    user = relationship("User", back_populates="session_players")


class Score(Base):
    __tablename__ = "scores"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    hole_number = Column(Integer, nullable=False)
    strokes = Column(Integer, nullable=False)

    session = relationship("Session", back_populates="scores")
    user = relationship("User", back_populates="scores")


class PlannedGame(Base):
    __tablename__ = "planned_games"

    id = Column(Integer, primary_key=True, index=True)
    pitch_id = Column(Integer, ForeignKey("pitches.id"), nullable=False)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    planned_date = Column(DateTime, nullable=False)
    note = Column(Text, nullable=True)
    status = Column(String(20), default="planned")  # planned, started, cancelled
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    pitch = relationship("Pitch")
    creator = relationship("User", back_populates="planned_games")
    session = relationship("Session")
    invitations = relationship("GameInvitation", back_populates="planned_game", cascade="all, delete-orphan")


class GameInvitation(Base):
    __tablename__ = "game_invitations"

    id = Column(Integer, primary_key=True, index=True)
    planned_game_id = Column(Integer, ForeignKey("planned_games.id"), nullable=False)
    email = Column(String(255), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status = Column(String(20), default="pending")  # pending, accepted, declined
    sent = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    planned_game = relationship("PlannedGame", back_populates="invitations")
    user = relationship("User")
