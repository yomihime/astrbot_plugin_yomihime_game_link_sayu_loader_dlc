"""Initialize device metadata within the account model's guarded transaction."""

from sqlalchemy import and_, select, update

from plugin_orm import with_session


@with_session
async def initialize_device(users, session, uid, game_name, device_id, expected=None):
    # Re-select by role in the write statement: another delivery can rebind
    # or replace a row after the public getter observed a missing device.
    column = users.get_gameid_name(game_name)
    roles = users.__table__.alias()
    expected = expected or {}
    eligible = and_(
        roles.c.device_id.is_(None),
        *(roles.c[name] == value for name, value in expected.items()),
    )
    first_eligible = select(eligible).where(roles.c[column] == uid).limit(1)
    statement = update(users.__table__).where(
        users.__table__.c[column] == uid, first_eligible.scalar_subquery()
    )
    await session.execute(statement.values(device_id=device_id))
    row = await users.select_data_by_uid(uid, game_name)
    if row is None or any(getattr(row, k) != v for k, v in expected.items()):
        return None
    return row.device_id
