"""Atomic, durable admission of individual Dispatcharr refresh starts."""

from sqlalchemy.dialects.sqlite import insert

from apps.database import connection
from apps.database.models import M3URefreshAttempt


def _values(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class M3URefreshRepository:
    def claim(self, scope, account_id, attempt_id, started_at):
        with connection.get_session() as session:
            result = session.execute(insert(M3URefreshAttempt).values(
                server_scope=scope, account_id=account_id, attempt_id=attempt_id,
                started_at=started_at, state='sending',
            ).on_conflict_do_nothing())
            session.commit()
            return result.rowcount == 1

    def pending(self, scope):
        with connection.get_session() as session:
            return [_values(row) for row in session.query(M3URefreshAttempt).filter_by(
                server_scope=scope,
            ).order_by(M3URefreshAttempt.account_id).all()]

    def uncertain(self, scope, account_id, attempt_id):
        with connection.get_session() as session:
            session.query(M3URefreshAttempt).filter_by(
                server_scope=scope, account_id=account_id, attempt_id=attempt_id,
            ).update({'state': 'unknown'})
            session.commit()

    def release(self, scope, account_id, attempt_id):
        with connection.get_session() as session:
            count = session.query(M3URefreshAttempt).filter_by(
                server_scope=scope, account_id=account_id, attempt_id=attempt_id,
            ).delete()
            session.commit()
            return count == 1
